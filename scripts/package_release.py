#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from linsi import VERSION
from linsi.updates import RELEASES
from scripts.validate_release import validate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--stable-name",action="store_true")
    args=parser.parse_args()
    paths = validate()
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination / (f"linsi-lite-{VERSION}.zip" if args.stable_name else f"linsi-lite-{VERSION}-{timestamp}.zip")
    manifest = {"product": "Linsi Lite", "version": VERSION, "update_format":1, "minimum_python":[3,11], "files": []}
    with zipfile.ZipFile(target, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in paths:
            relative = source.relative_to(ROOT).as_posix()
            content = source.read_bytes()
            archive.writestr(relative, content)
            manifest["files"].append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
        archive.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(digest + "  " + target.name + "\n", encoding="utf-8")
    if args.stable_name:
        announcement={"tag_name":"v"+VERSION,"draft":False,"prerelease":False,"body":(ROOT/"RELEASE_NOTES.md").read_text(encoding="utf-8"),"assets":[{"name":target.name,"size":target.stat().st_size,"digest":"sha256:"+digest,"browser_download_url":RELEASES+"/download/v"+VERSION+"/"+target.name}]}
        (destination/"update.json").write_text(json.dumps(announcement,ensure_ascii=False,indent=2),encoding="utf-8")
    print("发布包：" + str(target))
    print("SHA-256: " + digest)


if __name__ == "__main__":
    main()
