#!/usr/bin/env python3
"""Linsi Lite local web app. Python 3.11+, standard library only."""
import argparse
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from linsi import VERSION
from linsi.connector import fetch_account, read_config
from linsi.reports import atomic_text, save_sources
from linsi.store import DIRECTIONS, InputError, Store, text, web_url
from linsi.jobs import Jobs
from linsi.research import Research
from linsi.ai_runner import AIRunner
from linsi.updates import Updates, inspect_package


def report_summary(report):
    """Project display metadata without exposing source text in polling responses."""
    return {**{k: v for k, v in report.items() if k not in ("snapshot", "markdown")},
            "source_count": len(report.get("snapshot", {}).get("sources", []))}

ROOT = Path(__file__).resolve().parent


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port=5030, data_dir=None):
        self.lifecycle_lock = threading.RLock()
        self.installing_update = False
        self.store = Store(data_dir or os.environ.get("LINSI_DATA_DIR", ROOT / ".local"))
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.jobs = Jobs(self.store, base_url=f"http://127.0.0.1:{self.server_port}")
        self.research = Research(self.store, self.jobs, f"http://127.0.0.1:{self.server_port}")
        self.ai = AIRunner(self.research)
        self.updates = Updates(self.store.directory, VERSION)

    def server_close(self):
        if hasattr(self, "updates"):
            self.updates.close()
        if hasattr(self, "ai"):
            self.ai.close()
        if hasattr(self, "research"):
            self.research.close()
        if hasattr(self, "jobs"):
            self.jobs.close()
        super().server_close()

    def brand(self):
        # Product contact details come only from the distributed brand configuration.
        return json.loads((ROOT / "brand.json").read_text(encoding="utf-8-sig"))

    def install_update(self):
        if os.name != 'nt':raise InputError('自动安装目前支持 Windows，请下载发布包更新')
        if self.updates.public()['status'] != 'ready':raise InputError('请先下载并校验更新包')
        for profile in self.store.profiles():
            scope=profile['service_account_key']
            rows=self.store.records(scope,'job')+self.store.records(scope,'connection_flow')+self.store.records(scope,'report')+self.research.summaries(scope)
            if any(row.get('status') in ('queued','running','cancelling','analyzing','waiting_login','preparing') or row.get('api_requested') or (row.get('ai_auto') and row.get('status') in ('partial_ready','awaiting_ai')) for row in rows):
                raise InputError('仍有任务进行中，请完成或取消后再更新')
        directory=self.updates.directory
        pending=json.loads((directory/'pending.json').read_text(encoding='utf-8'))
        inspect_package(directory/'download.zip',pending['version'],pending['sha256'])
        worker=directory/'install-worker.py'
        shutil.copy2(ROOT/'linsi/updates.py',worker)
        lock_path=ROOT/'.linsi-update.lock'
        try:
            with lock_path.open('x',encoding='utf-8') as lock:json.dump({'pid':os.getpid(),'port':self.server_port},lock)
        except FileExistsError:raise InputError('此安装目录已有更新在进行，请稍后重试') from None
        try:
            with (directory/'installer.log').open('ab') as log:
                subprocess.Popen([sys.executable,str(worker),str(ROOT),str(self.store.directory.resolve()),str(self.server_port),str(os.getpid())],cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            lock_path.unlink();raise
        self.installing_update=True
        threading.Timer(1, self.shutdown).start()
        return {'status':'installing','version':pending['version'],'message':'正在备份并更新，完成后页面会自动恢复'}


class Handler(BaseHTTPRequestHandler):
    server_version = "LinsiLite/" + VERSION

    def log_message(self, fmt, *args):
        # Do not log user-provided URLs, bodies, source text or connector credentials.
        pass

    def guard(self, mutation=False):
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise PermissionError("仅允许本机访问")
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise PermissionError("禁止跨站请求")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("禁止跨站请求")
        if mutation and not secrets.compare_digest(self.headers.get("X-Linsi-Token", ""), self.server.token):
            raise PermissionError("会话已更新，请刷新网页后重试")

    def send(self, data, status=200, content_type="application/json; charset=utf-8", filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise InputError("请发送 JSON 数据")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise InputError("请求长度不正确") from None
        if length <= 0 or length > 4 * 1024 * 1024:
            raise InputError("请求为空或超过 4 MB，请分批导入")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise InputError("JSON 格式不正确") from None
        if not isinstance(body, dict):
            raise InputError("请求必须是 JSON 对象")
        return body

    def stream_video(self, file, filename=None):
        size=file.stat().st_size
        start,end=0,size-1
        requested=self.headers.get("Range")
        status=200
        if requested:
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",requested)
            if not match or not any(match.groups()):
                return self.send({"error":"不支持的 Range 请求"},416)
            if match[1]:start=int(match[1]);end=min(int(match[2]),size-1) if match[2] else size-1
            else:start=max(0,size-int(match[2]))
            if start>=size or start>end:
                self.send_response(416);self.send_header("Content-Range",f"bytes */{size}");self.send_header("Content-Length","0");self.end_headers();return
            status=206
        with file.open("rb") as handle:
            signature=handle.read(4)
            self.send_response(status)
            self.send_header("Content-Type","video/webm" if signature==b"\x1aE\xdf\xa3" else "video/mp4")
            self.send_header("Accept-Ranges","bytes")
            self.send_header("Content-Length",str(end-start+1))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            if status==206:self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
            if filename:self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
            self.end_headers()
            handle.seek(start);remaining=end-start+1
            while remaining>0:
                chunk=handle.read(min(1024*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)

    def error(self, error):
        if isinstance(error, PermissionError):
            return self.send({"error": str(error)}, 403)
        if isinstance(error, InputError):
            return self.send({"error": str(error)}, 400)
        if isinstance(error, sqlite3.Error):
            return self.send({"error": "本地数据库暂时无法写入，请确认磁盘可写后重试。"}, 503)
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        print(f"Request failed: {type(error).__name__}", file=sys.stderr)
        self.send({"error": "本地服务遇到错误。请运行诊断脚本；原有记录仍保留。"}, 500)

    def do_GET(self):
        try:
            self.guard()
            self.get_route()
        except Exception as error:
            self.error(error)

    def get_route(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path, store = parsed.path, self.server.store
        scope = query.get("service_account_key", "")
        if path == "/api/health":
            return self.send({"ok": True, "product": "linsi-lite", "version": VERSION, "local_only": True, "brand_read_only": True, "updates_enabled":True, "pid":os.getpid()})
        if path == "/api/updates":
            return self.send(self.server.updates.public())
        if path == "/api/state":
            state = {"version": VERSION, "ai": self.server.ai.config.public(), "token": self.server.token, "profiles": store.profiles(),
                     "directions": DIRECTIONS, "brand": self.server.brand(),
                     "connector_ready": (store.directory / "connector.json").exists(),
                     "capabilities": self.server.jobs.capabilities(), "connection": None, "binding": None, "jobs": [],
                     "accounts": [], "posts": [], "reports": [], "batches": [], "profile": None}
            if scope:
                state["profile"] = store.profile(scope)
                state["connection"] = self.server.jobs.state(scope)
                state["binding"] = store.bound_account(scope)
                state["jobs"] = self.server.jobs.public_jobs(scope)
                state["accounts"] = store.records(scope, "account")
                state["posts"] = store.records(scope, "post")
                state["reports"] = [report_summary(report) for report in store.records(scope, "report")]
                state["batches"] = self.server.research.summaries(scope)
            return self.send(state)
        if path == "/api/jobs":
            return self.send({"jobs":self.server.jobs.public_jobs(scope),"connection":self.server.jobs.state(scope),"capabilities":self.server.jobs.capabilities(),"reports":[report_summary(r) for r in store.records(scope,"report")],"batches":self.server.research.summaries(scope)})
        if path == "/api/research":
            return self.send(self.server.research.get(scope, query.get("id")))
        if path == "/api/research/packet":
            return self.send(self.server.research.packet(scope, query.get("id")), filename="batch-sources.json")
        if path == "/api/research/export":
            content = self.server.research.export(scope, query.get("id"))
            return self.send(content.encode('utf-8'), content_type="text/markdown; charset=utf-8", filename="linsi-research.md")
        if path == "/api/ai/config":
            return self.send(self.server.ai.config.public())
        if path == "/api/image":
            if query.get("kind") not in ("post","account"):
                raise InputError("图片类型不正确")
            record=store.get(scope,query["kind"],query.get("id"))
            image_path=record.get("image_path")
            if not image_path:return self.send({"error":"暂无图片"},404)
            file=(store.directory/image_path).resolve()
            if not file.is_relative_to(store.directory/"thumbnails") or not file.is_file():
                raise InputError("图片不存在")
            return self.send(file.read_bytes(),content_type=record.get("image_mime","image/jpeg"))
        if path in ("/api/media/export","/api/media/preview"):
            record=store.get(scope,"post",query.get("aweme_id"))
            file=(store.directory/record.get("media_path","missing")).resolve()
            if not file.is_relative_to(store.directory/"media") or not file.is_file():
                raise InputError("本地视频不存在，请先采集作品")
            return self.stream_video(file,record["aweme_id"]+".mp4" if path.endswith("export") else None)
        if path in ("/api/comments","/api/comments/export"):
            post=store.get(scope,"post",query.get("aweme_id"))
            comments=[row for row in store.records(scope,"comment") if row["aweme_id"]==post["aweme_id"]]
            comments.sort(key=lambda row:row.get("likes") or 0,reverse=True)
            result={"aweme_id":post["aweme_id"],"comments":comments,"count":len(comments),"has_more":post.get("comments_has_more"),"status":post.get("comments_status","not_started"),"error":post.get("comments_error","")}
            return self.send(result,filename="linsi-comments.json" if path.endswith("export") else None)
        if path == "/api/report":
            return self.send(store.get(scope, "report", query.get("id")))
        if path == "/api/report/export":
            report = store.get(scope, "report", query.get("id"))
            return self.send(report["markdown"].encode(), content_type="text/markdown; charset=utf-8", filename=f"linsi-report-{report['id']}.md")
        if path == "/api/account/export":
            account = store.get(scope, "account", query.get("sec_uid"))
            posts = [p for p in store.records(scope, "post") if p["sec_uid"] == account["sec_uid"]]
            data = json.dumps({"schema_version": 1, "account": account, "posts": posts}, ensure_ascii=False, indent=2)
            return self.send(data.encode(), filename="linsi-account.json")
        if path == "/api/report/packet":
            report = store.get(scope, "report", query.get("id"))
            packet = {"report_id": report["id"], "service_account_key": scope, "source_digest": report["source_digest"], "report_type":report.get("report_type","account"), **report["snapshot"]}
            return self.send(json.dumps(packet, ensure_ascii=False, indent=2).encode(), filename="sources.json")
        assets = {"/": "static/index.html", "/app.js": "static/app.js", "/workbench.js":"static/workbench.js", "/studio.js":"static/studio.js", "/ai.js":"static/ai.js", "/research.js":"static/research.js", "/research.css":"static/research.css", "/style.css": "static/style.css", "/icon.png": "static/icon.png", "/favicon.ico": "static/icon.png"}
        assets["/updates.js"] = "static/updates.js"
        assets.update({"/cloud-" + key + ".png": "static/cloud-" + key + ".png" for key in ("research", "knowledge", "script", "editor")})
        if path == "/auto-edit-demo.mp4":
            return self.stream_video(ROOT / "static/auto-edit-demo.mp4")
        assets["/auto-edit-demo.webp"] = "static/auto-edit-demo.webp"
        if path in assets:
            file = ROOT / assets[path]
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".webp":"image/webp"}[file.suffix]
            return self.send(file.read_bytes(), content_type=mime + "; charset=utf-8")
        return self.send({"error": "页面不存在"}, 404)

    def do_POST(self):
        try:
            self.guard(mutation=True)
            with self.server.lifecycle_lock:
                if self.server.installing_update:raise InputError('正在更新，请稍后重试')
                self.post_route(self.body())
        except Exception as error:
            self.error(error)

    def post_route(self, body):
        store, path = self.server.store, urlparse(self.path).path
        scope = body.get("service_account_key", "")
        if path == '/api/updates/check':return self.send(self.server.updates.request_check())
        if path == '/api/updates/dismiss':return self.send(self.server.updates.dismiss(body.get('action')))
        if path == '/api/updates/download':return self.send(self.server.updates.download(),202)
        if path == '/api/updates/install':return self.send(self.server.install_update(),202)
        if path == "/api/ai/config":
            return self.send(self.server.ai.config.save(body))
        if path == "/api/ai/test":
            return self.send(self.server.ai.client.test())
        if path == "/api/ai/models":
            return self.send({"models": self.server.ai.client.models()})
        if path == "/api/reports/run-ai":
            return self.send(self.server.ai.enable_report(scope, body.get("id")))
        if path == "/api/reports/cancel-ai":
            return self.send(self.server.ai.cancel_report(scope, body.get("id")))
        if path == "/api/research/create":
            return self.send(self.server.research.create(scope, body), 201)
        if path == "/api/research/confirm":
            b = self.server.research.confirm(scope, body.get("id"), body)
            if b['mode'] in ('research', 'breakdown') and self.server.ai.config.public()['configured']:
                b = self.server.ai.enable(scope, b['id'])
            return self.send(b)
        if path in ("/api/research/retry", "/api/research/continue"):
            b = self.server.research.resume(scope, body.get("id"), path.endswith("continue"))
            if b['mode'] in ('research', 'breakdown') and self.server.ai.config.public()['configured']:
                b = self.server.ai.enable(scope, b['id'])
            return self.send(b)
        if path == "/api/research/cancel":
            return self.send(self.server.research.cancel(scope, body.get("id")))
        if path == "/api/research/run-ai":
            return self.send(self.server.ai.enable(scope, body.get("id")))
        if path == "/api/workspaces/start":
            return self.send(store.workspace(reuse=True))
        if path == "/api/workspaces/create":
            return self.send(store.workspace(body.get("name", "")), 201)
        if path == "/api/workspaces/update":
            return self.send(store.update_profile(scope, body))
        if path == "/api/profiles":
            return self.send(store.create_profile(body), 201)
        if path == "/api/demo":
            existing = [p for p in store.profiles() if p["demo"]]
            if existing:
                return self.send(existing[0])
            profile = store.create_profile({"name": "小店内容观察 · 演示", "direction": "县城商业IP", "positioning": "体验如何整理小店账号的代表作品，所有人物、作品和数据均为虚构。"}, demo=True)
            store.import_bundle(profile["service_account_key"], json.loads((ROOT / "examples/demo.json").read_text(encoding="utf-8")))
            return self.send(profile, 201)
        if path == "/api/brand":
            raise PermissionError("云端联系方式由灵思统一提供，不支持修改")
        store.profile(scope)
        if path == "/api/jobs/create":
            return self.send(self.server.jobs.submit(scope,body.get("action"),body.get("payload")),202)
        if path == "/api/jobs/cancel":
            return self.send(self.server.jobs.cancel(scope,body.get("id")))
        if path == "/api/jobs/retry":
            return self.send(self.server.jobs.retry(scope,body.get("id")),202)
        if path == "/api/douyin/bind":
            connection=self.server.jobs.state(scope)
            identity=connection.get("identity")
            if connection["status"]!="connected" or not identity or identity["sec_uid"]!=body.get("sec_uid"):
                raise InputError("请先检查登录，确认绑定的是当前登录账号")
            return self.send(store.bind_my_account(scope,identity))
        if path == "/api/import":
            return self.send(store.import_bundle(scope, body.get("bundle")), 201)
        if path == "/api/transcript":
            return self.send(store.save_transcript(scope, body))
        if path == "/api/sync":
            account = store.get(scope, "account", body.get("sec_uid"))
            if account["demo"]:
                raise InputError("演示账号没有在线数据，请在真实研究空间中添加账号")
            bundle = fetch_account(store.directory, account["sec_uid"])
            return self.send(store.import_bundle(scope, bundle))
        if path == "/api/reports/create":
            report = store.create_report(scope, body)
            save_sources(store.directory, report)
            if self.server.ai.config.public()['configured']:
                report = self.server.ai.enable_report(scope, report['id'])
            return self.send(report, 201)
        if path == "/api/reports/progress":
            return self.send(store.report_progress(scope,body))
        if path == "/api/reports/save":
            report = store.save_report(scope, body)
            atomic_text(store.directory / "reports" / f"{report['id']}.md", report["markdown"])
            return self.send(report)
        return self.send({"error": "接口不存在"}, 404)


def main():
    parser = argparse.ArgumentParser(description="灵思 Lite 本地对标研究")
    parser.add_argument("--port", type=int, default=5030)
    parser.add_argument("--data-dir", help="独立运行数据目录，默认项目内 .local")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("请安装 Python 3.11 或更新版本")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("端口需在 1024～65535 之间")
    try:
        server = Server(args.port, args.data_dir)
    except OSError:
        raise SystemExit(f"端口 {args.port} 已被占用，或数据目录不可写。请先运行 scripts/doctor.py；不会停止其他服务。") from None
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"Linsi Lite {VERSION}  {address}", flush=True)
    print("Press Ctrl+C to stop. Local data is preserved.", flush=True)
    server.updates.start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
