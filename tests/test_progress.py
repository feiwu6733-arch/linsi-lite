import io
import json
import os
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app import Server
from linsi.media import download_video
from linsi.jobs import Jobs
from linsi.store import Store, InputError
from scripts.publish_report import publish


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='linsi-progress-')
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':self.tmp.name,'KNOWLEDGE_VAULT_DIR':str(self.root/'vault')});self.env.start()

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def response(self,length):
        data=b'\x00\x00\x00\x18ftypmp42'+b'x'*100
        response=io.BytesIO(data);response.headers=Message();response.headers['Content-Type']='video/mp4'
        if length is not None:response.headers['Content-Length']=str(length)
        return response,data

    def test_download_real_length_and_unknown_total(self):
        for i,length in enumerate([112,None]):
            response,data=self.response(length);updates=[];target=self.root/f'{i}.mp4'
            with patch('linsi.media.open_media',return_value=response):
                download_video('unused',target,lambda current,total:updates.append((current,total)))
            self.assertEqual(updates,[(0,length),(len(data),length)])
            self.assertEqual(target.read_bytes(),data)

    def test_truncated_download_never_succeeds(self):
        response,_=self.response(999);target=self.root/'bad.mp4'
        with patch('linsi.media.open_media',return_value=response),self.assertRaises(InputError):
            download_video('unused',target,lambda *args:None)
        self.assertFalse(target.exists());self.assertTrue(target.with_suffix('.part').exists())

    def test_transcription_measures_duration_and_failure_not_complete(self):
        store=Store(self.root);scope=store.workspace()['service_account_key']
        store.import_bundle(scope,{'account':{'sec_uid':'progress-user','name':'测试'},'posts':[{'aweme_id':'7600000000000000001','title':'隔离样本'}]})
        jobs=Jobs(store);updates=[];original=jobs.update
        def capture(job,**fields):
            result=original(job,**fields);updates.append(result);return result
        jobs.model=Mock();jobs.model.transcribe.return_value=(iter([SimpleNamespace(start=0,end=30,text='实际文案')]),SimpleNamespace(duration=60))
        try:
            with patch.object(jobs,'download_post',return_value=self.root/'source.mp4'),patch.object(jobs,'update',side_effect=capture):
                job=jobs.submit(scope,'transcribe',{'aweme_ids':['7600000000000000001']})
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    result=store.get(scope,'job',job['id'])
                    if result['status'] not in ('queued','running'):break
                    time.sleep(.02)
            self.assertEqual(result['status'],'completed');self.assertTrue(result['started_at']);self.assertTrue(result['finished_at'])
            self.assertTrue(any(x.get('progress',{}).get('percent')==50 and x['progress']['stage']=='transcribe' for x in updates))
            self.assertEqual(result['progress']['percent'],100)
            jobs.progress(job,'transcribe','识别语音',60,60,'秒')
            jobs.update(job,status='failed',message='保存失败')
            self.assertEqual(store.get(scope,'job',job['id'])['progress']['percent'],99)
        finally:jobs.close()

    def test_report_stages_cli_scope_digest_and_completion(self):
        server=Server(0,self.root);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            store=server.store;scope=store.workspace()['service_account_key'];other=store.workspace()['service_account_key']
            store.import_bundle(scope,{'account':{'sec_uid':'progress-report','name':'测试'},'posts':[{'aweme_id':'7600000000000000001','title':'隔离样本','transcript':'有来源的文案'}]})
            report=store.create_report(scope,{'sec_uid':'progress-report','aweme_ids':['7600000000000000001'],'report_type':'breakdown'})
            from linsi.reports import save_sources
            directory=save_sources(self.root,report)
            for phase in ['reading','analyzing','writing','failed','analyzing','writing']:
                self.assertEqual(publish(directory/'sources.json',None,base,phase),report['id'])
                current=store.get(scope,'report',report['id']);self.assertEqual(current['phase'],phase)
                self.assertNotEqual(current['status'],'completed')
            body={'id':report['id'],'source_digest':report['source_digest'],'phase':'reading'}
            with self.assertRaises(InputError):store.report_progress(other,body)
            with self.assertRaises(InputError):store.report_progress(scope,{**body,'source_digest':'wrong'})
            request=Request(base+'/api/reports/progress',data=json.dumps({**body,'service_account_key':scope}).encode(),headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,403)
            caught.exception.close()
            directory.joinpath('finished.md').write_text('来源 7600000000000000001\n拆解测试',encoding='utf-8')
            publish(directory/'sources.json',directory/'finished.md',base)
            self.assertEqual(store.get(scope,'report',report['id'])['phase'],'completed')
            with self.assertRaises(InputError):store.report_progress(scope,body)
        finally:server.shutdown();server.server_close();thread.join()

    def test_historical_failures_present_current_evidence_without_rewriting(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];other=store.workspace()['service_account_key']
        bundle={'account':{'sec_uid':'history-test','name':'测试'},'posts':[{'aweme_id':'7600000000000000001','title':'隔离视频'}]}
        store.import_bundle(scope,bundle);store.import_bundle(other,bundle)
        old={'id':'old-preview','action':'preview','status':'partial','payload':{'aweme_ids':['7600000000000000001']},'results':[{'aweme_id':'7600000000000000001','status':'failed','message':'旧错误'}]}
        store.merge(scope,'job',old['id'],old);store.merge(other,'job',old['id'],old)
        jobs=Jobs(store)
        try:
            current=jobs.public_jobs(scope)[0];self.assertEqual(current['display_status'],'failed');self.assertTrue(current['needs_attention'])
            self.root.joinpath('video.mp4').write_bytes(b'actual file placeholder')
            store.merge(scope,'post','7600000000000000001',{'media_path':'video.mp4'})
            current=jobs.public_jobs(scope)[0];self.assertFalse(current['needs_attention']);self.assertEqual(current['resolution']['kind'],'available')
            self.assertEqual(store.get(scope,'job',old['id'])['status'],'partial')
            self.assertNotIn('resolution',store.get(scope,'job',old['id']))
            self.assertTrue(jobs.public_jobs(other)[0]['needs_attention'])
            store.merge(scope,'post','7600000000000000001',{'media_path':'missing.mp4'})
            self.assertTrue(jobs.public_jobs(scope)[0]['needs_attention'])
            store.merge(scope,'post','7600000000000000001',{'media_type':'gallery'})
            current=jobs.public_jobs(scope)[0];self.assertEqual(current['resolution']['kind'],'not_applicable')
        finally:jobs.close()

    def test_comment_recovery_respects_requested_limit_and_exhaustion(self):
        store=Store(self.root);scope=store.workspace()['service_account_key']
        store.import_bundle(scope,{'account':{'sec_uid':'comment-recovery','name':'测试'},'posts':[{'aweme_id':'7600000000000000001','title':'隔离样本'}]})
        store.merge(scope,'job','old-comments',{'id':'old-comments','action':'comments','status':'failed','payload':{'aweme_ids':['7600000000000000001'],'limit':20}})
        jobs=Jobs(store)
        try:
            store.merge(scope,'post','7600000000000000001',{'comments_status':'completed','comments_has_more':True})
            self.assertTrue(jobs.public_jobs(scope)[0]['needs_attention'])
            store.merge(scope,'post','7600000000000000001',{'comments_has_more':False})
            self.assertFalse(jobs.public_jobs(scope)[0]['needs_attention'])
        finally:jobs.close()

    def test_force_transcription_replaces_only_after_success_preserving_previous_file(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];identity='7600000000000000001'
        store.import_bundle(scope,{'account':{'sec_uid':'force-test','name':'测试'},'posts':[{'aweme_id':identity,'title':'视频','transcript':'原文案'}]})
        self.root.joinpath('transcript.txt').write_text('原文件',encoding='utf-8')
        jobs=Jobs(store);jobs.model=Mock()
        try:
            post=store.get(scope,'post',identity);job={'id':'force-job','service_account_key':scope,'payload':{'force':True}}
            store.merge(scope,'job',job['id'],{**job,'action':'transcribe','status':'running'})
            jobs.model.transcribe.return_value=(iter([]),SimpleNamespace(duration=10))
            with patch.object(jobs,'download_post',return_value=self.root/'source.mp4'):
                with self.assertRaises(InputError):jobs.transcribe_post(job,post)
                self.assertEqual(store.get(scope,'post',identity)['transcript'],'原文案')
                jobs.model.transcribe.return_value=(iter([SimpleNamespace(start=0,end=10,text='新的有效文案')]),SimpleNamespace(duration=10))
                jobs.transcribe_post(job,post)
            self.assertEqual(store.get(scope,'post',identity)['transcript'],'新的有效文案')
            self.assertEqual(self.root.joinpath('transcript.txt').read_text(encoding='utf-8'),'原文件')
            self.assertEqual(len(list(self.root.glob('transcript-*/transcript.txt'))),1)
        finally:jobs.close()

    def test_gallery_rejected_before_queue_and_comments_still_supported(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];identity='7600000000000000001'
        store.import_bundle(scope,{'account':{'sec_uid':'gallery-test','name':'测试'},'posts':[{'aweme_id':identity,'title':'图文','media_type':'gallery'}]})
        store.merge(scope,'post',identity,{'media_type':'gallery'})
        jobs=Jobs(store)
        try:
            for action in ['transcribe','preview','download']:
                with self.assertRaises(InputError):jobs.submit(scope,action,{'aweme_ids':[identity]})
            self.assertEqual(store.records(scope,'job'),[])
        finally:jobs.close()
