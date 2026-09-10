#!/usr/bin/env python3
"""Optional local download/transcription. No cookies, cloud ASR or bundled private code."""
import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 500 * 1024 * 1024


def public_https(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("请提供 HTTPS 音视频直链，不支持主页、凭据或自定义端口")
    for answer in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(answer[4][0]).is_global:
            raise ValueError("下载地址不能指向内网或本机")


class MediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, folder):
    public_https(url)
    temporary = folder / "media.part"
    target = folder / "source.media"
    total = 0
    with build_opener(MediaRedirect()).open(Request(url, headers={"User-Agent":"Linsi-Lite/0.1"}), timeout=30) as response:
        content_type = response.headers.get("Content-Type", "").split(";")[0]
        if not (content_type.startswith(("audio/", "video/")) or content_type == "application/octet-stream"):
            raise ValueError("响应不是音视频文件；请提供可访问的媒体直链")
        with temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("单次下载上限为 500 MB；未完成文件保留为 .part")
                output.write(chunk)
    if not total:
        raise ValueError("下载内容为空，未标记为完成")
    temporary.rename(target)
    return target


def transcribe(path, folder, model):
    if not path.is_file():
        raise ValueError("输入文件不存在")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ValueError("未安装可选转写依赖。请按 README 建立单独虚拟环境并安装 faster-whisper；基础网页不需要此依赖。") from None
    engine = WhisperModel(model, device="cpu", compute_type="int8")
    segments, info = engine.transcribe(str(path), language="zh", vad_filter=True)
    rows = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    content = "\n".join(s["text"] for s in rows if s["text"])
    if not content.strip():
        raise ValueError("未识别到有效文案，不能标记为可研究")
    (folder / "transcript.txt").write_text(content, encoding="utf-8")
    (folder / "segments.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder / "transcript.txt"


def main():
    parser = argparse.ArgumentParser(description="可选本地音视频处理；只处理明确提供的文件或直链")
    parser.add_argument("action", choices=["download", "transcribe"])
    parser.add_argument("--aweme-id", required=True)
    parser.add_argument("--url")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--model", default="small", help="首次运行可能下载模型；也可填写已有模型目录")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{10,30}", args.aweme_id):
        raise SystemExit("请提供真实稳定的 aweme_id")
    folder = Path(os.environ.get("LINSI_DATA_DIR", ROOT / ".local")) / "media" / args.aweme_id / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True, exist_ok=False)
    try:
        if args.action == "download":
            if not args.url:
                raise ValueError("下载需要 --url 媒体直链")
            result = download(args.url, folder)
        else:
            if args.file is None:
                raise ValueError("转写需要 --file 本地音视频路径")
            result = transcribe(args.file, folder, args.model)
        print("处理完成：" + str(result.resolve()))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        # URLs can contain time-limited signatures; never echo them in errors or metadata.
        raise SystemExit("处理失败，请检查文件、网络与可选依赖。没有删除原始素材。") from None


if __name__ == "__main__":
    main()
