import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
from linsi.store import Store, InputError
from linsi.douyin import DouyinSession, source_link, user_record, post_record
from linsi.jobs import Jobs


USER={"sec_uid":"MS4wLjAB-qa-user","nickname":"隔离测试账号","follower_count":200}
RAW={"aweme_id":"7600000000000000001","author":USER,"desc":"这是标题，不是语音文案","statistics":{"digg_count":32},"create_time":1700000000,"video":{"play_addr":{"url_list":["https://v.example.douyinvod.com/video?expires=secret"]}}}


class Response:
    def __init__(self,path,payload):self.url="https://www.douyin.com"+path;self.payload=payload
    def json(self):return self.payload


class NormalizationTests(unittest.TestCase):
    def test_links_reject_foreign_hosts_and_strip_query(self):
        self.assertEqual(source_link('分享 https://v.douyin.com/ABCDE/ 复制打开'),"https://v.douyin.com/ABCDE/")
        self.assertEqual(source_link('https://www.douyin.com/user/abc123?token=not-saved'),"https://www.douyin.com/user/abc123")
        for value in ('https://evil.com/user/a','https://www.douyin.com.evil.com/user/a','http://www.douyin.com/user/a','https://www.douyin.com@evil.com/video/7600000000000000001'):
            with self.assertRaises(InputError):source_link(value)

    def test_unknown_counts_and_description_not_transcript(self):
        post=post_record(RAW)
        self.assertEqual(post['likes'],32)
        self.assertIsNone(post['comments'])
        self.assertNotIn('transcript',post)
        self.assertNotIn('video',post)

    def test_other_profile_never_becomes_logged_in_identity(self):
        session=DouyinSession('scope','unused')
        session.capture(Response('/aweme/v1/web/user/profile/other/',{'user':USER,'status_code':0}))
        self.assertIsNone(session.identity)
        session.capture(Response('/aweme/v1/web/user/profile/self/',{'user':USER,'status_code':0}))
        self.assertEqual(session.identity['sec_uid'],USER['sec_uid'])
        self.assertEqual(session.status,'connected')

    def test_media_urls_never_enter_public_state(self):
        session=DouyinSession('scope','unused')
        session.capture(Response('/aweme/v1/web/aweme/post/',{'aweme_list':[RAW],'has_more':1,'status_code':0}))
        self.assertIn(RAW['aweme_id'],session.media)
        self.assertNotIn('expires',json.dumps(session.public_status()))
        self.assertNotIn('expires',json.dumps(session.posts))

    def test_failed_self_response_invalidates_login(self):
        session=DouyinSession('scope','unused')
        session.capture(Response('/aweme/v1/web/user/profile/self/',{'user':USER,'status_code':0}))
        session.capture(Response('/aweme/v1/web/user/profile/self/',{'status_code':8}))
        self.assertIsNone(session.identity)

    def test_clear_failure_still_closes_browser(self):
        session=DouyinSession('scope','unused')
        context=Mock()
        runtime=Mock()
        session.context=context
        session.runtime=runtime
        context.clear_cookies.side_effect=RuntimeError('private details')
        with self.assertRaises(InputError):session.close(clear=True)
        context.close.assert_called_once()
        runtime.stop.assert_called_once()
        self.assertIsNone(session.context)
        self.assertIsNone(session.identity)


class FakeSession:
    def __init__(self,scope,directory):self.scope=scope;self.images={};self.has_more=False;self.context=None;self.message='等待真实登录';self.identity=None
    def pump(self):pass
    def public_status(self):return {'status':'waiting_login','identity':self.identity,'message':self.message,'browser_open':True}
    def login(self):return self.public_status()
    verify=login
    def close(self,clear=False):pass
    def collect(self,link,more=False):return {'account':user_record(USER),'posts':[post_record(RAW)]}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='linsi-queue-')
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':self.tmp.name,'KNOWLEDGE_VAULT_DIR':str(Path(self.tmp.name)/'vault')})
        self.env.start()
        self.store=Store(self.tmp.name)
        self.scope=self.store.create_profile({'name':'队列验收','direction':'男性成长','positioning':'隔离测试'})['service_account_key']
        self.jobs=Jobs(self.store,FakeSession)

    def tearDown(self):self.jobs.close();self.env.stop();self.tmp.cleanup()

    def wait(self,job):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            current=self.store.get(self.scope,'job',job['id'])
            if current['status'] not in ('queued','running','cancelling'):return current
            time.sleep(.02)
        self.fail('Task did not finish')

    def test_collect_saves_real_normalized_records_without_transcript(self):
        job=self.jobs.submit(self.scope,'collect',{'link':'https://www.douyin.com/user/'+USER['sec_uid']})
        result=self.wait(job)
        self.assertEqual(result['status'],'completed')
        post=self.store.get(self.scope,'post',RAW['aweme_id'])
        self.assertEqual(post['transcript'],'')
        self.assertNotIn('expires',json.dumps(post))
        self.assertEqual(result['result']['imported'],1)

    def test_connect_task_completion_does_not_claim_authenticated(self):
        self.wait(self.jobs.submit(self.scope,'connect'))
        self.assertEqual(self.jobs.state(self.scope)['status'],'waiting_login')
        self.assertIsNone(self.jobs.state(self.scope)['identity'])

    def test_quick_start_requires_choice_for_multiple_workspaces(self):
        self.assertEqual(self.store.workspace(reuse=True)['service_account_key'],self.scope)
        second=self.store.workspace()
        self.assertNotEqual(second['service_account_key'],self.scope)
        self.assertEqual(second['direction'],'')
        self.assertEqual(second['positioning'],'')
        with self.assertRaises(InputError):self.store.workspace(reuse=True)

    def test_edit_workspace_preserves_identity_and_other_fields(self):
        before=self.store.profile(self.scope)
        result=self.store.update_profile(self.scope,{'name':'改个名称'})
        self.assertEqual(result['service_account_key'],self.scope)
        self.assertEqual(result['positioning'],before['positioning'])
        with self.assertRaises(InputError):self.store.update_profile(self.scope,{'direction':'其他定位'})
        self.assertEqual(self.store.profile(self.scope)['direction'],before['direction'])

    def test_cannot_queue_unknown_scope_or_empty_selection(self):
        with self.assertRaises(InputError):self.jobs.submit('wrong','connect')
        with self.assertRaises(InputError):self.jobs.submit(self.scope,'download',{'aweme_ids':[]})

    def test_retry_keeps_failed_and_unprocessed_ids(self):
        job={'id':'retry-case','action':'download','payload':{'aweme_ids':['a','b','c']},'status':'cancelled','results':[{'aweme_id':'a','status':'completed'},{'aweme_id':'b','status':'failed'}]}
        self.store.merge(self.scope,'job',job['id'],job)
        with patch.object(self.jobs,'submit') as submit:
            self.jobs.retry(self.scope,job['id'])
            self.assertEqual(submit.call_args.args[2]['aweme_ids'],['b','c'])

    def test_interrupted_task_is_visible_after_restart(self):
        self.jobs.close()
        self.store.merge(self.scope,'job','interrupted',{'id':'interrupted','status':'running','action':'collect','payload':{}})
        self.jobs=Jobs(self.store,FakeSession)
        self.assertEqual(self.store.get(self.scope,'job','interrupted')['status'],'interrupted')

    def test_breakdown_requires_one_nonempty_transcript(self):
        self.store.import_bundle(self.scope,{'account':user_record(USER),'posts':[{**post_record(RAW),'transcript':'有效文案'}]})
        report=self.store.create_report(self.scope,{'sec_uid':USER['sec_uid'],'aweme_ids':[RAW['aweme_id']],'report_type':'breakdown'})
        self.assertEqual(report['report_type'],'breakdown')
        self.assertIn('单条拆解',report['title'])

    def test_empty_transcription_fails_without_ready_text(self):
        self.store.import_bundle(self.scope,{'account':user_record(USER),'posts':[post_record(RAW)]})
        self.jobs.model=Mock()
        self.jobs.model.transcribe.return_value=(iter([]),None)
        with patch.object(self.jobs,'download_post',return_value=Path(self.tmp.name)/'source.mp4'):
            job=self.wait(self.jobs.submit(self.scope,'transcribe',{'aweme_ids':[RAW['aweme_id']]}))
        self.assertEqual(job['results'][0]['status'],'failed')
        post=self.store.get(self.scope,'post',RAW['aweme_id'])
        self.assertEqual(post['transcript'],'')
        self.assertEqual(post['processing_status'],'failed')

    def test_transcription_saves_segments_and_source(self):
        self.store.import_bundle(self.scope,{'account':user_record(USER),'posts':[post_record(RAW)]})
        self.jobs.model=Mock()
        self.jobs.model.transcribe.return_value=(iter([SimpleNamespace(start=0,end=2,text='真实语音的隔离测试替身')]),None)
        with patch.object(self.jobs,'download_post',return_value=Path(self.tmp.name)/'source.mp4'):
            job=self.wait(self.jobs.submit(self.scope,'transcribe',{'aweme_ids':[RAW['aweme_id']]}))
        self.assertEqual(job['status'],'completed')
        post=self.store.get(self.scope,'post',RAW['aweme_id'])
        self.assertEqual(post['transcript_source'],'local_whisper')
        self.assertTrue((Path(self.tmp.name)/'segments.json').is_file())


class AutoSession(FakeSession):
    def __init__(self,scope,directory):
        super().__init__(scope,directory)
        self.started=False
        self.context=True
    def login(self):
        self.started=True
        return self.public_status()
    verify=login
    def pump(self):
        if self.started:self.identity=user_record(USER)
    def public_status(self):
        return {'status':'connected' if self.identity else 'waiting_login','identity':self.identity,'message':'自动登录测试','browser_open':True}


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='linsi-auto-login-')
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':self.tmp.name,'KNOWLEDGE_VAULT_DIR':str(Path(self.tmp.name)/'vault')})
        self.env.start()
        self.store=Store(self.tmp.name)
        self.scope=self.store.workspace()['service_account_key']
        self.jobs=Jobs(self.store,AutoSession)
    def tearDown(self):
        self.jobs.close();self.env.stop();self.tmp.cleanup()
    def wait_flow(self,expected):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            flow=self.jobs.state(self.scope).get('workflow') or {}
            if flow.get('status')==expected:return flow
            time.sleep(.03)
        self.fail('Auto flow did not reach '+expected)
    def test_scan_automatically_binds_and_syncs_once(self):
        self.jobs.submit(self.scope,'connect')
        self.wait_flow('completed')
        self.assertEqual(self.store.bound_account(self.scope)['sec_uid'],USER['sec_uid'])
        self.assertEqual(len(self.store.records(self.scope,'post')),1)
        time.sleep(.7)
        self.assertEqual(len([j for j in self.store.records(self.scope,'job') if j['action']=='sync_mine']),1)
    def test_different_login_does_not_replace_existing_binding(self):
        old={**user_record(USER),'sec_uid':'another-existing-account','url':'https://www.douyin.com/user/another-existing-account'}
        self.store.bind_my_account(self.scope,old)
        self.jobs.submit(self.scope,'connect')
        self.wait_flow('account_mismatch')
        self.assertEqual(self.store.bound_account(self.scope)['sec_uid'],old['sec_uid'])
        self.assertEqual(self.store.records(self.scope,'post'),[])
    def test_collecting_competitor_does_not_trigger_own_account_binding(self):
        self.jobs.submit(self.scope,'collect',{'link':'https://www.douyin.com/user/'+USER['sec_uid']})
        time.sleep(.7)
        self.assertIsNone(self.store.bound_account(self.scope))
        self.assertFalse(any(j['action']=='sync_mine' for j in self.store.records(self.scope,'job')))
    def test_failed_read_keeps_failure_visible_without_works(self):
        with patch.object(AutoSession,'collect',side_effect=InputError('平台暂不可读')):
            self.jobs.submit(self.scope,'connect')
            flow=self.wait_flow('failed')
        self.assertEqual(flow['message'],'平台暂不可读')
        self.assertEqual(self.store.records(self.scope,'post'),[])
    def test_changed_author_is_not_saved_as_my_works(self):
        bundle={'account':{**user_record(USER),'sec_uid':'different-author'},'posts':[]}
        with patch.object(AutoSession,'collect',return_value=bundle):
            self.jobs.submit(self.scope,'connect')
            self.wait_flow('failed')
        self.assertEqual(self.store.records(self.scope,'post'),[])


class DownloadTests(unittest.TestCase):
    def test_html_response_cannot_be_saved_as_video(self):
        from linsi.media import download_video
        with tempfile.TemporaryDirectory() as directory:
            response=Mock()
            response.headers.get_content_type.return_value='text/html'
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            path=Path(directory)/'source.mp4'
            with patch('linsi.media.open_media',return_value=response):
                with self.assertRaises(InputError):download_video('https://example.douyinvod.com/a',path,lambda n:None)
            self.assertFalse(path.exists())

    def test_cancelled_download_retains_partial_without_success_file(self):
        from linsi.media import download_video
        from linsi.jobs import Cancelled
        with tempfile.TemporaryDirectory() as directory:
            response=Mock()
            response.headers.get_content_type.return_value='video/mp4'
            response.read.side_effect=[b'\x00\x00\x00\x18ftypmp42'+b'x'*40,b'']
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            path=Path(directory)/'source.mp4'
            with patch('linsi.media.open_media',return_value=response):
                with self.assertRaises(Cancelled):download_video('https://example.douyinvod.com/a',path,Mock(side_effect=Cancelled))
            self.assertFalse(path.exists())
            self.assertTrue(path.with_suffix('.part').is_file())


if __name__=='__main__':unittest.main()
