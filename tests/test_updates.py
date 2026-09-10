import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from linsi.updates import Updates, version, inspect_package, install_files, rollback, allowed_path, run_install


def package(root, extra=None):
    files={'app.py':b'new app','linsi/__init__.py':b'VERSION = "0.6.1"','start.ps1':b'new start','notes.md':b'new notes'}
    if extra:files.update(extra)
    files['release-files.json']=json.dumps(list(files)+['release-files.json']).encode()
    manifest={'product':'Linsi Lite','version':'0.6.1','update_format':1,'minimum_python':[3,11],'files':[{'path':name,'sha256':hashlib.sha256(content).hexdigest()} for name,content in files.items()]}
    target=Path(root)/'download.zip'
    with zipfile.ZipFile(target,'w') as z:
        for name,content in files.items():z.writestr(name,content)
        z.writestr('MANIFEST.json',json.dumps(manifest))
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    return target,digest


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='linsi-update-test-');self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':str(self.root/'data'),'KNOWLEDGE_VAULT_DIR':str(self.root/'vault')});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def test_numeric_versions_and_reject_invalid(self):
        self.assertGreater(version('0.10.0'),version('0.9.9'))
        for value in ['v1.2.3-beta','../1.2.3','01.2.3','1.2','latest']:
            with self.assertRaises(ValueError):version(value)

    def test_package_digest_paths_and_private_files(self):
        file,digest=package(self.root)
        self.assertEqual(inspect_package(file,'0.6.1',digest)['version'],'0.6.1')
        with self.assertRaises(ValueError):inspect_package(file,'0.6.1','0'*64)
        for name in ['../app.py','C:/app.py','/app.py','.local/ai-config.json','.LOCAL/x','.ENV','a/../../app.py','.env','linsi\\evil.py','CON','COM2.txt','a/./b']:
            self.assertFalse(allowed_path(name),name)
        file,digest=package(self.root,{'.local/private.json':b'no'})
        with self.assertRaises(ValueError):inspect_package(file,'0.6.1',digest)

    def test_corrupt_file_rejected_even_if_outer_digest_matches(self):
        file,digest=package(self.root)
        with zipfile.ZipFile(file) as z:files={n:z.read(n) for n in z.namelist()}
        files['app.py']=b'tampered'
        with zipfile.ZipFile(file,'w') as z:
            for n,c in files.items():z.writestr(n,c)
        digest=hashlib.sha256(file.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):inspect_package(file,'0.6.1',digest)

    def test_install_and_rollback_preserve_data_and_unknown_files(self):
        app=self.root/'app';app.mkdir();(app/'linsi').mkdir()
        old={'app.py':b'old app','linsi/__init__.py':b'old version','start.ps1':b'old start'}
        for name,content in old.items():(app/name).write_bytes(content)
        (app/'release-files.json').write_text(json.dumps(list(old)+['release-files.json']))
        data=app/'.local';directory=data/'updates';directory.mkdir(parents=True)
        (data/'ai-config.json').write_text('private configuration')
        (app/'my-extra.txt').write_text('keep me')
        with closing(sqlite3.connect(data/'linsi.sqlite3')) as db:db.execute('create table sample(value text)');db.execute("insert into sample values('original')");db.commit()
        target,digest=package(directory)
        (directory/'pending.json').write_text(json.dumps({'version':'0.6.1','sha256':digest}))
        backup,new_version=install_files(app,directory)
        self.assertEqual(new_version,'0.6.1');self.assertEqual((app/'app.py').read_bytes(),b'new app')
        self.assertEqual((data/'ai-config.json').read_text(),'private configuration')
        self.assertEqual((app/'my-extra.txt').read_text(),'keep me')
        with closing(sqlite3.connect(data/'linsi.sqlite3')) as db:db.execute("update sample set value='changed by migration'");db.commit()
        rollback(app,backup)
        for name,content in old.items():self.assertEqual((app/name).read_bytes(),content)
        self.assertFalse((app/'notes.md').exists())
        with closing(sqlite3.connect(data/'linsi.sqlite3')) as db:self.assertEqual(db.execute('select value from sample').fetchone()[0],'original')

    def test_unknown_existing_file_conflict_is_not_overwritten(self):
        app=self.root/'app';app.mkdir();(app/'release-files.json').write_text('["release-files.json"]');(app/'app.py').write_text('custom')
        directory=self.root/'data'/'updates';directory.mkdir(parents=True);_,digest=package(directory)
        (directory/'pending.json').write_text(json.dumps({'version':'0.6.1','sha256':digest}))
        with self.assertRaises(ValueError):install_files(app,directory)
        self.assertEqual((app/'app.py').read_text(),'custom')

    def release(self):
        return {'tag_name':'v0.6.1','draft':False,'prerelease':False,'body':'New feature','assets':[{'name':'linsi-lite-0.6.1.zip','size':1024,'digest':'sha256:'+'a'*64,'browser_download_url':'https://github.com/feiwu6733-arch/linsi-lite/releases/download/v0.6.1/linsi-lite-0.6.1.zip'}]}

    def test_github_release_and_notification_preferences(self):
        u=Updates(self.root/'data','0.6.0')
        with patch('linsi.updates.urlopen',return_value=io.BytesIO(json.dumps(self.release()).encode())):u.check()
        self.assertTrue(u.public()['notify']);self.assertTrue(u.public()['download_ready'])
        u.dismiss('ignore');self.assertFalse(u.public()['notify'])
        restored=Updates(self.root/'data','0.6.0');self.assertEqual(restored.preferences['ignored'],'0.6.1')
        u.preferences={};u.dismiss('later');self.assertFalse(u.public()['notify'])

    def test_foreign_or_unverified_asset_not_installable(self):
        for change in [{'browser_download_url':'https://evil.test/app.zip'},{'digest':None}]:
            release=self.release();release['assets'][0].update(change);u=Updates(self.root/'data','0.6.0')
            with patch('linsi.updates.urlopen',return_value=io.BytesIO(json.dumps(release).encode())):u.check()
            self.assertFalse(u.public()['download_ready'])
            with self.assertRaises(ValueError):u.download()

    def test_offline_and_no_releases_are_non_blocking(self):
        for error,status in [(OSError('offline'),'error'),(HTTPError('https://api.github.com',404,'none',{},None),'unpublished')]:
            u=Updates(self.root/'data','0.6.0')
            with patch('linsi.updates.urlopen',side_effect=error):u.check()
            self.assertEqual(u.public()['status'],status);self.assertFalse(u.busy)

    def test_ready_package_survives_restart(self):
        data=self.root/'data';directory=data/'updates';directory.mkdir(parents=True);_,digest=package(directory)
        (directory/'pending.json').write_text(json.dumps({'version':'0.6.1','sha256':digest}))
        u=Updates(data,'0.6.0');self.assertEqual(u.public()['status'],'ready')
        u.request_check();self.assertEqual(u.public()['status'],'ready')
        self.assertNotEqual(Updates(data,'0.6.1').public()['status'],'ready')

    def test_download_verifies_package_before_marking_ready(self):
        target,digest=package(self.root);content=target.read_bytes()
        for corrupt in (False,True):
            u=Updates(self.root/('bad' if corrupt else 'good'),'0.6.0')
            release=self.release();release['assets'][0].update(size=len(content),digest='sha256:'+digest)
            with patch('linsi.updates.urlopen',return_value=io.BytesIO(json.dumps(release).encode())):u.check()
            response=io.BytesIO(content if not corrupt else b'x'*len(content))
            response.url=release['assets'][0]['browser_download_url']
            with patch('linsi.updates.urlopen',return_value=response):
                u.download()
                deadline=time.monotonic()+5
                while u.busy and time.monotonic()<deadline:time.sleep(.01)
            self.assertFalse(u.busy)
            self.assertEqual(u.public()['status'],'error' if corrupt else 'ready')
            self.assertEqual((u.directory/'pending.json').exists(),not corrupt)

    @unittest.skipUnless(os.name=='nt','Windows installer process integration')
    def test_install_refuses_work_in_every_workspace_and_login_watch(self):
        from app import Server
        from linsi.store import InputError
        for kind,row in [('job',{'status':'queued'}),('connection_flow',{'status':'waiting_login'}),('report',{'status':'awaiting_ai','api_requested':True}),('batch',{'status':'preparing'}),('batch',{'status':'awaiting_ai','ai_auto':True})]:
            server=Server.__new__(Server)
            server.updates=SimpleNamespace(public=lambda:{'status':'ready'})
            server.store=SimpleNamespace(profiles=lambda:[{'service_account_key':'first'},{'service_account_key':'other'}],records=lambda scope,k:[row] if scope=='other' and k==kind else [])
            server.research=SimpleNamespace(summaries=lambda scope:[row] if scope=='other' and kind=='batch' else [])
            with self.assertRaisesRegex(InputError,'仍有任务'):server.install_update()

    @unittest.skipUnless(os.name=='nt','Windows installer process integration')
    def test_real_installer_restart_and_failed_start_rollback(self):
        server_code='''import argparse,json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--port',type=int);p.add_argument('--data-dir');p.add_argument('--no-browser',action='store_true');a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data=json.dumps({'product':'linsi-lite','version':'VERSION_VALUE','pid':os.getpid()}).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
'''
        original_popen=subprocess.Popen
        for fails in (False,True):
            app=self.root/('rollback-app' if fails else 'success-app');app.mkdir();(app/'linsi').mkdir()
            old=server_code.replace('VERSION_VALUE','0.6.0').encode()
            (app/'app.py').write_bytes(old);(app/'start.ps1').write_text('old start');(app/'linsi/__init__.py').write_text('old version')
            (app/'release-files.json').write_text(json.dumps(['app.py','start.ps1','linsi/__init__.py','release-files.json']))
            data=app/'.local';directory=data/'updates';directory.mkdir(parents=True)
            new=b'raise RuntimeError("test startup failure")' if fails else server_code.replace('VERSION_VALUE','0.6.1').encode()
            _,digest=package(directory,{'app.py':new})
            (directory/'pending.json').write_text(json.dumps({'version':'0.6.1','sha256':digest}))
            with socket.socket() as bound:bound.bind(('127.0.0.1',0));port=bound.getsockname()[1]
            old_process=original_popen([sys.executable,'-c','import time;time.sleep(.2)'],creationflags=subprocess.CREATE_NO_WINDOW)
            children=[]
            def launch(*args,**kwargs):
                child=original_popen(*args,**kwargs);children.append(child);return child
            try:
                with patch('linsi.updates.subprocess.Popen',side_effect=launch):run_install(app,data,port,old_process.pid)
                result=json.loads((directory/'last-install.json').read_text())
                self.assertEqual(result['status'],'failed' if fails else 'completed')
                self.assertEqual(len(children),2 if fails else 1)
                self.assertEqual((app/'app.py').read_bytes(),old if fails else new)
                self.assertEqual((directory/'pending.json').exists(),fails)
            finally:
                old_process.wait(timeout=5)
                for child in children:
                    if child.poll() is None:child.terminate()
                    child.wait(timeout=5)


if __name__=='__main__':unittest.main()
