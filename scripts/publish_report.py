#!/usr/bin/env python3
"""Publish a source-bound Codex report through the local app, never directly to SQLite."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def publish(packet_path, markdown_path, base, phase=None):
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("回写只允许本机灵思 Lite 地址")
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8-sig"))
    content = Path(markdown_path).read_text(encoding="utf-8-sig") if not phase else None
    base = base.rstrip("/")
    with urlopen(base + "/api/health", timeout=10) as response:
        health = json.load(response)
    if health.get("product") != "linsi-lite":
        raise ValueError("这个端口不是灵思 Lite 服务")
    with urlopen(base + "/api/state", timeout=10) as response:
        token = json.load(response)["token"]
    body = {"service_account_key": packet["service_account_key"], "id": packet["report_id"],
            "source_digest": packet["source_digest"], **({"phase":phase} if phase else {"markdown":content})}
    request = Request(base + ("/api/reports/progress" if phase else "/api/reports/save"), data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "X-Linsi-Token": token}, method="POST")
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    return result["id"]


def main():
    parser = argparse.ArgumentParser(description="把 Codex 研究报告保存回本地网页")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--markdown")
    parser.add_argument("--phase",choices=("reading","analyzing","writing","failed"),help="在实际开始相应阶段时更新网页进度")
    parser.add_argument("--base", default=os.environ.get("LINSI_URL", "http://127.0.0.1:5030"))
    args = parser.parse_args()
    if not args.phase and not args.markdown:parser.error("发布报告需要 --markdown；更新阶段使用 --phase")
    try:
        identity = publish(args.packet, args.markdown, args.base,args.phase)
        print(("研究进度已更新：" if args.phase else "报告已回写：") + identity)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", "本地服务拒绝回写")
        except ValueError:
            message = "本地服务拒绝回写"
        raise SystemExit(message) from None
    except (URLError, OSError, ValueError, KeyError):
        raise SystemExit("回写失败：请检查本地服务、资料包与 Markdown 文件。不会修改其他报告。") from None


if __name__ == "__main__":
    main()
