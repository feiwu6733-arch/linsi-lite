"""Local AI configuration and OpenAI-compatible Chat Completions transport."""
import ipaddress
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .reports import atomic_text
from .store import InputError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an API credential to a redirect target.


class AIConfig:
    def __init__(self, directory):
        self.path = directory / 'ai-config.json'
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            try:
                return json.loads(self.path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                return {'base_url': '', 'model': '', 'api_key': '', 'timeout': 180}
            except (OSError, ValueError):
                raise InputError('AI 配置无法读取，请在设置中重新保存。') from None

    def public(self):
        c = self.read()
        return {k: c.get(k) for k in ('base_url', 'model', 'timeout')} | {'thinking_mode': c.get('thinking_mode', 'fast'),
            'has_key': bool(c.get('api_key')), 'configured': bool(c.get('base_url') and c.get('model')),
            'protocol': 'chat_completions'}

    def save(self, body):
        with self.lock:
            old = self.read()
            base = body.get('base_url', '').strip() if isinstance(body.get('base_url'), str) else ''
            base = base.rstrip('/')
            if base.endswith('/chat/completions'):
                base = base[:-len('/chat/completions')]
            try:
                u = urlsplit(base)
                local = u.hostname == 'localhost'
                try:
                    local = local or ipaddress.ip_address(u.hostname or '').is_loopback
                except ValueError:
                    pass
                valid = bool(u.hostname) and u.scheme in ('https', 'http') and (u.scheme == 'https' or local)
                valid = valid and not (u.username or u.password or u.query or u.fragment) and not any(ord(c) < 33 for c in base)
                _ = u.port
            except ValueError:
                valid = False
            if not valid or len(base) > 1000:
                raise InputError('请输入 HTTPS 接口根地址，例如 https://api.deepseek.com；本机模型可用 http://127.0.0.1:端口/v1。地址不能含密钥、查询参数或账号密码。')
            model = body.get('model', '').strip() if isinstance(body.get('model'), str) else ''
            if len(model) > 200 or any(ord(c) < 32 for c in model):
                raise InputError('请输入有效模型名称。')
            key = body.get('api_key', '')
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 for c in key):
                raise InputError('API Key 格式不正确。')
            key = key.strip()
            if body.get('clear_key') is True:
                key = ''
            elif not key:
                if old.get('base_url') and base != old['base_url'] and old.get('api_key'):
                    raise InputError('更换接口地址时，请重新填写该服务的 API Key，或勾选清除密钥以使用无密钥的本机服务。')
                key = old.get('api_key', '')
            timeout = body.get('timeout', 180)
            if type(timeout) is not int or not 30 <= timeout <= 600:
                raise InputError('超时请设置为 30～600 秒。')
            thinking = body.get('thinking_mode', old.get('thinking_mode', 'fast'))
            if thinking not in ('fast', 'deep', 'provider'): raise InputError('请选择有效的分析模式。')
            atomic_text(self.path, json.dumps({'base_url': base, 'model': model, 'api_key': key, 'timeout': timeout, 'thinking_mode': thinking}, ensure_ascii=False))
            if os.name != 'nt':
                os.chmod(self.path, 0o600)
            return self.public()


class AIClient:
    def __init__(self, config):
        self.config = config

    def request(self, path, payload=None, cancelled=lambda: False, progress=lambda n: None, config=None):
        c = config or self.config.read()
        if not c.get('base_url'):
            raise InputError('请先在「设置 → AI 接口配置」填写地址、API Key 和模型。')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if c.get('api_key'):
            headers['Authorization'] = 'Bearer ' + c['api_key']
        req = Request(c['base_url'] + path, data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None, headers=headers)
        started = time.monotonic()
        def check():
            if cancelled():
                raise InputError('已停止本次 AI 研究；已有结果保留。')
            if time.monotonic() - started > c.get('timeout', 180):
                raise InputError('AI 响应超时，请检查服务或调整设置中的超时时间后重试。')
        try:
            check()
            with build_opener(NoRedirect).open(req, timeout=min(c.get('timeout', 180), 30)) as response:
                check()
                if 'text/event-stream' not in response.headers.get('Content-Type', '').lower():
                    chunks, size = [], 0
                    while True:
                        check()
                        chunk = response.read(16384)
                        if not chunk: break
                        size += len(chunk)
                        if size > 2000000: raise InputError('AI 响应过大，请减少样本。')
                        chunks.append(chunk)
                    return json.loads(b''.join(chunks))
                parts, size, finish, ended, received = [], 0, None, False, 0
                event_lines = []
                def consume(lines):
                    nonlocal finish, ended, received
                    data = b'\n'.join(lines).strip()
                    if not data: progress(received); return
                    if data == b'[DONE]': ended = True; return
                    try: event = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise InputError('AI 流式响应中有无法解析的数据，本次未保存报告；请重试当前报告。') from None
                    if not isinstance(event, dict): raise InputError('AI 返回了无效的响应事件，请重试当前报告。')
                    if event.get('error'): raise InputError('AI 服务返回错误，请检查模型、额度或接口状态。')
                    choices = event.get('choices') or []
                    if not isinstance(choices, list): raise InputError('AI 返回的候选结果格式不正确。')
                    for choice in choices[:1]:
                        delta = choice.get('delta') or {}
                        content = delta.get('content')
                        if isinstance(content, str) and content:
                            parts.append(content); received += len(content)
                        if choice.get('finish_reason'): finish = choice['finish_reason']
                    progress(received)
                while True:
                    check()
                    line = response.readline(262145)
                    if not line:
                        if event_lines: consume(event_lines)
                        break
                    size += len(line)
                    if size > 32000000 or len(line) > 262144: raise InputError('AI 流式响应超过读取上限，请减少样本或使用快速模式。')
                    line = line.removeprefix(b'\xef\xbb\xbf').rstrip(b'\r\n')
                    if not line:
                        if event_lines: consume(event_lines); event_lines = []
                        else: progress(received)
                        if ended: break
                    elif line.startswith(b'data:'):
                        data = line[5:]
                        if data.startswith(b' '): data = data[1:]
                        event_lines.append(data)
                    elif line.startswith(b':'): progress(received)
                check()
                if not ended and finish != 'stop': raise InputError('AI 连接中断，尚未保存完整报告，请重试。')
                return {'choices': [{'message': {'content': ''.join(parts)}, 'finish_reason': finish}]}

        except HTTPError as e:
            labels = {401: 'API Key 无效或未填写', 403: '该接口或模型没有访问权限', 404: '接口地址或模型不存在', 429: '调用频率或额度受限'}
            raise InputError(f"AI 请求失败（HTTP {e.code}）：{labels.get(e.code, '服务暂不可用或地址发生跳转')}。请在设置中检查后重试。") from None
        except InputError:
            raise
        except (URLError, TimeoutError, OSError):
            raise InputError('无法连接 AI 接口或等待响应超时，请检查网络、接口地址与服务状态。') from None
        except (ValueError, TypeError, KeyError):
            raise InputError('接口返回格式不兼容，请选择支持 Chat Completions 的服务。') from None

    def complete(self, messages, cancelled=lambda: False, progress=lambda n: None):
        c = self.config.read()
        if not c.get('model'): raise InputError('请先填写模型名称并保存配置。')
        payload = {'model': c.get('model'), 'messages': messages, 'stream': True}
        if urlsplit(c.get('base_url', '')).hostname == 'api.deepseek.com':
            mode = c.get('thinking_mode', 'fast')
            if mode != 'provider': payload['thinking'] = {'type': 'disabled' if mode == 'fast' else 'enabled'}
        result = self.request('/chat/completions', payload, cancelled, progress, c)
        try:
            choice = result['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise InputError('AI 输出未完整结束（可能达到输出上限或被过滤），请调整模型或减少样本后重试。')
            content = choice['message']['content']
            if not isinstance(content, str) or not content.strip(): raise ValueError()
            return content.strip()
        except InputError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise InputError('AI 未返回有效正文；推理过程或空响应不能算作研究结果。') from None

    def models(self):
        data = self.request('/models')
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise InputError('接口不支持模型列表，请手动填写模型名称。')
        return sorted({m['id'] for m in data['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)})[:300]

    def test(self):
        self.complete([{'role': 'user', 'content': 'Reply with OK only.'}])
        return {'ok': True, 'message': '连接测试成功，所选模型已返回有效响应。'}
