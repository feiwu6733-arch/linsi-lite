#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="灵思 Lite 本地诊断，不停止任何进程")
    parser.add_argument("--port", type=int, default=5030)
    args = parser.parse_args()
    ok = sys.version_info >= (3, 11)
    print(f"Python: {sys.version.split()[0]} {'OK' if ok else '需要 3.11+'}")
    directory = Path(os.environ.get("LINSI_DATA_DIR", ROOT / ".local"))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as file:
            file.write(b"linsi-health")
        print("数据目录: 可写")
    except OSError:
        print("数据目录: 不可写")
        ok = False
    database = directory / "linsi.sqlite3"
    if database.exists():
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
            print("数据库: " + result)
            ok &= result == "ok"
        except sqlite3.Error:
            print("数据库: 无法读取，请先保留备份")
            ok = False
    else:
        print("数据库: 首次启动时创建")
    try:
        with urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=3) as response:
            health = json.load(response)
        print("本地服务: " + ("运行中，版本 " + str(health.get("version", "未知")) if health.get("product") == "linsi-lite" else "端口被其他程序使用"))
        ok &= health.get("product") == "linsi-lite"
    except (URLError, TimeoutError, ValueError):
        print("本地服务: 未连接；运行 python app.py 启动")
    print("浏览器组件: " + ("已安装；需在专用浏览器扫码验证" if importlib.util.find_spec("playwright") else "未安装，请运行安装采集组件.cmd"))
    print("转写组件: " + ("已安装" if importlib.util.find_spec("faster_whisper") else "未安装，请运行安装转写组件.cmd"))
    print("转写模型: " + ("已有准备完成记录" if (directory / "models" / "ready.json").exists() else "尚未准备，请在任务中心准备模型"))
    print("安装脚本使用 .venv；如全局 Python 显示组件缺失，请用 .venv\\Scripts\\python.exe 运行本诊断。")
    print("诊断不读取或输出凭据，不停止任何进程。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
