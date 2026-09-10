"""Opt-in user-configured normalized JSON source. Never copies cloud connectors."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .store import InputError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_config(directory):
    path = directory / "connector.json"
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError()
        return config
    except (OSError, ValueError):
        raise InputError("本地 connector.json 格式不正确") from None


def fetch_account(directory, sec_uid):
    config = read_config(directory)
    if config is None:
        raise InputError("尚未配置在线数据连接器。可以先导入作品清单与文案，或按 README 配置自己的数据服务。")
    template = config.get("account_url", "")
    if not isinstance(template, str) or "{sec_uid}" not in template:
        raise InputError("连接器 account_url 需要包含 {sec_uid}")
    url = template.replace("{sec_uid}", quote(sec_uid, safe=""))
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise InputError("连接器必须配置不含凭据的 HTTPS 地址")
    # Configuration is owned by the local operator; remote redirects are never followed.
    token = os.environ.get("LINSI_CONNECTOR_TOKEN", "")
    headers = {"Accept": "application/json", "User-Agent": "Linsi-Lite/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=25) as response:
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise InputError("连接器响应过大，请减小单次作品数量")
        bundle = json.loads(data)
    except (HTTPError, URLError, TimeoutError, ValueError):
        raise InputError("数据服务请求失败。请检查服务状态、响应格式和本机凭据；未修改现有资料。") from None
    if not isinstance(bundle, dict) or not isinstance(bundle.get("account"), dict) or bundle["account"].get("sec_uid") != sec_uid:
        raise InputError("连接器返回的账号身份不匹配；未写入资料")
    return bundle
