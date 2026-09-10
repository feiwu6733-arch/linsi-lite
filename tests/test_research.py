import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen, Request

from app import Server
from linsi.ai_runner import AIRunner
from linsi.research import Research, recommend
from linsi.store import Store, InputError, now



class Queue:
    def __init__(self, store):self.store=store;self.ids=[]
    def submit(self, scope, action, payload):
        rid='test-job-'+str(len(self.ids));self.ids.append(rid)
        return self.store.merge(scope,'job',rid,{'id':rid,'service_account_key':scope,'action':action,'payload':payload,'status':'queued','created_at':now()})
    def cancel(self, scope, rid):self.store.merge(scope,'job',rid,{'status':'cancelled'})


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='linsi-research-tests-')
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':self.tmp.name,'KNOWLEDGE_VAULT_DIR':str(Path(self.tmp.name)/'vault')});self.env.start()
        self.store=Store(self.tmp.name);self.scope=self.store.workspace()['service_account_key'];self.other=self.store.workspace()['service_account_key']
        self.ids=['7600000000000001001','7600000000000001002','7600000000000001003']
        for sec in ('account-one','account-two'):
            self.store.import_bundle(self.scope,{'account':{'sec_uid':sec,'name':sec},'posts':[
                {'aweme_id':self.ids[0 if sec=='account-one' else 2],'title':'视频测试','transcript':'真实来源测试文案','likes':100}]})
        self.store.import_bundle(self.scope,{'account':{'sec_uid':'account-one','name':'account-one'},'posts':[{'aweme_id':self.ids[1],'title':'图文测试','transcript':''}]})
        self.store.merge(self.scope,'post',self.ids[1],{'media_type':'gallery'})
        self.queue=Queue(self.store);self.research=Research(self.store,self.queue,'http://127.0.0.1:5030',start=False)
    def tearDown(self):self.research.close();self.env.stop();self.tmp.cleanup()
    def create(self,mode='research',ids=None):return self.research.create(self.scope,{'mode':mode,'aweme_ids':ids or self.ids})
    def confirm(self,b,comments=False):
        return self.research.confirm(self.scope,b['id'],{'accounts':[{'sec_uid':a['sec_uid'],'items':[{'aweme_id':i['aweme_id'],'mode':'breakdown'} for i in a['items'] if i['selected']]} for a in b['accounts']], 'comments':comments})
    def tick(self,b):self.research.tick(self.store.get(self.scope,'research_batch',b['id']));return self.research.get(self.scope,b['id'])
    def complete_reports(self,b):
        for task in self.research.packet(self.scope,b['id'])['tasks']:
            content='# 有依据的分析\n'+'\n'.join(p['aweme_id']+'：这条来源的内容结构分析。' for p in task['sources'])
            self.store.save_report(self.scope,{'id':task['report_id'],'source_digest':task['source_digest'],'markdown':content})
    def test_selection_transparent_and_deduplicated(self):
        posts=[{'aweme_id':str(i),'published_at':f'2026-09-{i+1:02}','likes':10-i,'comments':i%3,'collects':i%2} for i in range(12)]
        chosen=recommend(posts,6);self.assertEqual(len(chosen),6);self.assertIn('11',chosen);self.assertIn('0',chosen)
    def test_create_confirm_are_idempotent_and_scoped(self):
        b=self.create();self.assertEqual(self.create()['id'],b['id']);self.confirm(b);self.confirm(b)
        self.assertEqual(len(self.store.records(self.scope,'research_batch')),1)
        with self.assertRaises(InputError):self.research.get(self.other,b['id'])
        with self.assertRaises(InputError):self.research.create(self.other,{'aweme_ids':self.ids})
    def test_mixed_gallery_transcribe_reuses_text_and_skips(self):
        b=self.create('transcribe');self.confirm(b);b=self.tick(b)
        self.assertEqual(b['status'],'completed');self.assertEqual(self.queue.ids,[])
        a=next(a for a in b['accounts'] if a['sec_uid']=='account-one');self.assertEqual(a['skipped_ids'],[self.ids[1]])
    def test_partial_account_does_not_block_other_account(self):
        b=self.create();self.confirm(b);b=self.tick(b)
        self.assertEqual(b['accounts'][0]['status'],'partial_ready')
        self.assertTrue(b['accounts'][1]['report_ids'])
        self.research.resume(self.scope,b['id'],partial=True);b=self.tick(b)
        self.assertEqual(sum(len(a['report_ids']) for a in b['accounts']),2)
        self.complete_reports(b);b=self.tick(b)
        self.assertTrue(all(a.get('summary_id') for a in b['accounts']))
        self.complete_reports(b);b=self.tick(b);self.assertEqual(b['status'],'partial')
        self.assertIn(self.ids[0],self.research.export(self.scope,b['id']))
        for a in b['accounts']:
            r=self.store.get(self.scope,'report',a['summary_id']);self.assertTrue(all(p['sec_uid']==a['sec_uid'] for p in r['snapshot']['sources']))
    def test_multiple_breakdowns_are_distinct_and_no_summary(self):
        b=self.create('breakdown',[self.ids[0],self.ids[2]]);self.confirm(b);b=self.tick(b)
        self.assertEqual(len(self.research.packet(self.scope,b['id'])['tasks']),2)
        self.complete_reports(b);b=self.tick(b);self.assertEqual(b['status'],'completed')
        self.assertFalse(any(a.get('summary_id') for a in b['accounts']))
    def test_recovered_work_gets_new_summary_without_rewriting_old_result(self):
        b=self.create();self.confirm(b);self.tick(b);self.research.resume(self.scope,b['id'],partial=True);b=self.tick(b)
        self.complete_reports(b);b=self.tick(b);self.complete_reports(b);b=self.tick(b)
        old=self.store.get(self.scope,'report',b['accounts'][0]['summary_id'])
        self.assertEqual(len(old['snapshot']['completed_work_analyses']),1)
        self.store.save_transcript(self.scope,{'aweme_id':self.ids[1],'transcript':'现在补齐图文正文。'})
        self.research.resume(self.scope,b['id']);b=self.tick(b);self.complete_reports(b);b=self.tick(b)
        self.assertNotEqual(b['accounts'][0]['summary_id'],old['id'])
        new=self.store.get(self.scope,'report',b['accounts'][0]['summary_id'])
        self.assertEqual(len(new['snapshot']['sources']),2)
        self.assertEqual(self.store.get(self.scope,'report',old['id'])['markdown'],old['markdown'])
    def test_comment_failure_is_visible_and_retry_reuses_completed_breakdown(self):
        self.store.merge(self.scope,'post',self.ids[0],{'comments_status':'completed','comments_has_more':True})
        b=self.create('breakdown',[self.ids[0]]);self.confirm(b,comments=True);b=self.tick(b)
        self.assertEqual(len(self.queue.ids),1)
        self.store.merge(self.scope,'job',self.queue.ids[0],{'status':'failed','message':'需要验证'})
        self.store.merge(self.scope,'post',self.ids[0],{'comments_status':'failed','comments_error':'需要验证'})
        b=self.tick(b);self.complete_reports(b);b=self.tick(b)
        self.assertEqual(b['status'],'partial');report_id=b['accounts'][0]['report_ids'][self.ids[0]]
        self.assertIn('需要验证',b['accounts'][0]['items'][0]['comment_warning'])
        self.research.resume(self.scope,b['id']);b=self.tick(b);self.assertEqual(len(self.queue.ids),2)
        self.store.merge(self.scope,'job',self.queue.ids[-1],{'status':'completed'})
        self.store.merge(self.scope,'post',self.ids[0],{'comments_status':'completed','comments_has_more':False,'comments_error':''})
        b=self.tick(b);self.assertEqual(b['status'],'completed')
        self.assertEqual(b['accounts'][0]['report_ids'][self.ids[0]],report_id)
    def test_scan_checks_account_identity_before_sample_review(self):
        b=self.research.create(self.scope,{'mode':'research','sec_uids':['account-one'],'refresh':True})
        b=self.tick(b);self.assertEqual(b['status'],'scanning')
        self.store.merge(self.scope,'job',self.queue.ids[0],{'status':'completed','result':{'sec_uid':'wrong-account'},'message':'读取到了其他账号'})
        b=self.tick(b);self.assertEqual(b['status'],'scan_failed')
        self.research.resume(self.scope,b['id']);b=self.tick(b)
        self.store.merge(self.scope,'job',self.queue.ids[-1],{'status':'completed','result':{'sec_uid':'account-one'}})
        self.assertEqual(self.tick(b)['status'],'review_ready')
    def test_failed_jobs_retry_only_missing_work(self):
        self.store.save_transcript(self.scope,{'aweme_id':self.ids[0],'transcript':''})
        b=self.create('transcribe',[self.ids[0],self.ids[2]]);self.confirm(b);b=self.tick(b)
        self.assertEqual(len(self.queue.ids),1);j=self.store.get(self.scope,'job',self.queue.ids[0]);self.assertEqual(j['payload']['aweme_ids'],[self.ids[0]])
        self.store.merge(self.scope,'job',j['id'],{'status':'failed','message':'测试失败'});b=self.tick(b);self.assertEqual(b['status'],'partial_ready')
        self.research.resume(self.scope,b['id']);b=self.tick(b);self.assertEqual(len(self.queue.ids),2)
        self.store.save_transcript(self.scope,{'aweme_id':self.ids[0],'transcript':'已恢复'})
        self.store.merge(self.scope,'job',self.queue.ids[-1],{'status':'completed'});b=self.tick(b);self.assertEqual(b['status'],'completed')
    def test_cancel_and_restart_preserve_completed_text(self):
        self.store.save_transcript(self.scope,{'aweme_id':self.ids[0],'transcript':''})
        b=self.create('transcribe',[self.ids[0]]);self.confirm(b);self.tick(b);self.research.cancel(self.scope,b['id'])
        self.assertEqual(self.store.get(self.scope,'job',self.queue.ids[0])['status'],'cancelled')
        self.assertEqual(self.tick(b)['status'],'cancelled')
        self.research.resume(self.scope,b['id']);self.tick(b)
        restored=Research(self.store,self.queue,'http://127.0.0.1:5030',start=False)
        self.assertEqual(restored.get(self.scope,b['id'])['status'],'interrupted')
        self.assertEqual(self.store.get(self.scope,'post',self.ids[2])['transcript'],'真实来源测试文案')
    def test_source_digest_rejects_wrong_report_and_snapshot_is_frozen(self):
        b=self.create('breakdown',[self.ids[0]]);self.confirm(b);b=self.tick(b)
        task=self.research.packet(self.scope,b['id'])['tasks'][0]
        self.store.save_transcript(self.scope,{'aweme_id':self.ids[0],'transcript':'后续修改'})
        self.assertEqual(task['sources'][0]['transcript'],'真实来源测试文案')
        with self.assertRaises(InputError):self.store.save_report(self.scope,{'id':task['report_id'],'source_digest':'wrong','markdown':self.ids[0]})
    def test_injected_unknown_selection_rejected_before_saving(self):
        b=self.create('breakdown',[self.ids[0]])
        with self.assertRaises(InputError):self.research.confirm(self.scope,b['id'],{'accounts':[{'sec_uid':'account-one','items':[{'aweme_id':self.ids[2]}]}]})
        self.assertEqual(self.research.get(self.scope,b['id'])['status'],'review_ready')
    def test_codex_adapter_writes_validated_results_and_stops_on_invalid(self):
        b=self.create('breakdown',[self.ids[0],self.ids[2]]);self.confirm(b);b=self.tick(b)
        runner=AIRunner(self.research,start=False)
        self.store.merge(self.scope,'research_batch',b['id'],{'ai_auto':True})
        with patch.object(runner,'generate',side_effect=lambda packet,cancelled,progress:'# 分析\n'+packet['sources'][0]['aweme_id']+'\n具体来源的结构与原文依据。'):
            runner.process_batch(self.scope,b['id'])
        self.assertEqual(self.tick(b)['status'],'completed')
        b2=self.create('breakdown',[self.ids[0]]);self.confirm(b2);self.tick(b2);self.store.merge(self.scope,'research_batch',b2['id'],{'ai_auto':True})
        with patch.object(runner,'generate',return_value='缺少来源标识的错误响应'):runner.process_batch(self.scope,b2['id'])
        state=self.research.get(self.scope,b2['id']);self.assertFalse(state['ai_auto']);self.assertTrue(state['ai_error'])
        self.assertEqual(self.research.packet(self.scope,b2['id'])['tasks'][0]['report_type'],'breakdown')
        runner.close()


class ResearchHTTPTests(unittest.TestCase):
    def test_api_handoff_and_export_are_scoped(self):
        with tempfile.TemporaryDirectory(prefix='linsi-research-http-') as tmp,patch.dict(os.environ,{'LINSI_DATA_DIR':tmp,'KNOWLEDGE_VAULT_DIR':str(Path(tmp)/'vault')}):
            server=Server(0,tmp);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                scope=server.store.workspace()['service_account_key'];aweme='7600000000000009011'
                server.store.import_bundle(scope,{'account':{'sec_uid':'http-batch','name':'HTTP测试'},'posts':[{'aweme_id':aweme,'title':'来源','transcript':'测试原文'}]})
                base=f'http://127.0.0.1:{server.server_port}'
                def post(path,body):
                    with urlopen(Request(base+path,data=json.dumps({'service_account_key':scope,**body}).encode(),headers={'Content-Type':'application/json','X-Linsi-Token':server.token})) as r:return json.load(r)
                b=post('/api/research/create',{'mode':'breakdown','aweme_ids':[aweme]})
                post('/api/research/confirm',{'id':b['id'],'accounts':[{'sec_uid':'http-batch','items':[{'aweme_id':aweme}]}]})
                with server.research.lock:server.research.tick(server.store.get(scope,'research_batch',b['id']))
                packet_path=Path(tmp)/'research-sources'/server.research.packet(scope,b['id'])['tasks'][0]['report_id']/'sources.json'
                self.assertTrue(packet_path.exists())
                from scripts.publish_report import publish
                output=packet_path.with_name('report.md');output.write_text('# 真实回写验收\n'+aweme+'：基于本条原文的分析。',encoding='utf-8')
                publish(packet_path,output,base)
                with urlopen(base+'/api/research/export?service_account_key='+scope+'&id='+b['id']) as r:self.assertIn(aweme,r.read().decode())
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
