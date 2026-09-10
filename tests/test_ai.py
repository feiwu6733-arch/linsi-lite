import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import Server
from linsi.ai_api import AIConfig, AIClient
from linsi.ai_runner import AIRunner
from linsi.store import InputError, Store
from linsi.research import Research


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get('Authorization'), None))
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(json.dumps({'data':[{'id':'test-model'}]}).encode())
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        self.server.requests.append((self.path,self.headers.get('Authorization'),body))
        mode=self.server.mode
        if mode=='unauthorized':
            self.send_response(401);self.end_headers();self.wfile.write(b'do not expose test-secret in this response');return
        if mode=='redirect':
            self.send_response(307);self.send_header('Location',self.server.base+'/stolen');self.end_headers();return
        if mode=='bad-json':
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'not-json');return
        text='OK'
        if len(body['messages'])>1:
            source=json.loads(body['messages'][1]['content'].split('\n',1)[1])
            text='# 有来源的研究\n'+'\n'.join(p['aweme_id']+'：依据原文，先提出问题再列出步骤。' for p in source['sources'])
        if mode=='json':
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'choices':[{'message':{'content':text},'finish_reason':'stop'}]}).encode());return
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        if mode=='heartbeat':
            self.wfile.write(b'\xef\xbb\xbf: keep-alive\r\n\r\ndata: \n\n');self.wfile.flush()
        parts=[{'choices':[{'delta':{'content':text[:len(text)//2]}}]}, {'choices':[{'delta':{'content':text[len(text)//2:]},'finish_reason':'length' if mode=='truncated' else 'stop'}]}]
        if mode=='empty':parts=[{'choices':[{'delta':{'reasoning_content':'not a final answer'},'finish_reason':'stop'}]}]
        if mode=='interrupted':parts=parts[:1]
        for event in parts:
            encoded=json.dumps(event,indent=2) if mode=='heartbeat' else json.dumps(event)
            self.wfile.write(('\n'.join('data: '+line for line in encoded.splitlines())+'\n\n').encode());self.wfile.flush()
        if mode!='interrupted':self.wfile.write(b'data: [DONE]\n\n')


class Provider(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self):
        super().__init__(('127.0.0.1',0),ProviderHandler)
        self.base=f'http://127.0.0.1:{self.server_port}/v1';self.mode='stream';self.requests=[]
        self.thread=threading.Thread(target=self.serve_forever,daemon=True);self.thread.start()
    def close(self):self.shutdown();self.server_close();self.thread.join()


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='linsi-ai-tests-');self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'LINSI_DATA_DIR':self.tmp.name,'KNOWLEDGE_VAULT_DIR':str(self.root/'vault')});self.env.start()
        self.provider=Provider();self.config=AIConfig(self.root)
        self.config.save({'base_url':self.provider.base,'model':'test-model','api_key':'test-secret'})
        self.client=AIClient(self.config)
    def tearDown(self):self.provider.close();self.env.stop();self.tmp.cleanup()
    def test_secret_never_returned_and_blank_key_preserves(self):
        result=self.config.save({'base_url':self.provider.base,'model':'other-model','api_key':''})
        self.assertNotIn('api_key',result);self.assertNotIn('test-secret',json.dumps(result));self.assertTrue(result['has_key'])
        with self.assertRaises(InputError):self.config.save({'base_url':'https://different.example/v1','model':'new'})
        self.config.save({'base_url':self.provider.base,'model':'test-model','clear_key':True});self.assertFalse(self.config.public()['has_key'])
    def test_url_validation_and_completion_suffix(self):
        for url in ['http://external.example/v1','https://name:secret@host/v1','https://example.com?key=bad','https://example.com/\nheader']:
            with self.assertRaises(InputError):self.config.save({'base_url':url,'model':'test'})
        c=self.config.save({'base_url':self.provider.base+'/chat/completions','model':'test-model'})
        self.assertEqual(c['base_url'],self.provider.base)
    def test_model_discovery_without_selected_model(self):
        self.config.save({'base_url':self.provider.base,'model':''})
        self.assertEqual(self.client.models(),['test-model']);self.assertFalse(self.config.public()['configured'])
    def test_real_http_stream_and_json_compatible_response(self):
        progress=[];self.assertEqual(self.client.complete([{'role':'user','content':'OK'}],progress=progress.append),'OK')
        self.assertTrue(progress);self.assertEqual(self.provider.requests[0][0],'/v1/chat/completions');self.assertEqual(self.provider.requests[0][1],'Bearer test-secret')
        self.provider.mode='json';self.assertTrue(self.client.test()['ok'])
    def test_stream_heartbeat_bom_empty_and_multiline_events(self):
        self.provider.mode='heartbeat';updates=[]
        self.assertEqual(self.client.complete([{'role':'user','content':'OK'}],progress=updates.append),'OK')
        self.assertIn(0,updates);self.assertEqual(updates[-1],2)
    def test_domain_errors_are_not_misreported_as_incompatible_format(self):
        with self.assertRaisesRegex(InputError,'已停止'):
            self.client.complete([{'role':'user','content':'OK'}],cancelled=lambda:True)
        self.provider.mode='truncated'
        with self.assertRaisesRegex(InputError,'输出未完整结束'):self.client.test()
        self.provider.mode='stream'
        with self.assertRaisesRegex(InputError,'模拟超时'):
            self.client.complete([{'role':'user','content':'OK'}],progress=lambda n:(_ for _ in ()).throw(InputError('模拟超时')))
    def test_deepseek_modes_only_apply_to_official_endpoint(self):
        self.config.save({'base_url':'https://api.deepseek.com','model':'deepseek-v4-flash','api_key':'test-key'})
        with patch.object(self.client,'request',return_value={'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]}) as call:
            self.client.test();self.assertEqual(call.call_args.args[1]['thinking'],{'type':'disabled'})
        self.config.save({'base_url':'https://api.deepseek.com','model':'deepseek-v4-flash','thinking_mode':'deep'})
        with patch.object(self.client,'request',return_value={'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]}) as call:
            self.client.test();self.assertEqual(call.call_args.args[1]['thinking'],{'type':'enabled'})
    def test_retry_batch_report_only_processes_the_selected_report(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];ids=['7600000000000051011','7600000000000051012']
        store.import_bundle(scope,{'account':{'sec_uid':'only-current','name':'当前报告'},'posts':[{'aweme_id':i,'title':i,'transcript':'测试文案'} for i in ids]})
        research=Research(store,None,self.provider.base,start=False);runner=AIRunner(research,start=False)
        b=research.create(scope,{'mode':'breakdown','aweme_ids':ids})
        research.confirm(scope,b['id'],{'accounts':[{'sec_uid':'only-current','items':[{'aweme_id':i} for i in ids]}]});research.tick(store.get(scope,'research_batch',b['id']))
        reports=research.get(scope,b['id'])['accounts'][0]['report_ids'];first,second=reports[ids[0]],reports[ids[1]]
        store.merge(scope,'research_batch',b['id'],{'status':'cancelled','ai_auto':False})
        queued=runner.enable_report(scope,second);self.assertTrue(queued['api_requested']);runner.enable_report(scope,second)
        runner.process_report(scope,second);self.assertEqual(store.get(scope,'report',second)['status'],'completed')
        self.assertEqual(store.get(scope,'report',first)['status'],'awaiting_ai');self.assertFalse(store.get(scope,'research_batch',b['id'])['ai_auto']);self.assertEqual(len(self.provider.requests),1)
        runner.close();research.close()
    def test_stop_current_does_not_cancel_sibling_reports(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];ids=['7600000000000051021','7600000000000051022']
        store.import_bundle(scope,{'account':{'sec_uid':'stop-one','name':'单份停止'},'posts':[{'aweme_id':i,'title':i,'transcript':'测试文案'} for i in ids]})
        research=Research(store,None,self.provider.base,start=False);runner=AIRunner(research,start=False)
        b=research.create(scope,{'mode':'breakdown','aweme_ids':ids});research.confirm(scope,b['id'],{'accounts':[{'sec_uid':'stop-one','items':[{'aweme_id':i} for i in ids]}]});research.tick(store.get(scope,'research_batch',b['id']))
        reports=research.get(scope,b['id'])['accounts'][0]['report_ids'];runner.enable(scope,b['id']);runner.cancel_report(scope,reports[ids[0]])
        runner.process_batch(scope,b['id']);self.assertEqual(store.get(scope,'report',reports[ids[0]])['status'],'paused');self.assertEqual(store.get(scope,'report',reports[ids[1]])['status'],'completed');self.assertTrue(store.get(scope,'research_batch',b['id'])['ai_auto'])
        runner.close();research.close()
    def test_errors_truncation_empty_and_disconnect_not_success(self):
        for mode in ['unauthorized','bad-json','truncated','empty','interrupted']:
            self.provider.mode=mode
            with self.assertRaises(InputError) as ctx:self.client.test()
            self.assertNotIn('test-secret',str(ctx.exception))
    def test_redirect_does_not_forward_secret_and_cancel_makes_no_request(self):
        self.provider.mode='redirect'
        with self.assertRaises(InputError):self.client.test()
        self.assertEqual(len(self.provider.requests),1)
        with self.assertRaises(InputError):self.client.complete([{'role':'user','content':'OK'}],cancelled=lambda:True)
        self.assertEqual(len(self.provider.requests),1)
    def test_report_api_execution_saves_bound_result_and_is_idempotent(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];identity='7600000000000050001'
        store.import_bundle(scope,{'account':{'sec_uid':'ai-test','name':'API测试'},'posts':[{'aweme_id':identity,'title':'来源','transcript':'提出一个问题，再给出步骤。'}]})
        research=Research(store,None,self.provider.base,start=False);runner=AIRunner(research,start=False)
        r=store.create_report(scope,{'sec_uid':'ai-test','aweme_ids':[identity]})
        runner.enable_report(scope,r['id']);runner.enable_report(scope,r['id']);runner.process_report(scope,r['id'])
        saved=store.get(scope,'report',r['id']);self.assertEqual(saved['status'],'completed');self.assertIn(identity,saved['markdown']);self.assertFalse(saved['api_requested'])
        runner.enable_report(scope,r['id']);self.assertEqual(len(self.provider.requests),1)
        runner.close();research.close()
    def test_old_waiting_reports_migrate_without_touching_snapshot(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];identity='7600000000000050002'
        store.import_bundle(scope,{'account':{'sec_uid':'legacy','name':'历史报告'},'posts':[{'aweme_id':identity,'title':'来源','transcript':'历史文案'}]})
        r=store.create_report(scope,{'sec_uid':'legacy','aweme_ids':[identity]});store.merge(scope,'report',r['id'],{'status':'awaiting_codex'})
        research=Research(store,None,self.provider.base,start=False)
        saved=store.get(scope,'report',r['id']);self.assertEqual(saved['status'],'awaiting_ai');self.assertEqual(saved['snapshot'],r['snapshot']);self.assertEqual(saved['source_digest'],r['source_digest']);research.close()
    def test_cancel_during_response_does_not_save_report(self):
        store=Store(self.root);scope=store.workspace()['service_account_key'];identity='7600000000000050003'
        store.import_bundle(scope,{'account':{'sec_uid':'cancel-test','name':'取消验证'},'posts':[{'aweme_id':identity,'title':'来源','transcript':'测试文案'}]})
        research=Research(store,None,self.provider.base,start=False);runner=AIRunner(research,start=False)
        r=store.create_report(scope,{'sec_uid':'cancel-test','aweme_ids':[identity]});runner.enable_report(scope,r['id'])
        def stop_during_request(packet,cancelled,progress):
            runner.cancel_report(scope,r['id']);return '# 本结果不能写入\n'+identity
        with patch.object(runner,'generate',side_effect=stop_during_request):runner.process_report(scope,r['id'])
        saved=store.get(scope,'report',r['id']);self.assertEqual(saved['status'],'paused');self.assertFalse(saved['api_requested']);self.assertEqual(saved['markdown'],r['markdown']);runner.close();research.close()


if __name__=='__main__':unittest.main()
