"""Transactional local records; every business record belongs to a research space."""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DIRECTIONS = ("县城商业IP", "男性成长", "情感提升", "其他定位")


class InputError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value, name, maximum=500, required=False):
    if not isinstance(value, str):
        raise InputError(f"{name}需要是文字")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise InputError(f"请填写{name}，最多 {maximum} 字")
    return value


def web_url(value, kind=None):
    value = text(value, "链接", 2000)
    if not value:
        return ""
    p = urlparse(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise InputError("链接必须为不含凭据的 HTTPS 地址")
    if kind and p.hostname not in ("www.douyin.com", "www.iesdouyin.com"):
        raise InputError("请使用抖音完整主页或作品链接；短链接请先在浏览器打开后复制完整地址")
    return value


def stable_id(value, name, demo=False):
    value = text(value, name, 160, True)
    pattern = r"demo:[a-zA-Z0-9_-]+" if demo else (r"\d{10,30}" if name == "aweme_id" else r"[a-zA-Z0-9_=-]{6,160}")
    if not re.fullmatch(pattern, value):
        raise InputError(f"{name}格式不正确，请使用来源中的稳定 ID")
    return value


def counter(value, name):
    if type(value) is not int or value < 0 or value > 10**12:
        raise InputError(f"{name}必须是非负整数；未知请留空")
    return value


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "linsi.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    scope TEXT NOT NULL REFERENCES profiles(key), kind TEXT NOT NULL,
                    id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scope, kind, id));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def profiles(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM profiles ORDER BY rowid")]

    def profile(self, key):
        if not isinstance(key, str) or not key:
            raise InputError("请先选择研究空间")
        with self.connect() as db:
            row = db.execute("SELECT data FROM profiles WHERE key=?", (key,)).fetchone()
        if not row:
            raise InputError("研究空间不存在，请重新选择")
        return json.loads(row[0])

    def create_profile(self, body, demo=False):
        name = text(body.get("name", ""), "空间名称", 60, True)
        direction = body.get("direction")
        if direction not in DIRECTIONS:
            raise InputError("请选择内容方向")
        positioning = text(body.get("positioning", ""), "研究目标与账号定位", 2000, True)
        if direction == "其他定位":
            for field in ("niche", "host", "audience", "pillars", "monetization"):
                text(body.get(field, ""), {"niche":"具体赛道", "host":"主理人", "audience":"目标用户", "pillars":"内容支柱", "monetization":"变现路径"}[field], 500, True)
        data = {"service_account_key": uuid.uuid4().hex, "name": name, "direction": direction,
                "positioning": positioning, "demo": demo, "created_at": now()}
        for k in ("niche", "host", "audience", "pillars", "monetization"):
            data[k] = text(body.get(k, ""), k, 500)
        with self.connect() as db:
            db.execute("INSERT INTO profiles VALUES (?,?)", (data["service_account_key"], json.dumps(data, ensure_ascii=False)))
        return data

    def workspace(self, name="", reuse=False):
        name = text(name, "工作台名称", 60) or "我的工作台"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            profiles = [json.loads(row[0]) for row in db.execute("SELECT data FROM profiles")]
            real = [p for p in profiles if not p["demo"]]
            if reuse and len(real) > 1:
                raise InputError("请选择要继续使用的工作台")
            if reuse and real:
                return real[0]
            data = {"service_account_key":uuid.uuid4().hex,"name":name,"direction":"",
                    "positioning":"","demo":False,"created_at":now(),"positioning_status":"unconfigured"}
            db.execute("INSERT INTO profiles VALUES (?,?)", (data["service_account_key"],json.dumps(data,ensure_ascii=False)))
        return data

    def update_profile(self, scope, body):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM profiles WHERE key=?", (scope,)).fetchone()
            if not row:raise InputError("工作台不存在，请重新选择")
            data = json.loads(row[0])
            data["name"] = text(body.get("name", data["name"]), "工作台名称", 60, True)
            direction = body.get("direction", data.get("direction", ""))
            if direction and direction not in DIRECTIONS:raise InputError("请选择有效的内容方向")
            data["direction"] = direction
            data["positioning"] = text(body.get("positioning", data.get("positioning", "")), "研究目标", 2000)
            for field in ("niche", "host", "audience", "pillars", "monetization"):
                data[field] = text(body.get(field, data.get(field, "")), field, 500, direction == "其他定位")
            data["positioning_status"] = "configured" if data["positioning"] else "unconfigured"
            db.execute("UPDATE profiles SET data=? WHERE key=?", (json.dumps(data,ensure_ascii=False),scope))
        return data

    def records(self, scope, kind):
        self.profile(scope)
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE scope=? AND kind=? ORDER BY rowid DESC", (scope, kind))]

    def get(self, scope, kind, identity, db=None):
        if db is None:
            self.profile(scope)
            with self.connect() as connection:
                return self.get(scope, kind, identity, connection)
        row = db.execute("SELECT data FROM records WHERE scope=? AND kind=? AND id=?", (scope, kind, identity)).fetchone()
        if row is None:
            raise InputError("记录不存在或不属于当前研究空间")
        return json.loads(row[0])

    def put(self, db, scope, kind, identity, data):
        db.execute("INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(scope,kind,id) DO UPDATE SET data=excluded.data",
                   (scope, kind, identity, json.dumps(data, ensure_ascii=False)))

    def merge(self, scope, kind, identity, fields):
        self.profile(scope)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(scope, kind, identity, db)
            except InputError:
                old = {}
            data = {**old, **fields, "updated_at": now()}
            self.put(db, scope, kind, identity, data)
        return data

    def bound_account(self, scope):
        try:
            return self.get(scope, "binding", "my-account")
        except InputError:
            return None

    def account_data(self, raw, demo=False):
        if not isinstance(raw, dict):
            raise InputError("账号资料格式不正确")
        identity = stable_id(raw.get("sec_uid", ""), "sec_uid", demo)
        url = web_url(raw.get("url", ""), "account")
        if url and urlparse(url).path.rstrip("/") != f"/user/{identity}":
            raise InputError("主页链接与 sec_uid 不一致")
        data = {"sec_uid": identity, "name": text(raw.get("name", ""), "账号昵称", 100, True),
                "url": url or ("" if demo else f"https://www.douyin.com/user/{identity}"),
                "bio": text(raw.get("bio", ""), "账号简介", 3000),
                "demo": demo, "updated_at": now()}
        for key in ("followers", "following", "total_favorited", "work_count"):
            if key in raw:
                data[key] = None if raw[key] is None else counter(raw[key], key)
        return data

    def bind_my_account(self, scope, identity):
        if self.profile(scope)["demo"]:raise InputError("演示工作台不能关联真实账号")
        account = self.account_data(identity)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM records WHERE scope=? AND kind='binding' AND id='my-account'", (scope,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and previous["sec_uid"] != account["sec_uid"]:
                raise InputError("登录账号与当前工作台的账号不同，请切换登录账号或使用另一个工作台")
            row = db.execute("SELECT data FROM records WHERE scope=? AND kind='account' AND id=?", (scope,account["sec_uid"])).fetchone()
            old = json.loads(row[0]) if row else {}
            self.put(db,scope,"account",account["sec_uid"],{**old,**account})
            binding = {"sec_uid":account["sec_uid"],"name":account["name"],"updated_at":now()}
            self.put(db,scope,"binding","my-account",binding)
        return binding

    def post_data(self, raw, sec_uid, demo=False):
        if not isinstance(raw, dict):
            raise InputError("作品资料格式不正确")
        identity = stable_id(raw.get("aweme_id", ""), "aweme_id", demo)
        if raw.get("sec_uid", sec_uid) != sec_uid:
            raise InputError("作品的 sec_uid 与所选账号不一致")
        if raw.get("usable_for_codex") is False or raw.get("quarantined") or raw.get("rejected"):
            raise InputError("不能导入已隔离、已拒绝或不可用于研究的素材")
        url = web_url(raw.get("url", ""), "post")
        if url and urlparse(url).path.rstrip("/") not in (f"/video/{identity}", f"/share/video/{identity}"):
            raise InputError("作品链接与 aweme_id 不一致")
        published = text(raw.get("published_at", ""), "发布日期", 40)
        if published:
            try:
                datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                raise InputError("发布日期请使用 YYYY-MM-DD 或 ISO 时间") from None
        data = {"aweme_id": identity, "sec_uid": sec_uid,
                "title": text(raw.get("title", ""), "作品标题", 1000, True),
                "url": url or ("" if demo else f"https://www.douyin.com/video/{identity}"),
                "published_at": published, "transcript": text(raw.get("transcript", ""), "文案", 100000),
                "notes": text(raw.get("notes", ""), "研究笔记", 5000), "demo": demo, "updated_at": now()}
        for key in ("likes", "comments", "collects", "shares"):
            data[key] = None if raw.get(key) in (None, "") else counter(raw[key], key)
        data["transcript_status"] = "text_available" if data["transcript"] else "missing"
        if raw.get("media_type") in ("video","gallery"):data["media_type"]=raw["media_type"]
        return data

    def import_bundle(self, scope, bundle):
        profile = self.profile(scope)
        if not isinstance(bundle, dict) or not isinstance(bundle.get("posts", []), list):
            raise InputError("导入文件需要包含 account 对象和 posts 数组")
        if len(bundle.get("posts", [])) > 2000:
            raise InputError("单次最多导入 2000 条作品")
        account = self.account_data(bundle.get("account"), profile["demo"])
        posts = [self.post_data(p, account["sec_uid"], profile["demo"]) for p in bundle.get("posts", [])]
        if len({p["aweme_id"] for p in posts}) != len(posts):
            raise InputError("导入文件包含重复的 aweme_id，请先合并")
        with self.connect() as db:
            try:
                previous = self.get(scope, "account", account["sec_uid"], db)
            except InputError:
                previous = {}
            self.put(db, scope, "account", account["sec_uid"], {**previous, **account})
            for raw, post in zip(bundle.get("posts", []), posts):
                try:
                    previous = self.get(scope, "post", post["aweme_id"], db)
                    if previous["sec_uid"] != account["sec_uid"]:
                        raise InputError("同一作品 ID 已归属其他对标账号")
                except InputError as e:
                    if "已归属" in str(e):
                        raise
                    previous = {}
                merged = {**previous, **post}
                # A partial sync must preserve transcript, notes and unknown old fields.
                for key in ("transcript", "notes", "published_at", "likes", "comments", "collects", "shares"):
                    if key not in raw and key in previous:
                        merged[key] = previous[key]
                merged["transcript_status"] = "text_available" if merged["transcript"] else "missing"
                self.put(db, scope, "post", post["aweme_id"], merged)
        return {"account": account, "imported": len(posts)}

    def save_transcript(self, scope, body):
        identity = body.get("aweme_id")
        with self.connect() as db:
            data = self.get(scope, "post", identity, db)
            data["transcript"] = text(body.get("transcript", ""), "文案", 100000)
            data["notes"] = text(body.get("notes", data.get("notes", "")), "笔记", 5000)
            data["transcript_status"] = "text_available" if data["transcript"] else "missing"
            data["updated_at"] = now()
            self.put(db, scope, "post", identity, data)
        return data

    def create_report(self, scope, body, identity=None):
        profile = self.profile(scope)
        account = self.get(scope, "account", body.get("sec_uid"))
        ids = body.get("aweme_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 30 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise InputError("请选择 1～30 条不重复的代表作品")
        sources = [self.get(scope, "post", identity) for identity in ids]
        if any(p["sec_uid"] != account["sec_uid"] for p in sources):
            raise InputError("一份报告的作品必须来自同一对标账号")
        if any(not p["transcript"].strip() for p in sources):
            raise InputError("所选作品有空文案，请补充或取消选择后再研究")
        snapshot = {"profile": profile, "account": account, "sources": sources}
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        from .reports import draft_markdown
        report_type = body.get("report_type", "account")
        if report_type not in ("account", "breakdown") or (report_type == "breakdown" and len(sources) != 1):
            raise InputError("单条拆解需只选择一条有文案的作品")
        report = {"id": identity or uuid.uuid4().hex, "service_account_key": scope, "sec_uid": account["sec_uid"], "report_type": report_type,
                  "account_name": account["name"], "title": f"{account['name']} · 对标研究",
                  "created_at": now(), "status": "awaiting_ai", "phase":"waiting", "source_digest": digest,
                  "snapshot": snapshot, "markdown": draft_markdown(snapshot), "demo": profile["demo"]}
        if report_type == "breakdown":
            report["title"] = f"{sources[0]['title']} · 单条拆解"
        with self.connect() as db:
            self.put(db, scope, "report", report["id"], report)
        return report

    def report_progress(self, scope, body):
        phase=body.get("phase")
        labels={"reading":"正在核对来源与阅读文案","analyzing":"正在分析内容结构与原文依据","writing":"正在整理拆解结论与报告","failed":"分析已暂停，请检查 AI 接口配置后重试"}
        if phase not in labels:raise InputError("研究阶段不正确")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            report=self.get(scope,"report",body.get("id"),db)
            if body.get("source_digest")!=report["source_digest"]:raise InputError("来源摘要不匹配")
            if report["status"]=="completed":raise InputError("报告已经完成，无需更新分析进度")
            report.setdefault("started_at",now())
            report.update(phase=phase,status="paused" if phase=="failed" else "analyzing",progress_message=labels[phase],updated_at=now())
            self.put(db,scope,"report",report["id"],report)
        return {k:v for k,v in report.items() if k not in ("snapshot","markdown")}

    def save_report(self, scope, body):
        with self.connect() as db:
            report = self.get(scope, "report", body.get("id"), db)
            if body.get("source_digest") != report["source_digest"]:
                raise InputError("来源摘要不匹配，请使用此报告自己的来源快照")
            content = text(body.get("markdown", ""), "研究报告", 200000, True)
            missing = [p["aweme_id"] for p in report["snapshot"]["sources"] if p["aweme_id"] not in content]
            if missing:
                raise InputError("报告需要保留全部所选作品的来源 ID：" + "、".join(missing[:5]))
            report.update(markdown=content, status="completed", phase="completed", updated_at=now(),finished_at=now())
            self.put(db, scope, "report", report["id"], report)
        return report
