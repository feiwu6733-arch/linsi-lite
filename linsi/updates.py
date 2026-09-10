"""Updates from the single official GitHub repository; no user data is transmitted."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

REPOSITORY = "feiwu6733-arch/linsi-lite"
RELEASES = "https://github.com/" + REPOSITORY + "/releases"
API = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"
MAX_PACKAGE = 80 * 1024 * 1024


def version(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", str(value))
    if not match:
        raise ValueError("版本号格式不正确")
    return tuple(map(int, match.groups()))


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def allowed_path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    parts = PurePosixPath(name).parts
    if name.startswith("/") or any(p.lower() in (".", "..", ".local", ".venv", ".git", "dist", "artifacts", "__pycache__") or p.endswith((" ", ".")) or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])',p.upper().split('.')[0]) for p in parts):
        return False
    return not any(p.lower().startswith('.env') for p in parts) and PurePosixPath(name).as_posix() == name


def inspect_package(package, expected_version, expected_digest):
    package = Path(package)
    if package.stat().st_size > MAX_PACKAGE or hashlib.sha256(package.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("更新包校验失败，请重新下载")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist(); names = [e.filename for e in entries]
        if len(names) > 500 or len({n.casefold() for n in names}) != len(names) or any(not allowed_path(n) for n in names):
            raise ValueError("更新包包含不允许的文件路径")
        if sum(e.file_size for e in entries) > 150 * 1024 * 1024 or any(stat.S_ISLNK(e.external_attr >> 16) for e in entries):
            raise ValueError("更新包内容超出允许范围")
        manifest = json.loads(archive.read("MANIFEST.json"))
        if manifest.get("product") != "Linsi Lite" or manifest.get("version") != expected_version or manifest.get("update_format") != 1:
            raise ValueError("更新包与发布版本不一致")
        if sys.version_info[:2] < tuple(manifest.get("minimum_python", [3, 11])):
            raise ValueError("请先升级 Python，再安装此版本")
        records = manifest["files"]
        listing = [r['path'] for r in records]
        if len(set(listing)) != len(listing) or set(names) != set(listing) | {"MANIFEST.json"}:
            raise ValueError("更新包文件清单不完整")
        if set(json.loads(archive.read('release-files.json'))) != set(listing):
            raise ValueError("更新包发行清单不一致")
        if not {'app.py', 'linsi/__init__.py', 'start.ps1', 'release-files.json'} <= set(listing):
            raise ValueError("更新包缺少启动文件")
        for record in records:
            if hashlib.sha256(archive.read(record['path'])).hexdigest() != record['sha256']:
                raise ValueError("更新包内文件校验失败")
        return manifest


class Updates:
    def __init__(self, data_dir, current_version):
        self.directory = Path(data_dir).resolve() / 'updates'
        self.current = current_version
        self.lock = threading.RLock(); self.stop_event = threading.Event(); self.busy = False
        self.preferences = {}
        try:self.preferences = json.loads((self.directory/'preferences.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):pass
        self.result = {'status':'idle', 'message':'启动后自动检查更新', 'checked_at':None}
        self.release = None
        try:
            pending=json.loads((self.directory/'pending.json').read_text(encoding='utf-8'))
            if version(pending['version'])>version(self.current):
                inspect_package(self.directory/'download.zip',pending['version'],pending['sha256'])
                self.result.update(status='ready',latest_version=pending['version'],message='更新包已下载，可以更新并重启')
        except (OSError,ValueError,KeyError,zipfile.BadZipFile):pass

    def public(self):
        with self.lock:
            data = dict(self.result)
            data.update(current_version=self.current, repository=REPOSITORY, releases_url=RELEASES, busy=self.busy)
            try:data['last_install']=json.loads((self.directory/'last-install.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):pass
            latest = data.get('latest_version')
            data['notify'] = bool(latest and version(latest)>version(self.current) and self.preferences.get('ignored')!=latest and time.time()>self.preferences.get('remind_after',0))
            return data

    def start(self):
        def loop():
            if self.stop_event.wait(2):return
            while not self.stop_event.is_set():
                self.request_check()
                if self.stop_event.wait(24*3600):return
        threading.Thread(target=loop,daemon=True).start()

    def close(self):self.stop_event.set()

    def request_check(self):
        with self.lock:
            if self.busy or self.result['status']=='ready':return self.public()
            self.busy=True;self.result.update(status='checking',message='正在检查 GitHub 最新版本…')
        threading.Thread(target=self.check,daemon=True).start()
        return self.public()

    def check(self):
        try:
            request=Request(API,headers={'Accept':'application/vnd.github+json','User-Agent':'Linsi-Lite-Update','X-GitHub-Api-Version':'2022-11-28'})
            def read_release(request):
                with urlopen(request,timeout=15) as response:
                    raw=response.read(2*1024*1024+1)
                    if len(raw)>2*1024*1024:raise ValueError('版本信息过大')
                    return raw
            try:raw=read_release(request)
            except OSError as error:
                if isinstance(error,HTTPError) and error.code not in (403,429):raise
                # Release-hosted metadata avoids the public REST API's shared IP quota.
                raw=read_release(Request(RELEASES+'/latest/download/update.json',headers={'User-Agent':'Linsi-Lite-Update'}))
            release=json.loads(raw); tag=release['tag_name']; latest='.'.join(map(str,version(tag)))
            if release.get('draft') or release.get('prerelease'):raise ValueError('不是正式发布版本')
            url=RELEASES+'/tag/'+tag
            asset=next((a for a in release.get('assets',[]) if a.get('name')==f'linsi-lite-{latest}.zip'),None)
            if asset:
                parsed=urlparse(asset.get('browser_download_url',''))
                prefix=f'/{REPOSITORY}/releases/download/{tag}/'
                digest=asset.get('digest') or ''
                if parsed.scheme!='https' or parsed.netloc!='github.com' or not parsed.path.startswith(prefix) or not re.fullmatch(r'sha256:[a-f0-9]{64}',digest) or not 0<asset.get('size',0)<=MAX_PACKAGE:
                    asset=None
            with self.lock:
                self.release={'version':latest,'asset':asset} if asset else None
                self.result={'status':'available' if version(latest)>version(self.current) else 'current','message':'发现新版本' if version(latest)>version(self.current) else '已是最新版本','latest_version':latest,'notes':str(release.get('body') or '')[:12000],'release_url':url,'download_ready':bool(asset),'checked_at':time.time()}
        except HTTPError as error:
            with self.lock:self.result.update(status='unpublished' if error.code==404 else 'error',message='暂无正式发布版本' if error.code==404 else '暂时无法检查更新，稍后可重试',checked_at=time.time())
        except Exception:
            with self.lock:self.result.update(status='error',message='暂时无法连接 GitHub，本地功能可继续使用',checked_at=time.time())
        finally:
            with self.lock:self.busy=False

    def dismiss(self, action):
        with self.lock:
            if action=='ignore':self.preferences['ignored']=self.result.get('latest_version')
            elif action=='later':self.preferences['remind_after']=time.time()+24*3600
            else:raise ValueError('请选择稍后提醒或忽略此版本')
            write_json(self.directory/'preferences.json',self.preferences)
            return self.public()

    def download(self):
        with self.lock:
            if self.busy:return self.public()
            if not self.release or version(self.release['version'])<=version(self.current):raise ValueError('请先检查可用的新版本')
            release=dict(self.release); self.busy=True
            self.result.update(status='downloading',message='正在下载更新包…',received=0,total=release['asset']['size'])
        def worker():
            try:
                self.directory.mkdir(parents=True,exist_ok=True)
                target=self.directory/'download.zip';temp=self.directory/'download.part'
                request=Request(release['asset']['browser_download_url'],headers={'User-Agent':'Linsi-Lite-Update'})
                with urlopen(request,timeout=30) as response,temp.open('wb') as stream:
                    final=urlparse(response.url)
                    if final.scheme!='https' or final.hostname not in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):raise ValueError('更新下载地址不正确')
                    size=0
                    while chunk:=response.read(128*1024):
                        size+=len(chunk)
                        if size>MAX_PACKAGE or size>release['asset']['size']:raise ValueError('更新包大小不正确')
                        stream.write(chunk)
                        with self.lock:self.result['received']=size
                if size!=release['asset']['size']:raise ValueError('更新包未下载完整')
                digest=release['asset']['digest'].split(':')[1]
                inspect_package(temp,release['version'],digest);os.replace(temp,target)
                write_json(self.directory/'pending.json',{'version':release['version'],'sha256':digest})
                with self.lock:self.result.update(status='ready',message='下载完成，可以更新并重启')
            except Exception as error:
                with self.lock:self.result.update(status='error',message=str(error) if isinstance(error,ValueError) else '下载失败，请检查网络后重试')
            finally:
                with self.lock:self.busy=False
        threading.Thread(target=worker,daemon=True).start();return self.public()


def safe_target(root, name):
    if not allowed_path(name):raise ValueError('不允许的更新路径')
    target=root/name
    if not target.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [target,*target.parents] if p!=root.parent):
        raise ValueError('更新路径不能包含符号链接')
    return target


def install_files(root, directory):
    """Called only after the serving process has exited. Returns rollback information."""
    root=Path(root).resolve();directory=Path(directory).resolve()
    pending=json.loads((directory/'pending.json').read_text(encoding='utf-8'))
    manifest=inspect_package(directory/'download.zip',pending['version'],pending['sha256'])
    backup=directory/('backup-'+str(time.time_ns()));backup.mkdir(parents=True)
    records=[]
    for record in manifest['files']:
        path=record['path'];target=safe_target(root,path)
        # Unknown locally authored files are never silently overwritten.
        current=json.loads((root/'release-files.json').read_text(encoding='utf-8'))
        if target.exists() and path not in current:raise ValueError('更新与本地额外文件冲突：'+path)
        if target.exists():
            destination=backup/path;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,destination)
        records.append({'path':path,'existed':target.exists()})
    database=directory.parent/'linsi.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database)) as source,closing(sqlite3.connect(backup/'database.sqlite3')) as dest:source.backup(dest)
    write_json(backup/'rollback.json',records)
    try:
        with zipfile.ZipFile(directory/'download.zip') as archive:
            for record in records:
                target=safe_target(root,record['path']);target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_name(target.name+'.update-tmp');temp.write_bytes(archive.read(record['path']));os.replace(temp,target)
    except Exception:
        rollback(root,backup,False);raise
    return backup,pending['version']


def rollback(root, backup, database=True):
    root=Path(root).resolve();backup=Path(backup)
    for record in json.loads((backup/'rollback.json').read_text(encoding='utf-8')):
        target=safe_target(root,record['path'])
        if record['existed']:shutil.copy2(backup/record['path'],target)
        elif target.is_file():target.unlink()
    if database and (backup/'database.sqlite3').exists():
        db=backup.parent.parent/'linsi.sqlite3'
        with closing(sqlite3.connect(backup/'database.sqlite3')) as source,closing(sqlite3.connect(db)) as dest:source.backup(dest)


def wait_process(pid, timeout=45):
    # A Windows process handle observes this exact process, with no signal probe.
    if os.name!='nt':raise ValueError('自动安装目前支持 Windows 桌面端')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x00100000,False,pid)
    if not handle:
        if ctypes.get_last_error()==87:return
        raise ValueError('无法确认原服务退出，请重新启动后重试')
    try:
        if kernel.WaitForSingleObject(handle,int(timeout*1000))!=0:raise ValueError('原服务尚未退出，更新已暂停')
    finally:kernel.CloseHandle(handle)


def run_install(root, data_dir, port, pid):
    root=Path(root).resolve();data_dir=Path(data_dir).resolve();directory=data_dir/'updates';backup=None
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    def launch():
        log=(directory/'restart.log').open('ab')
        try:return subprocess.Popen([sys.executable,str(root/'app.py'),'--port',str(port),'--data-dir',str(data_dir),'--no-browser'],cwd=root,creationflags=flags,stdout=log,stderr=log)
        finally:log.close()
    try:
        wait_process(pid)
        backup,target_version=install_files(root,directory)
        child=launch();healthy=False
        for _ in range(60):
            if child.poll() is not None:break
            try:
                with urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1) as response:health=json.load(response)
                if health.get('product')=='linsi-lite' and health.get('version')==target_version and health.get('pid')==child.pid:healthy=True;break
            except Exception:pass
            time.sleep(.5)
        if not healthy:
            if child.poll() is None:child.terminate();child.wait(timeout=10)
            raise ValueError('新版启动失败，已恢复更新前版本')
        write_json(directory/'last-install.json',{'status':'completed','version':target_version,'time':time.time()})
        (directory/'pending.json').unlink(missing_ok=True)
    except Exception as error:
        if backup is not None:
            rollback(root,backup);launch()
        write_json(directory/'last-install.json',{'status':'failed','message':str(error),'time':time.time()})
        if backup is None:
            # If preparation failed after old service exited, recover the original app.
            try:
                with urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1):pass
            except Exception:launch()
    finally:
        lock_path=root/'.linsi-update.lock'
        try:
            if json.loads(lock_path.read_text(encoding='utf-8')).get('pid')==pid:lock_path.unlink()
        except (OSError,ValueError):pass


if __name__=='__main__':
    run_install(sys.argv[1],sys.argv[2],int(sys.argv[3]),int(sys.argv[4]))
