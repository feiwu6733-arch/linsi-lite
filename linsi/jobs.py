"""Local task queue. Browser calls stay on one owning thread; database is authoritative."""
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .douyin import DouyinSession, source_link
from .reports import atomic_text
from .store import InputError, now

ACTIVE = ("queued", "running", "cancelling")


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self, store, session_factory=DouyinSession, base_url=None):
        self.store = store
        self.factory = session_factory
        self.queue = queue.Queue()
        self.sessions = {}
        self.states = {}
        self.cancelled = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.model = None
        self.base_url = base_url
        self.login_watch = set()
        for profile in store.profiles():
            for flow in store.records(profile["service_account_key"], "connection_flow"):
                if flow.get("status") in ("running","queued","waiting_login"):
                    store.merge(profile["service_account_key"],"connection_flow","my-account",{"status":"interrupted","message":"服务已重启，点击连接抖音继续同步"})
            for post in store.records(profile["service_account_key"], "post"):
                if post.get("processing_status") in ("downloading", "transcribing"):
                    store.merge(profile["service_account_key"], "post", post["aweme_id"], {"processing_status":"interrupted","processing_error":"服务退出，处理已中断；已完成文件保留，可重新处理"})
            for job in store.records(profile["service_account_key"], "job"):
                if job["status"] in ACTIVE:
                    store.merge(profile["service_account_key"], "job", job["id"], {"status":"interrupted","message":"上次服务退出，任务已暂停。可以重试，已完成文件保留。"})
        self.thread = threading.Thread(target=self.run, name="linsi-local-worker", daemon=True)
        self.thread.start()

    def capabilities(self):
        model_dir = self.store.directory / "models"
        return {"browser_installed":importlib.util.find_spec("playwright") is not None,
                "transcription_installed":importlib.util.find_spec("faster_whisper") is not None,
                "model_ready":self.model is not None or (model_dir/"ready.json").is_file(),
                "model_name":"small · 本地中文转写"}

    def state(self, scope):
        with self.lock:
            result = dict(self.states.get(scope, {"status":"disconnected","message":"点击连接，在本地浏览器登录抖音","identity":None,"browser_open":False,"read_state":"not_started","has_more":None}))
        flows = self.store.records(scope, "connection_flow")
        result["workflow"] = flows[0] if flows else None
        return result

    def public_jobs(self,scope):
        """Present current recovery evidence without rewriting historical task outcomes."""
        jobs=self.store.records(scope,"job")
        posts={p["aweme_id"]:p for p in self.store.records(scope,"post")}
        def local_file(post):
            if not post.get("media_path"):return False
            path=(self.store.directory/post["media_path"]).resolve()
            return path.is_relative_to(self.store.directory) and path.is_file() and path.stat().st_size>0
        result=[]
        for original in jobs[:100]:
            job=dict(original)
            rows=job.get("results",[])
            job["display_status"]="failed" if job["status"]=="partial" and rows and all(r.get("status")=="failed" for r in rows) else job["status"]
            job["needs_attention"]=job["status"] in ("failed","partial","interrupted","cancelled")
            if job["needs_attention"]:
                action=job["action"];payload=job.get("payload",{});ids=payload.get("aweme_ids",[])
                selected=[posts.get(i,{}) for i in ids]
                resolution=None
                if ids and all(selected):
                    if action in ("preview","download") and all(local_file(p) for p in selected):
                        resolution={"kind":"available","message":"视频已在后续处理时保存，现在可以直接播放。","aweme_id":ids[0]}
                    elif action=="preview" and all(p.get("media_type")=="gallery" for p in selected):
                        resolution={"kind":"not_applicable","message":"这是图文作品，使用图文预览即可，无需重试视频下载。","aweme_id":ids[0]}
                    elif action=="transcribe" and all(p.get("transcript","").strip() and (not payload.get("force") or p.get("transcribed_at","")>job.get("created_at","")) for p in selected):
                        resolution={"kind":"available","message":"文案已在后续处理时保存，现在可以阅读。","aweme_id":ids[0]}
                    elif action=="comments":
                        comments=self.store.records(scope,"comment")
                        if all(p.get("comments_status")=="completed" and (p.get("comments_has_more") is False or sum(c.get("aweme_id")==p["aweme_id"] for c in comments)>=payload.get("limit",50)) for p in selected):
                            resolution={"kind":"available","message":"评论已在后续采集时保存，现在可以查看。","aweme_id":ids[0]}
                elif action=="prepare_model" and self.capabilities()["model_ready"]:
                    resolution={"kind":"available","message":"本地转写模型现已准备好，无需重复处理旧任务。"}
                if not resolution and action in ("collect","sync_mine"):
                    newer=next((j for j in jobs if j["status"]=="completed" and j["action"]==action and j.get("payload")==payload and j.get("created_at","")>job.get("created_at","") and j.get("result",{}).get("sec_uid")),None)
                    if newer:resolution={"kind":"available","message":"相同采集任务后续已成功，可查看作品。","sec_uid":newer["result"]["sec_uid"]}
                if resolution:job.update(resolution=resolution,needs_attention=False)
            result.append(job)
        return result

    def flow(self, scope, **fields):
        return self.store.merge(scope,"connection_flow","my-account",fields)

    def after_login(self, scope, session):
        if scope not in self.login_watch:return
        status=session.public_status()
        identity=status.get("identity")
        if status.get("status")=="disconnected" and not status.get("browser_open"):
            self.login_watch.discard(scope)
            self.flow(scope,status="disconnected",message="抖音窗口已关闭，点击连接后继续")
            return
        if status.get("status")!="connected" or not identity:return
        self.login_watch.discard(scope)
        existing=self.store.bound_account(scope)
        if existing and existing["sec_uid"]!=identity["sec_uid"]:
            self.flow(scope,status="account_mismatch",message="登录的是另一个抖音账号，请切换登录账号或使用另一个工作台")
            return
        try:
            job=self.submit(scope,"sync_mine",{"automatic":True})
            self.flow(scope,status="queued",message="登录成功，正在自动关联并同步作品…",job_id=job["id"])
        except InputError as error:
            self.flow(scope,status="failed",message=str(error))

    def publish_state(self, scope, session):
        with self.lock:
            self.states[scope] = session.public_status()

    def submit(self, scope, action, payload=None):
        profile = self.store.profile(scope)
        if profile["demo"]:
            raise InputError("演示空间不连接真实抖音；请先创建自己的研究空间")
        payload = payload or {}
        if action not in ("connect","verify","disconnect","clear","collect","download","transcribe","prepare_model","sync_mine","preview","comments"):
            raise InputError("不支持的任务")
        if action == "collect":
            payload = {"link":source_link(payload.get("link", "")),"more":payload.get("more") is True}
        if action == "sync_mine":payload={"automatic":payload.get("automatic") is True}
        if action in ("download","transcribe","preview","comments"):
            ids = payload.get("aweme_ids")
            if not isinstance(ids,list) or not ids or len(ids)>30 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):
                raise InputError("请选择 1～30 条不重复的作品")
            posts = [self.store.get(scope,"post",i) for i in ids]
            if action in ("download","preview","transcribe") and any(p.get("media_type")=="gallery" for p in posts):
                raise InputError("所选作品包含图文，请点击封面预览图文和采集评论；图文没有视频语音可转写")
            if len({p["sec_uid"] for p in posts}) != 1:
                raise InputError("同一任务只能处理一个对标账号的作品")
            limit=payload.get("limit",50)
            if action=="comments" and (type(limit) is not int or not 1<=limit<=100):raise InputError("每条作品采集 1～100 条评论")
            payload = {"aweme_ids":ids,**({"limit":limit} if action=="comments" else {}),**({"force":True} if action=="transcribe" and payload.get("force") is True else {})}
        with self.lock:
            active = [j for j in self.store.records(scope,"job") if j["status"] in ACTIVE]
            if len(active)>=6:raise InputError("已有多项任务等待处理，请完成或取消后再试")
            if any(j["action"] == action and j.get("payload") == payload for j in active):
                raise InputError("同样的任务已在队列中，请到任务中心查看")
            job = {"id":uuid.uuid4().hex,"service_account_key":scope,"action":action,"payload":payload,
                   "status":"queued","message":"等待本地处理","created_at":now(),"completed":0,"total":len(payload.get("aweme_ids",[])) or 1,"results":[],
                   "progress":{"stage":"queued","label":"排队等待","current":0,"total":None,"unit":"","percent":None}}
            self.store.merge(scope,"job",job["id"],job)
            self.queue.put(job)
        return job

    def cancel(self, scope, identity):
        job=self.store.get(scope,"job",identity)
        if job["status"] not in ACTIVE:return job
        self.cancelled.add(identity)
        return self.store.merge(scope,"job",identity,{"status":"cancelling","message":"正在取消；当前网络操作结束后停止，已完成文件保留"})

    def retry(self,scope,identity):
        job=self.store.get(scope,"job",identity)
        if job["status"] in ACTIVE:raise InputError("任务仍在运行")
        payload=dict(job["payload"])
        successful={r["aweme_id"] for r in job.get("results",[]) if r.get("status") == "completed"}
        if "aweme_ids" in payload:
            pending=[i for i in payload["aweme_ids"] if i not in successful]
            if pending:payload["aweme_ids"]=pending
        return self.submit(scope,job["action"],payload)

    def check(self,job):
        if self.stop.is_set() or job["id"] in self.cancelled:raise Cancelled()

    def update(self,job,**fields):
        if fields.get("status")=="running":fields.setdefault("started_at",now())
        if fields.get("status") in ("completed","failed","partial","cancelled","interrupted"):
            fields.setdefault("finished_at",now())
        if fields.get("status")=="completed":
            fields["progress"]={"stage":"completed","label":"处理完成","current":1,"total":1,"unit":"","percent":100}
        return self.store.merge(job["service_account_key"],"job",job["id"],fields)

    def progress(self,job,stage,label,current=0,total=None,unit="",**fields):
        # Percent describes this measured stage, not a guessed end-to-end duration.
        total=total if isinstance(total,(int,float)) and total>0 else None
        self.update(job,progress={"stage":stage,"label":label,"current":current,"total":total,"unit":unit,
                                  "percent":min(99,int(current/total*100)) if total else None},**fields)

    def session(self,scope):
        if scope not in self.sessions:self.sessions[scope]=self.factory(scope,self.store.directory)
        return self.sessions[scope]

    def run(self):
        while not self.stop.is_set():
            try:
                job=self.queue.get(timeout=.3)
            except queue.Empty:
                for scope,session in list(self.sessions.items()):
                    try:session.pump()
                    except Exception:
                        try:session.close()
                        except Exception:pass
                    self.publish_state(scope,session)
                    try:self.after_login(scope,session)
                    except Exception:self.flow(scope,status="failed",message="自动同步未能开始，请重试连接")
                continue
            try:
                self.check(job)
                self.progress(job,"starting","启动任务",status="running",message="正在启动本地处理…")
                self.execute(job)
            except Cancelled:
                self.update(job,status="cancelled",message="任务已取消，已完成的资料保留")
                if job["action"]=="sync_mine":self.flow(job["service_account_key"],status="cancelled",message="同步已取消，已保存的资料保留")
            except InputError as error:
                self.update(job,status="failed",message=str(error))
                if job["action"]=="sync_mine":self.flow(job["service_account_key"],status="failed",message=str(error))
            except Exception:
                self.update(job,status="failed",message="本地处理未完成，请检查浏览器、网络或组件后重试。原始资料保留。")
                if job["action"]=="sync_mine":self.flow(job["service_account_key"],status="failed",message="作品同步未完成，请检查抖音窗口后重试")
            finally:
                if job["service_account_key"] in self.sessions:
                    self.publish_state(job["service_account_key"],self.sessions[job["service_account_key"]])
                self.queue.task_done()
        for session in self.sessions.values():
            try:session.close()
            except Exception:pass

    def execute(self,job):
        scope,action,payload=job["service_account_key"],job["action"],job["payload"]
        if action in ("connect","verify","disconnect","clear"):
            session=self.session(scope)
            if action in ("connect","verify"):
                self.login_watch.add(scope)
                self.flow(scope,status="waiting_login",message="请扫码登录，完成后会自动同步作品，无需再点检查或关联")
            else:
                self.login_watch.discard(scope)
                self.flow(scope,status="disconnected",message="连接已断开，已保存的作品仍可查看")
            self.update(job,message="正在打开本地抖音浏览器" if action in ("connect","verify") else "正在断开连接")
            if action == "connect":session.login()
            elif action == "verify":session.verify()
            else:
                if action == "clear" and session.context is None:session.open()
                session.close(clear=action=="clear")
            self.update(job,status="completed",completed=1,message=session.message)
            self.after_login(scope,session)
        elif action == "sync_mine":
            session=self.session(scope)
            status=session.public_status()
            identity=status.get("identity")
            if status.get("status")!="connected" or not identity:raise InputError("登录尚未确认，请重新连接抖音后自动同步")
            self.store.bind_my_account(scope,identity)
            self.flow(scope,status="running",message=f"已连接 {identity['name']}，正在同步作品…",job_id=job["id"])
            self.progress(job,"collect","读取我的作品",message="登录成功，正在同步我的作品")
            bundle=session.collect("https://www.douyin.com/user/"+identity["sec_uid"])
            self.check(job)
            current=session.public_status()
            if current.get("status")!="connected" or not current.get("identity") or current["identity"]["sec_uid"]!=identity["sec_uid"] or bundle["account"]["sec_uid"]!=identity["sec_uid"]:
                raise InputError("同步期间登录账号或作品作者发生变化，已停止保存，请重新连接")
            result=self.store.import_bundle(scope,bundle)
            self.store.merge(scope,"account",identity["sec_uid"],{"collected_at":now(),"collection_source":"local_browser","has_more":session.has_more})
            self.cache_images(scope,session,bundle,job)
            self.update(job,status="completed",completed=1,message=f"已自动同步 {result['imported']} 条作品",result={"sec_uid":identity["sec_uid"],"imported":result["imported"]})
            self.flow(scope,status="completed",message=f"已同步 {result['imported']} 条作品",sec_uid=identity["sec_uid"],count=result["imported"])
            if payload.get("automatic") and self.base_url and hasattr(session,"return_to_workbench"):
                try:session.return_to_workbench(self.base_url)
                except Exception:pass
        elif action == "collect":
            session=self.session(scope)
            self.progress(job,"collect","读取账号作品",message="正在浏览抖音账号并读取作品；如出现验证，请在打开的浏览器中完成")
            bundle=session.collect(payload["link"],payload["more"])
            self.check(job)
            result=self.store.import_bundle(scope,bundle)
            sec_uid=result["account"]["sec_uid"]
            self.store.merge(scope,"account",sec_uid,{"collected_at":now(),"collection_source":"local_browser","has_more":session.has_more})
            self.cache_images(scope,session,bundle,job)
            self.update(job,status="completed",completed=1,message=f"已读取并保存 {result['imported']} 条作品",result={"sec_uid":sec_uid,"imported":result["imported"]})
        elif action == "prepare_model":
            self.prepare_model(job)
            self.update(job,status="completed",completed=1,message="本地转写模型已准备好")
        else:
            results=[]
            for index,identity in enumerate(payload["aweme_ids"]):
                self.check(job)
                try:
                    post=self.store.get(scope,"post",identity)
                    self.progress(job,"preparing","准备作品",message=f"正在处理第 {index+1}/{len(payload['aweme_ids'])} 条：{post['title'][:45]}",completed=index,current_title=post['title'],current_aweme_id=identity)
                    if action in ("download","preview"):self.download_post(job,post)
                    elif action=="comments":
                        self.progress(job,"comments","读取公开评论",message="正在读取评论，请在抖音窗口完成可能出现的验证")
                        self.store.merge(scope,"post",identity,{"comments_status":"running","comments_error":""})
                        result=self.session(scope).collect_comments(post,payload["limit"],lambda:self.check(job))
                        self.check(job)
                        for comment in result["comments"]:
                            if comment["aweme_id"]!=identity:raise InputError("评论来源与作品不一致")
                        for comment in result["comments"]:
                            self.store.merge(scope,"comment",identity+":"+comment["comment_id"],comment)
                        self.store.merge(scope,"post",identity,{"comments_status":"completed","comments_collected_at":now(),"comments_has_more":result["has_more"],"comments_error":""})
                    else:self.transcribe_post(job,post)
                    results.append({"aweme_id":identity,"status":"completed",**({"reused":True} if action=="transcribe" and post.get("transcript","").strip() and not payload.get("force") else {})})
                except Cancelled:
                    self.store.merge(scope,"post",identity,{"comments_status":"cancelled","comments_error":"评论采集已取消，已保存的评论保留"} if action=="comments" else {"processing_status":"cancelled","processing_error":"处理已取消，已完成文件保留"})
                    raise
                except Exception as error:
                    stage=self.store.get(scope,"job",job["id"]).get("progress",{}).get("stage")
                    message=str(error) if isinstance(error,InputError) else ("本地语音识别未完成，请检查转写模型和视频音轨后重试；无需反复登录抖音" if stage in ("transcribe","model") else "本地文件保存未完成，请检查磁盘空间和写入权限" if stage=="saving" else "视频读取或下载未完成，请检查作品是否可播放及网络连接")
                    results.append({"aweme_id":identity,"status":"failed","message":message})
                    self.store.merge(scope,"post",identity,{"comments_status":"failed","comments_error":message} if action=="comments" else {"processing_status":"failed","processing_error":message,"processing_error_kind":type(error).__name__})
                self.update(job,results=results,completed=index+1)
            failures=sum(r["status"]=="failed" for r in results)
            reused=sum(r.get("reused",False) for r in results)
            self.update(job,status="failed" if failures==len(results) else "partial" if failures else "completed",message=f"已完成 {len(results)-failures}/{len(results)} 条"+(f"，其中 {reused} 条复用已有文案；点击查看文案，或在预览中重新转写" if reused else "")+("，失败作品可重试" if failures else ""),results=results)

    def cache_images(self,scope,session,bundle,job):
        candidates=[("account",bundle["account"]["sec_uid"])]+[("post",p["aweme_id"]) for p in bundle["posts"][:30]]
        for index,(kind,identity) in enumerate(candidates):
            self.check(job)
            self.progress(job,"covers","保存头像与封面",index,len(candidates),"张",message=f"正在保存头像与封面 {index}/{len(candidates)}")
            url=session.images.get((kind,identity))
            if not url:continue
            try:
                from .media import read_public
                try:content,mime=read_public(url,3*1024*1024,image=True)
                except Exception:
                    if not hasattr(session,"read_image"):raise
                    content,mime=session.read_image(url)
                relative=Path("thumbnails")/scope/(uuid.uuid4().hex+".img")
                file=self.store.directory/relative
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(content)
                self.store.merge(scope,kind,identity,{"image_path":relative.as_posix(),"image_mime":mime,"image_error":""})
            except Exception as error:
                self.store.merge(scope,kind,identity,{"image_error":str(error) if isinstance(error,InputError) else "图片暂时无法读取，请刷新作品重试"})

    def download_post(self,job,post):
        scope=job["service_account_key"]
        existing=post.get("media_path")
        if existing:
            file=(self.store.directory/existing).resolve()
            if file.is_relative_to(self.store.directory) and file.is_file() and file.stat().st_size:
                self.store.merge(scope,"post",post["aweme_id"],{"processing_status":"downloaded","processing_error":""})
                return file
        self.store.merge(scope,"post",post["aweme_id"],{"processing_status":"downloading","processing_error":""})
        self.progress(job,"download","准备下载",message="正在获取视频地址…")
        url=self.session(scope).media_for(post)
        relative=Path("media")/scope/post["aweme_id"]/uuid.uuid4().hex/"source.mp4"
        file=self.store.directory/relative
        file.parent.mkdir(parents=True,exist_ok=True)
        from .media import download_video
        def progress(size,total=None):
            self.check(job)
            self.progress(job,"download","下载视频",size,total,"bytes",message=f"正在下载视频：{size/1024/1024:.1f}"+(f" / {total/1024/1024:.1f} MB" if total else " MB · 服务器未提供总大小"))
        try:download_video(url,file,progress)
        except Exception:
            self.session(scope).media.pop(post["aweme_id"],None)
            raise
        self.store.merge(scope,"post",post["aweme_id"],{"media_path":relative.as_posix(),"processing_status":"downloaded","processing_error":""})
        return file

    def prepare_model(self,job):
        if self.model is not None:return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise InputError("转写组件未安装。请双击「安装转写组件.cmd」，完成后重新启动灵思") from None
        self.progress(job,"model","准备转写模型",message="正在准备本地 small 模型；首次使用需要下载，已有文件会复用。")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY","1")
        directory=self.store.directory/"models"
        process=subprocess.Popen([sys.executable,"-m","linsi.model_setup",str(directory)],cwd=str(Path(__file__).resolve().parents[1]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)
        started=time.monotonic();last_progress=started;last_size=-1;last_scan=0
        try:
            while process.poll() is None:
                self.check(job)
                current=time.monotonic()
                if current-last_scan>2:
                    size=0
                    for path in directory.rglob('*'):
                        try:
                            if path.is_file():size+=path.stat().st_size
                        except OSError:pass  # Downloaders atomically rename completed cache files.
                    if size!=last_size:last_progress=current;last_size=size
                    self.progress(job,"model","准备转写模型",size,None,"bytes",message=f"正在准备本地模型，模型目录已有 {size//1024//1024} MB；可随时取消")
                    last_scan=current
                if current-last_progress>180 or current-started>1800:raise InputError("模型下载长时间无进展，已停止等待；缓存保留，可检查网络后重试")
                time.sleep(.25)
            if process.returncode:raise InputError("模型未准备成功，请检查模型下载网络和磁盘空间后重试")
            self.check(job)
            model_path=str(directory/"small-local") if (directory/"small-local"/"model.bin").is_file() else "small"
            self.progress(job,"model","加载转写模型",message="模型文件已就绪，正在加载到内存…")
            self.model=WhisperModel(model_path,device="cpu",compute_type="int8",download_root=str(directory),local_files_only=True)
        except (Cancelled,InputError):raise
        except Exception:
            raise InputError("模型未准备成功，请检查模型下载网络和磁盘空间后重试") from None
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        self.check(job)
        atomic_text(self.store.directory/"models"/"ready.json",json.dumps({"model":"small","ready_at":now()}))

    def transcribe_post(self,job,post):
        scope=job["service_account_key"]
        if post.get("transcript","").strip() and not job.get("payload",{}).get("force"):
            self.store.merge(scope,"post",post["aweme_id"],{"processing_status":"transcribed","processing_error":""})
            return
        self.prepare_model(job)
        file=self.download_post(job,post)
        self.check(job)
        self.store.merge(scope,"post",post["aweme_id"],{"processing_status":"transcribing"})
        self.progress(job,"transcribe","识别语音",message="正在分析音频，首段文字识别需要一些时间…")
        segments,info=self.model.transcribe(str(file),language="zh",vad_filter=True)
        duration=getattr(info,"duration",None)
        self.progress(job,"transcribe","识别语音",0,duration,"秒",message="正在识别第一段语音…")
        rows=[]
        for segment in segments:
            self.check(job)
            rows.append({"start":segment.start,"end":segment.end,"text":segment.text.strip()})
            self.progress(job,"transcribe","识别语音",segment.end,duration,"秒",message=f"正在转写：已处理 {int(segment.end)}"+(f" / {int(duration)} 秒" if duration else " 秒"))
        content="\n".join(r["text"] for r in rows if r["text"])
        if not content.strip():raise InputError("没有识别到有效语音，文案仍为空，不能用于研究")
        self.progress(job,"saving","保存转写文案",message="语音识别完成，正在保存文案和时间轴…")
        target=file.parent/("transcript-"+uuid.uuid4().hex) if job.get("payload",{}).get("force") else file.parent
        atomic_text(target/"transcript.txt",content)
        atomic_text(target/"segments.json",json.dumps(rows,ensure_ascii=False,indent=2))
        self.store.save_transcript(scope,{"aweme_id":post["aweme_id"],"transcript":content})
        self.store.merge(scope,"post",post["aweme_id"],{"processing_status":"transcribed","processing_error":"","transcript_source":"local_whisper","transcribed_at":now()})

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)
