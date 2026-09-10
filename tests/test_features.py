import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from unittest.mock import patch, Mock
from app import Server
from linsi.douyin import comment_record, DouyinSession
from linsi.media import validate_url
from linsi.store import InputError
from linsi.jobs import Jobs, Cancelled


class ParsingTests(unittest.TestCase):
    def test_comment_video_identity_and_top_level_parent(self):
        raw={'cid':'7600000000000000999','aweme_id':'7600000000000000001','text':'真实评论','reply_id':'0','digg_count':0,'user':{'nickname':'读者'}}
        row=comment_record(raw,'7600000000000000001')
        self.assertEqual(row['parent_comment_id'],'')
        self.assertEqual(row['likes'],0)
        self.assertIsNone(comment_record(raw,'7600000000000000002'))
    def test_unrelated_comment_response_is_not_collected(self):
        session=DouyinSession('scope','unused');session.comment_target='7600000000000000001'
        response=Mock(url='https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=7600000000000000002')
        session.capture(response)
        response.json.assert_not_called()
    def test_fake_dns_only_for_approved_media_hosts(self):
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('198.18.1.29',443))]):
            validate_url('https://p3-pc-sign.douyinpic.com/image')
            with self.assertRaises(InputError):validate_url('https://untrusted.example/image')
        for address in ['127.0.0.1','10.0.0.1','192.168.1.1','169.254.169.254']:
            with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',(address,443))]):
                with self.assertRaises(InputError):validate_url('https://p3-pc-sign.douyinpic.com/image')


class FeatureHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='linsi-features-')
        cls.env=patch.dict(os.environ,{'LINSI_DATA_DIR':cls.tmp.name,'KNOWLEDGE_VAULT_DIR':str(Path(cls.tmp.name)/'vault')});cls.env.start()
        cls.server=Server(0,cls.tmp.name);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.store=cls.server.store;cls.scope=cls.store.workspace()['service_account_key']
        cls.id='7600000000000000001'
        cls.store.import_bundle(cls.scope,{'account':{'sec_uid':'feature-account','name':'测试账号'},'posts':[{'aweme_id':cls.id,'title':'测试视频'}]})
        relative=Path('media')/cls.scope/'sample.mp4';file=Path(cls.tmp.name)/relative;file.parent.mkdir(parents=True);file.write_bytes(b'\0\0\0\x18ftypmp42'+b'x'*100)
        cls.store.merge(cls.scope,'post',cls.id,{'media_path':relative.as_posix()})
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.env.stop();cls.tmp.cleanup()
    def request(self,path,headers={}):
        try:
            with urlopen(Request(self.base+path,headers=headers),timeout=4) as r:return r.status,r.headers,r.read()
        except HTTPError as e:
            with e:return e.code,e.headers,e.read()
    def test_preview_supports_seek_and_rejects_invalid_range(self):
        path=f'/api/media/preview?service_account_key={self.scope}&aweme_id={self.id}'
        status,headers,data=self.request(path,{'Range':'bytes=4-11'})
        self.assertEqual(status,206);self.assertEqual(data,b'ftypmp42');self.assertEqual(headers['Content-Range'],'bytes 4-11/112')
        self.assertEqual(self.request(path,{'Range':'bytes=200-'})[0],416)
        self.assertEqual(self.request(path,{'Range':'bytes=0-1,4-5'})[0],416)
        self.assertEqual(self.request(path,{'Range':'bytes=-4'})[2],b'xxxx')
    def test_video_cannot_be_read_from_other_workspace(self):
        scope=self.store.workspace()['service_account_key']
        self.assertEqual(self.request(f'/api/media/preview?service_account_key={scope}&aweme_id={self.id}')[0],400)
    def test_comments_export_is_scoped_and_preserves_text(self):
        self.store.merge(self.scope,'comment',self.id+':c1',{'comment_id':'c1','aweme_id':self.id,'text':'<script>不执行</script>','likes':2})
        status,headers,data=self.request(f'/api/comments/export?service_account_key={self.scope}&aweme_id={self.id}')
        self.assertEqual(status,200);self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(json.loads(data)['comments'][0]['text'],'<script>不执行</script>')
    def test_model_cancel_terminates_owned_child(self):
        process=Mock();process.poll.return_value=None
        worker=self.server.jobs
        with patch('subprocess.Popen',return_value=process),patch.object(worker,'check',side_effect=Cancelled):
            with self.assertRaises(Cancelled):worker.prepare_model({'service_account_key':self.scope,'id':'cancel-model'})
        process.terminate.assert_called_once();process.wait.assert_called_once()


if __name__=='__main__':unittest.main()
