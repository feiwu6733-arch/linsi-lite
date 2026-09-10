import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import ROOT, Server
from linsi.connector import fetch_account
from linsi.store import InputError, Store
from scripts.publish_report import publish


def fixture(name="真实资料测试"):
    return {"account": {"sec_uid": "MS4wLjAB-test-identity", "name": name}, "posts": [
        {"aweme_id": "7600000000000000001", "title": "样本一", "transcript": "第一份有效文案。", "likes": 10},
        {"aweme_id": "7600000000000000002", "title": "样本二", "transcript": "第二份有效文案。", "likes": None}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linsi-test-")
        self.env = patch.dict(os.environ, {"KNOWLEDGE_VAULT_DIR": str(Path(self.temp.name) / "unused-vault"), "LINSI_DATA_DIR": self.temp.name})
        self.env.start()
        self.store = Store(self.temp.name)
        self.profile = self.store.create_profile({"name": "测试空间", "direction": "县城商业IP", "positioning": "测试独立空间"})
        self.scope = self.profile["service_account_key"]
        self.bundle = fixture()
        self.store.import_bundle(self.scope, self.bundle)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def report(self):
        return self.store.create_report(self.scope, {"sec_uid": self.bundle["account"]["sec_uid"], "aweme_ids": [p["aweme_id"] for p in self.bundle["posts"]]})

    def test_partial_sync_preserves_text_notes_unknown_fields(self):
        identity = self.bundle["posts"][0]["aweme_id"]
        with self.store.connect() as db:
            post = self.store.get(self.scope, "post", identity, db)
            post.update(notes="我的原笔记", future_field={"kept": True})
            self.store.put(db, self.scope, "post", identity, post)
        self.store.import_bundle(self.scope, {"account": self.bundle["account"], "posts": [{"aweme_id": identity, "title": "更新标题", "likes": 22}]})
        post = self.store.get(self.scope, "post", identity)
        self.assertEqual(post["transcript"], "第一份有效文案。")
        self.assertEqual(post["notes"], "我的原笔记")
        self.assertTrue(post["future_field"]["kept"])
        self.assertEqual(post["likes"], 22)

    def test_invalid_batch_is_atomic(self):
        bad = copy.deepcopy(self.bundle)
        bad["account"]["name"] = "不应写入"
        bad["posts"][1]["likes"] = -1
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, bad)
        self.assertEqual(self.store.get(self.scope, "account", self.bundle["account"]["sec_uid"])["name"], "真实资料测试")

    def test_missing_scope_and_cross_scope_blocked(self):
        with self.assertRaises(InputError):
            self.store.records("", "post")
        other = self.store.create_profile({"name": "另一空间", "direction": "男性成长", "positioning": "不继承任何数据"})
        key = other["service_account_key"]
        self.assertEqual(self.store.records(key, "post"), [])
        with self.assertRaises(InputError):
            self.store.get(key, "post", "7600000000000000001")
        report = self.report()
        with self.assertRaises(InputError):
            self.store.save_report(key, {"id": report["id"], "source_digest": report["source_digest"], "markdown": "禁止跨空间"})

    def test_report_snapshot_survives_transcript_edit(self):
        report = self.report()
        self.store.save_transcript(self.scope, {"aweme_id": "7600000000000000001", "transcript": "修改后的文案"})
        old = self.store.get(self.scope, "report", report["id"])
        self.assertEqual(old["snapshot"]["sources"][0]["transcript"], "第一份有效文案。")
        self.assertEqual(old["status"], "awaiting_ai")

    def test_empty_transcript_and_wrong_digest_rejected(self):
        report = self.report()
        with self.assertRaises(InputError):
            self.store.save_report(self.scope, {"id": report["id"], "source_digest": "wrong", "markdown": "研究"})
        with self.assertRaises(InputError):
            self.store.save_report(self.scope, {"id": report["id"], "source_digest": report["source_digest"], "markdown": "缺少来源"})
        self.store.save_transcript(self.scope, {"aweme_id": "7600000000000000001", "transcript": " "})
        with self.assertRaises(InputError):
            self.report()

    def test_duplicate_and_mismatched_ids_rejected(self):
        bad = copy.deepcopy(self.bundle)
        bad["posts"].append(bad["posts"][0])
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, bad)
        bad = copy.deepcopy(self.bundle)
        bad["posts"][0]["url"] = "https://www.douyin.com/video/7600000000000000099"
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, bad)
        bad = copy.deepcopy(self.bundle)
        bad["posts"][0]["sec_uid"] = "other-account"
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, bad)

    def test_existing_post_cannot_move_to_another_account(self):
        other = copy.deepcopy(self.bundle)
        other["account"]["sec_uid"] = "another-valid-identity"
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, other)
        with self.assertRaises(InputError):
            self.store.get(self.scope, "account", "another-valid-identity")

    def test_quarantined_input_rejected(self):
        bad = copy.deepcopy(self.bundle)
        bad["posts"][0]["usable_for_codex"] = False
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, bad)

    def test_other_direction_requires_actual_positioning(self):
        with self.assertRaises(InputError):
            self.store.create_profile({"name": "其他", "direction": "其他定位", "positioning": "其他"})
        self.store.create_profile({"name":"科技账号", "direction":"其他定位", "positioning":"讲工具实际用途", "niche":"软件", "host":"创作者", "audience":"运营者", "pillars":"工具演示", "monetization":"服务"})

    def test_demo_ids_cannot_be_imported_into_real_space(self):
        demo = json.loads((ROOT / "examples/demo.json").read_text(encoding="utf-8"))
        with self.assertRaises(InputError):
            self.store.import_bundle(self.scope, demo)

    def test_records_persist_after_reopening(self):
        another = Store(self.temp.name)
        self.assertEqual(len(another.records(self.scope, "post")), 2)

    def test_concurrent_imports_preserve_all_records(self):
        failures = []
        def worker(n):
            try:
                data = fixture()
                data["posts"] = [{"aweme_id": str(7600000000000000100 + n), "title": str(n), "transcript": "有效文案"}]
                self.store.import_bundle(self.scope, data)
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(self.store.records(self.scope, "post")), 7)

    def test_connector_mismatch_does_not_write(self):
        directory = Path(self.temp.name)
        (directory / "connector.json").write_text(json.dumps({"account_url":"https://example.com/accounts/{sec_uid}"}))
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,size):return json.dumps({"account":{"sec_uid":"wrong"},"posts":[]}).encode()
        with patch("linsi.connector.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaises(InputError):
                fetch_account(directory, self.bundle["account"]["sec_uid"])
        self.assertEqual(len(self.store.records(self.scope, "post")), 2)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="linsi-http-test-")
        cls.env = patch.dict(os.environ, {"KNOWLEDGE_VAULT_DIR": str(Path(cls.temp.name) / "unused-vault"), "LINSI_DATA_DIR": cls.temp.name})
        cls.env.start()
        cls.server = Server(0, cls.temp.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.env.stop()
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=headers or {})
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=5) as response:
                content = response.read()
                return response.status, content, response.headers
        except HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def post(self, path, data):
        status, content, _ = self.request(path, data, {"X-Linsi-Token": self.server.token})
        return status, json.loads(content)

    def test_host_and_csrf_and_cross_origin(self):
        self.assertEqual(self.request("/api/state", headers={"Host":"evil.example"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Origin":"https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/demo", {})[0], 403)
        self.assertEqual(self.request("/api/demo", {}, {"X-Linsi-Token":self.server.token,"Origin":"https://evil.example"})[0], 403)

    def test_static_allowlist_and_csp(self):
        code, page, headers = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("灵思".encode(), page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/.local/linsi.sqlite3", "/../brand.json", "/static/../app.py"):
            self.assertEqual(self.request(path)[0], 404)

    def test_public_engine_media_supports_playback_ranges(self):
        expected=(ROOT / "static/auto-edit-demo.mp4").read_bytes()
        code, data, headers=self.request("/auto-edit-demo.mp4", headers={"Range":"bytes=0-63"})
        self.assertEqual(code,206)
        self.assertEqual(data,expected[:64])
        self.assertEqual(headers["Content-Type"],"video/mp4")
        self.assertEqual(headers["Content-Range"],f"bytes 0-63/{len(expected)}")
        self.assertEqual(self.request("/auto-edit-demo.mp4",headers={"Range":f"bytes={len(expected)}-"})[0],416)
        code, poster, headers=self.request("/auto-edit-demo.webp")
        self.assertEqual(code,200)
        self.assertEqual(poster,(ROOT / "static/auto-edit-demo.webp").read_bytes())
        self.assertTrue(headers["Content-Type"].startswith("image/webp"))

    def test_full_report_handoff_and_cli_roundtrip(self):
        _, profile = self.post("/api/profiles", {"name":"HTTP空间", "direction":"男性成长", "positioning":"只做测试"})
        scope = profile["service_account_key"]
        code, _ = self.post("/api/import", {"service_account_key":scope,"bundle":fixture()})
        self.assertEqual(code, 201)
        code, report = self.post("/api/reports/create", {"service_account_key":scope,"sec_uid":fixture()["account"]["sec_uid"],"aweme_ids":["7600000000000000001"]})
        self.assertEqual(code, 201)
        directory = Path(self.temp.name) / "research-sources" / report["id"]
        self.assertTrue((directory / "sources.json").is_file())
        markdown_path = directory / "finished.md"
        markdown_path.write_text("# 测试报告\n来源 7600000000000000001\n这是一项回写测试，不是真实研究。", encoding="utf-8")
        self.assertEqual(publish(directory / "sources.json", markdown_path, self.base), report["id"])
        query = urlencode({"service_account_key":scope,"id":report["id"]})
        code, content, _ = self.request("/api/report?"+query)
        self.assertEqual(json.loads(content)["status"], "completed")
        code, content, headers = self.request("/api/report/export?"+query)
        self.assertEqual(content.decode(), markdown_path.read_text(encoding="utf-8"))
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_demo_is_idempotent(self):
        _, one = self.post("/api/demo", {})
        _, two = self.post("/api/demo", {})
        self.assertEqual(one["service_account_key"], two["service_account_key"])
        self.assertTrue(one["demo"])

    def test_brand_is_read_only_and_ignores_legacy_local_override(self):
        before = hashlib.sha256((ROOT / "brand.json").read_bytes()).hexdigest()
        code, _ = self.post("/api/brand", {"wechat":"test-only", "website":"https://example.com/", "trial_message":"测试本地设置"})
        self.assertEqual(code, 403)
        self.assertEqual(hashlib.sha256((ROOT / "brand.json").read_bytes()).hexdigest(), before)
        self.assertFalse((Path(self.temp.name) / "brand.json").exists())
        legacy = Path(self.temp.name) / "brand.json"
        legacy.write_text(json.dumps({"wechat":"legacy-override", "website":"https://example.com/"}), encoding="utf-8")
        expected = json.loads((ROOT / "brand.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(self.server.brand(), expected)
        code, content, _ = self.request("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(content)["brand"], expected)
        self.assertEqual(json.loads(legacy.read_text())["wechat"], "legacy-override")
        code, _ = self.post("/api/brand", {"wechat":"test-only", "website":"javascript:alert(1)", "trial_message":"拒绝危险链接"})
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
