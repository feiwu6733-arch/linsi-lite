"""Local Douyin browsing. Observe pages the user opens; no private signing engine."""
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qs

from .store import InputError, stable_id


def source_link(value):
    urls = re.findall(r"https://[^\s<>\"]+", str(value))
    if not urls:
        raise InputError("请粘贴抖音主页、作品或分享链接")
    url = urls[0].rstrip("，。；！、)）")
    p = urlparse(url)
    if p.hostname not in ("www.douyin.com", "v.douyin.com", "www.iesdouyin.com", "iesdouyin.com") or p.username or p.password or p.port not in (None, 443):
        raise InputError("只接受抖音官网和官方分享链接")
    if p.hostname != "v.douyin.com" and not re.match(r"^/(user/[^/]+|video/\d+|share/video/\d+)", p.path):
        raise InputError("请使用账号主页或作品链接")
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def value_count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def first_url(value):
    if isinstance(value, dict):
        value = value.get("url_list", value.get("urlList",value.get("url", value.get("src",[]))))
    if isinstance(value, list):
        for item in value:
            candidate=first_url(item)
            if candidate:return candidate
        return ""
    if isinstance(value,str) and value.startswith("http://"):value="https://"+value[7:]
    return value if isinstance(value, str) and value.startswith("https://") else ""


def user_record(raw):
    if not isinstance(raw, dict) or not raw.get("sec_uid") or not raw.get("nickname"):
        return None
    try:
        identity = stable_id(raw["sec_uid"], "sec_uid")
    except InputError:
        return None
    return {"sec_uid": identity, "name": raw["nickname"], "bio": raw.get("signature") or "",
            "url": f"https://www.douyin.com/user/{identity}", "followers":value_count(raw.get("follower_count")),
            "following":value_count(raw.get("following_count")), "total_favorited":value_count(raw.get("total_favorited")),
            "work_count":value_count(raw.get("aweme_count"))}


def post_record(raw):
    if not isinstance(raw, dict) or not re.fullmatch(r"\d{10,30}", str(raw.get("aweme_id", ""))):
        return None
    author = user_record(raw.get("author"))
    if not author:
        return None
    identity = str(raw["aweme_id"])
    stats = raw.get("statistics") or {}
    published = ""
    try:
        published = datetime.fromtimestamp(raw["create_time"], timezone.utc).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        pass
    # Description is a title/caption, never represented as speech transcription.
    return {"aweme_id":identity,"sec_uid":author["sec_uid"],"title":raw.get("desc") or f"作品 {identity}",
            "url":f"https://www.douyin.com/video/{identity}","published_at":published,
            "likes":value_count(stats.get("digg_count")),"comments":value_count(stats.get("comment_count")),
            "collects":value_count(stats.get("collect_count")),"shares":value_count(stats.get("share_count")),
            "media_type":"gallery" if raw.get("images") else "video"}


def media_url(raw):
    video = raw.get("video") or {}
    return first_url(video.get("play_addr")) or first_url(video.get("download_addr"))


def comment_record(raw, aweme_id):
    if not isinstance(raw,dict) or not re.fullmatch(r"\d{10,30}",str(raw.get("cid",""))):return None
    if raw.get("aweme_id") and str(raw["aweme_id"])!=aweme_id:return None
    content=raw.get("text")
    if not isinstance(content,str) or not content.strip():return None
    user=raw.get("user") or {}
    parent=str(raw.get("reply_id") or "")
    if parent in ("0","None","null"):parent=""
    return {"comment_id":str(raw["cid"]),"aweme_id":aweme_id,"text":content[:8000],
            "nickname":str(user.get("nickname") or "抖音用户")[:100],"likes":value_count(raw.get("digg_count")),
            "reply_count":value_count(raw.get("reply_comment_total")),"parent_comment_id":parent,
            "created_at":raw.get("create_time") if type(raw.get("create_time")) is int else None}


class DouyinSession:
    def __init__(self, scope, directory):
        self.scope = scope
        self.directory = Path(directory)
        self.profile_path = self.directory / "browser-profiles" / hashlib.sha256(scope.encode()).hexdigest()[:24]
        self.context = None
        self.runtime = None
        self.page = None
        self.identity = None
        self.accounts = {}
        self.posts = {}
        self.media = {}  # Expiring media URLs exist in memory only, never in records/exports/logs.
        self.images = {}
        self.last_response = 0
        self.status = "disconnected"
        self.message = "尚未连接抖音"
        self.read_state = "not_started"
        self.has_more = None
        self.target = None
        self.comment_target = None
        self.comments = {}
        self.comment_seen = False
        self.comment_more = None

    def public_status(self):
        return {"status":self.status,"message":self.message,"identity":self.identity,"read_state":self.read_state,
                "browser_open": self.context is not None,"has_more":self.has_more}

    def open(self):
        if self.context is not None and self.page and not self.page.is_closed():
            return
        self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise InputError("浏览器组件未安装，请双击「安装采集组件.cmd」后重试") from None
        self.runtime = sync_playwright().start()
        self.profile_path.mkdir(parents=True, exist_ok=True)
        candidates = ["msedge", "chrome", None]
        for channel in candidates:
            try:
                options = {"user_data_dir":str(self.profile_path),"headless":False,"locale":"zh-CN",
                           "viewport":{"width":1280,"height":820},"timeout":20000,"accept_downloads":False}
                if channel:
                    options["channel"] = channel
                self.context = self.runtime.chromium.launch_persistent_context(**options)
                break
            except Exception:
                self.context = None
        if self.context is None:
            self.close()
            raise InputError("无法启动专用浏览器。请安装 Edge/Chrome，或关闭正在使用此工具配置的浏览器后重试")
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.context.on("response", self.capture)
        self.status = "waiting_login"
        self.message = "请在打开的抖音窗口扫码，登录后自动关联并同步作品"

    def capture(self, response):
        parsed = urlparse(response.url)
        path = parsed.path
        if parsed.hostname=="www.douyin.com" and "/comment/list/" in path:
            try:
                identity=parse_qs(parsed.query).get("aweme_id",[None])[0]
                if not identity or identity!=self.comment_target:return
                raw=response.json()
                if raw.get("status_code",0)!=0:return
                items=raw.get("comments")
                if not isinstance(items,list):return
                self.comment_seen=True
                self.comment_more=bool(raw["has_more"]) if "has_more" in raw else None
                for item in items:
                    row=comment_record(item,identity)
                    if row:self.comments[row["comment_id"]]=row
            except Exception:pass
            return
        if parsed.hostname != "www.douyin.com" or not any(key in path for key in ("/user/profile/self/","/user/profile/other/","/aweme/post/","/aweme/detail/")):
            return
        try:
            raw = response.json()
            if not isinstance(raw, dict):
                return
            if raw.get("status_code", 0) != 0:
                if "/user/profile/self/" in path:
                    self.identity = None
                    self.status = "waiting_login"
                self.read_state = "restricted"
                return
            self.last_response = time.monotonic()
            user = user_record(raw.get("user"))
            if user:
                self.accounts[user["sec_uid"]] = user
                avatar = first_url((raw["user"].get("avatar_larger") or raw["user"].get("avatar_thumb")))
                if avatar:self.images[("account",user["sec_uid"])] = avatar
                if "/user/profile/self/" in path:
                    self.identity = user
                    self.status = "connected"
                    self.message = "已识别本地登录账号；登录信息仅保存在本机"
            items = raw.get("aweme_list", [])
            if isinstance(raw.get("aweme_detail"), dict):
                items = [raw["aweme_detail"]]
            if not isinstance(items, list):return
            for item in items:
                post = post_record(item)
                if not post:continue
                author = user_record(item["author"])
                self.accounts.setdefault(author["sec_uid"], author)
                self.posts[post["aweme_id"]] = post
                media = media_url(item)
                if media:self.media[post["aweme_id"]] = media
                video=item.get("video") or {}
                cover = first_url(video.get("cover")) or first_url(video.get("origin_cover")) or first_url(video.get("dynamic_cover"))
                if cover:self.images[("post",post["aweme_id"])] = cover
                avatar=first_url(item["author"].get("avatar_larger")) or first_url(item["author"].get("avatar_thumb"))
                if avatar:self.images.setdefault(("account",author["sec_uid"]),avatar)
            if "/aweme/post/" in path:
                self.read_state = "readable" if items or raw.get("has_more") in (0, False) else "restricted"
                self.has_more = bool(raw["has_more"]) if "has_more" in raw else None
        except Exception:
            # Unrelated malformed responses never expose URLs or authentication details.
            pass

    def pump(self):
        if self.page and not self.page.is_closed():
            self.page.wait_for_timeout(120)
        elif self.context:
            self.close()

    def navigate(self, url):
        self.open()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=35000)
            for _ in range(12):
                self.page.wait_for_timeout(350)
        except Exception:
            raise InputError("抖音页面尚未就绪，请在专用浏览器检查网络或完成验证后重试") from None
        if urlparse(self.page.url).hostname not in ("www.douyin.com","v.douyin.com","www.iesdouyin.com","iesdouyin.com"):
            raise InputError("链接跳转到了非抖音页面，已停止采集")

    def login(self):
        self.identity = None
        self.navigate("https://www.douyin.com/user/self")
        if not self.identity:
            self.status = "waiting_login"
            self.message = "浏览器已打开，请扫码；成功后会自动同步作品"
        return self.public_status()

    def verify(self):
        self.identity = None
        self.status = "checking"
        self.navigate("https://www.douyin.com/user/self")
        if not self.identity:
            self.status = "waiting_login"
            self.message = "正在等待登录身份，请在抖音窗口完成扫码或验证"
        return self.public_status()

    def collect(self, link, more=False):
        link = source_link(link)
        previous = set(self.posts)
        started = time.monotonic()
        self.read_state = "reading"
        if not more or not self.page or self.page.is_closed() or self.target != link:
            self.posts = {}
            self.has_more = None
            self.navigate(link)
            previous = set()
            self.target = link
        identity = None
        parsed = urlparse(self.page.url)
        match = re.match(r"^/user/([^/]+)", parsed.path)
        if match and match[1] != "self":identity = match[1]
        video = re.search(r"/(?:share/)?video/(\d+)", parsed.path)
        for _ in range(10):
            if video and video[1] in self.posts:
                identity = self.posts[video[1]]["sec_uid"]
                break
            related = [p for p in self.posts.values() if p["sec_uid"] == identity]
            added = [p for p in related if p["aweme_id"] not in previous]
            if len(added) >= 20 or (related and self.has_more is False):break
            # Scroll the page the user explicitly asked to collect; never fabricate signatures.
            self.page.mouse.wheel(0, 1100)
            self.page.wait_for_timeout(650)
        if identity is None or identity not in self.accounts:
            self.read_state = "restricted"
            raise InputError("未取得可核验的账号资料。请在抖音窗口完成登录/验证，或改用完整主页链接重试")
        posts = [p for p in self.posts.values() if p["sec_uid"] == identity]
        if video:
            posts = [p for p in posts if p["aweme_id"] == video[1]]
        if not posts and not (self.read_state == "readable" and self.has_more is False and self.last_response >= started):
            self.read_state = "restricted"
            raise InputError("账号已识别，但作品未成功读取。请检查抖音窗口，完成验证后再试；未用旧数据伪装成功")
        if not posts and (self.accounts[identity].get("work_count") or 0)>0:
            self.read_state="restricted"
            raise InputError("账号显示有作品，但本次未读取到作品列表。请检查页面权限或验证状态后重试")
        if more and not any(p["aweme_id"] not in previous for p in posts) and self.has_more is not False:
            raise InputError("本次没有取得新作品；可能需要在抖音窗口验证或稍后重试")
        self.read_state = "readable"
        return {"account": self.accounts[identity], "posts":posts}

    def media_for(self, post):
        cached=self.media.get(post["aweme_id"])
        self.navigate(post["url"])
        rendered=self.rendered_content(post["aweme_id"])
        if rendered.get("gallery") or urlparse(self.page.url).path.startswith('/note/'):
            raise InputError("这是一条图文作品，请查看图文预览；不能当作视频下载或语音转写")
        if rendered.get("sec_uid") and rendered["sec_uid"]!=post["sec_uid"]:raise InputError("视频作者与当前账号不一致")
        current = self.posts.get(post["aweme_id"])
        if current and current["sec_uid"] != post["sec_uid"]:
            raise InputError("视频作者与当前对标不一致，已停止")
        url = self.media.get(post["aweme_id"]) or first_url(rendered.get("media")) or cached
        if not url:
            self.collect('https://www.douyin.com/user/'+post['sec_uid'])
            url=self.media.get(post['aweme_id'])
        if not url:
            raise InputError("未取得可下载视频。请在抖音窗口确认作品可播放后重试；图文作品暂不转写")
        return url

    def rendered_content(self, identity):
        # Read only the displayed work and public comments from its rendered component.
        # Account credentials and unrelated application props never leave the page.
        return self.page.evaluate("""id => {
          const out={comments:[],media:[],seen:false,has_more:null,gallery:false};
          const urls=v=>{if(typeof v==='string')return /^https?:/.test(v)?[v]:[];if(Array.isArray(v))return v.flatMap(urls);if(v&&typeof v==='object')return ['urlList','url_list','url','src'].flatMap(k=>urls(v[k]));return [];};
          const add=c=>{if(c&&c.cid&&typeof c.text==='string')out.comments.push({cid:c.cid,text:c.text,user:{nickname:c.user?.nickname},digg_count:c.diggCount??c.digg_count,reply_comment_total:c.replyTotal??c.reply_comment_total,reply_id:c.replyId??c.reply_id,create_time:c.createTime??c.create_time,aweme_id:id});};
          for(const el of [...document.querySelectorAll('[data-e2e="comment-item"],[data-e2e="feed-active-video"],[data-e2e="note-detail"],[data-e2e="comment-list"]')].slice(0,120)){
            let f=el[Object.keys(el).find(k=>k.startsWith('__reactFiber$'))];
            for(let n=0;f&&n<12;n++,f=f.return){
              const p=f.memoizedProps||{},a=p.awemeInfo||p.aweme?.detail;
              if(!a||String(a.awemeId)!==id)continue;
              out.sec_uid=a.authorInfo?.secUid;
              const v=a.video||{};out.media.push(...urls(v.playAddr),...urls(v.play_addr),...urls(v.playAddrH264),...urls(v.downloadAddr));
              out.gallery=out.gallery||!!(a.images?.length||a.imageInfos?.length);
              if(p.commentInfo){add(p.commentInfo);out.seen=true;}
              const initial=p.defaultComment||p.comment;
              if(initial?.statusCode===0&&Array.isArray(initial.comments)){initial.comments.forEach(add);out.seen=true;out.has_more=initial.hasMore;}
              if(Array.isArray(p.defaultCommentList)){p.defaultCommentList.forEach(add);out.seen=true;out.has_more=p.defaultHasMore;}
            }
          }return out;
        }""",identity)

    def return_to_workbench(self, base_url):
        page = getattr(self,"workbench_page",None)
        if page is None or page.is_closed():
            page = self.context.new_page()
            self.workbench_page = page
        page.goto(base_url+"/?workspace="+self.scope+"#mine",wait_until="domcontentloaded",timeout=10000)
        page.bring_to_front()

    def read_image(self, url):
        from .media import validate_url
        validate_url(url)
        # Use the same local browser networking and referer as the page.
        response=self.context.request.get(url,headers={"Referer":"https://www.douyin.com/"},timeout=15000,max_redirects=0)
        try:
            if not response.ok:raise InputError(f"图片响应 HTTP {response.status}")
            mime=response.headers.get("content-type","").split(";")[0]
            if mime not in ("image/jpeg","image/png","image/webp","image/avif"):raise InputError("图片格式不支持")
            data=response.body()
            if not data or len(data)>3*1024*1024:raise InputError("图片为空或超过 3 MB")
            return data,mime
        finally:response.dispose()

    def collect_comments(self, post, limit=50, check=lambda:None):
        self.comment_target=post["aweme_id"]
        self.comments={};self.comment_seen=False;self.comment_more=None
        try:
            self.navigate(post["url"])
            for _ in range(16):
                check()
                rendered=self.rendered_content(post["aweme_id"])
                if rendered.get("sec_uid") and rendered["sec_uid"]!=post["sec_uid"]:raise InputError("评论页面作者不一致，已停止采集")
                if rendered.get("seen"):
                    if not self.comment_seen:self.comment_more=rendered.get("has_more")
                    self.comment_seen=True
                    for item in rendered["comments"]:
                        row=comment_record(item,post["aweme_id"])
                        if row:self.comments[row["comment_id"]]=row
                if len(self.comments)>=limit or self.comment_seen and self.comment_more is False:break
                self.page.evaluate("""() => {
                  let node=document.querySelector('[data-e2e="comment-item"]') || document.querySelector('[data-e2e="comment-list"]');
                  while(node && node!==document.body){if(node.scrollHeight>node.clientHeight+30 && ['auto','scroll'].includes(getComputedStyle(node).overflowY)){node.scrollTop+=800;return;}node=node.parentElement;}
                }""")
                self.page.wait_for_timeout(700)
            if not self.comment_seen:raise InputError("未读取到评论，请在抖音窗口展开评论区或完成验证后重试")
            return {"comments":list(self.comments.values())[:limit],"has_more":True if len(self.comments)>limit else self.comment_more,"collected_at":datetime.now(timezone.utc).isoformat()}
        finally:self.comment_target=None

    def close(self, clear=False):
        failed = False
        if self.context:
            try:
                if clear:
                    self.context.clear_cookies()
                    page = self.page if self.page and not self.page.is_closed() else self.context.new_page()
                    client = self.context.new_cdp_session(page)
                    client.send("Storage.clearDataForOrigin", {"origin":"https://www.douyin.com","storageTypes":"all"})
            except Exception:
                failed = True
            finally:
                try:
                    self.context.close()
                except Exception:
                    failed = True
                self.context = None
        if self.runtime:
            try:
                self.runtime.stop()
            except Exception:
                failed = True
        self.runtime = None
        self.page = None
        self.identity = None
        self.status = "disconnected"
        self.message = "已断开；可重新打开本地浏览器" if not clear else "已清除本工具的抖音登录状态"
        self.media.clear()
        self.images.clear()
        self.read_state = "not_started"
        if clear and failed:
            self.message = "登录状态未能完全清除，请重新打开专用浏览器后再执行清除"
            raise InputError(self.message)
