#!/usr/bin/env python3
"""Validate the exact public surface. Runtime data is never scanned or packaged."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def public_files(root=ROOT):
    root = Path(root).resolve()
    paths = json.loads((root / "release-files.json").read_text(encoding="utf-8"))
    if not isinstance(paths, list) or len(set(paths)) != len(paths):
        raise ValueError("发布白名单格式错误或存在重复项")
    result = []
    for relative in paths:
        source = root / relative
        if source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_file():
            raise ValueError("发布文件缺失、是链接或超出项目目录：" + relative)
        if any(p in (".local", ".git", "dist", "artifacts", "__pycache__", ".venv") for p in Path(relative).parts):
            raise ValueError("发布清单包含运行数据：" + relative)
        result.append(source)
    return result


def validate():
    paths = public_files()
    patterns = [r"github_pat_[a-zA-Z0-9_]{30,}", r"ghp_[a-zA-Z0-9]{30,}", r"sk-[a-zA-Z0-9_-]{35,}", r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"]
    for path in paths:
        if path.suffix == ".png":
            if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("PNG 图片无效：" + path.name)
            continue
        if path.suffix == ".jpg":
            content = path.read_bytes()
            if not content.startswith(b"\xff\xd8\xff") or not content.endswith(b"\xff\xd9"):
                raise ValueError("JPEG 图片无效：" + path.name)
            continue
        if path.suffix == ".webp":
            content = path.read_bytes()
            if content[:4] != b"RIFF" or content[8:12] != b"WEBP":
                raise ValueError("WebP 图片无效：" + path.name)
            continue
        if path.suffix == ".mp4":
            with path.open("rb") as handle:
                if handle.read(12)[4:8] != b"ftyp":
                    raise ValueError("MP4 视频无效：" + path.name)
            continue
        content = path.read_text(encoding="utf-8-sig")
        if any(re.search(pattern, content) for pattern in patterns):
            raise ValueError("疑似凭据，禁止打包：" + path.name)
        if path.suffix == ".json":
            json.loads(content)
    brand = json.loads((ROOT / "brand.json").read_text(encoding="utf-8"))
    if not brand.get("wechat") or not brand.get("website", "").startswith("https://"):
        raise ValueError("公开试用入口未配置")
    print(f"发布校验通过：{len(paths)} 个白名单文件；JSON 和试用配置有效。")
    print("检查仅覆盖公开文件，不读取业务数据。此检查不替代代码来源与新增依赖许可证审核。")
    return paths


if __name__ == "__main__":
    try:
        validate()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
