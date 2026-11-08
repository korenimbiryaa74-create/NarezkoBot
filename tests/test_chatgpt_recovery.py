import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
# Real websocket-client is installed by the Windows installer; this test mocks CDP.
try:
    import websocket
except ModuleNotFoundError:
    import sys, types
    fake=types.ModuleType('websocket')
    fake.WebSocketConnectionClosedException=type('WebSocketConnectionClosedException',(Exception,),{})
    fake.WebSocketTimeoutException=type('WebSocketTimeoutException',(Exception,),{})
    sys.modules['websocket']=fake
import chatgpt_web
import pipeline
from service import Service
from settings import Settings
from chatgpt_finder import ChatGPTFinder
from transcribe import TranscribeError

ANSWER='''Анализ завершен
{"reactions":[{"start":"00:00:10.000","end":"00:00:30.000","title":"Просмотр видео","confidence":"high"}]}'''

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def test_text_answer_ignores_unrelated_unloaded_images(self):
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        web.tab=object();web._read_answer_state=lambda:{'texts':[ANSWER],'text':ANSWER,'imgs':[{'u':'some-ui-image','ok':False}],'busy':False,'n':1}
        received=[];web.status_callback=received.append
        with patch('chatgpt_web.time.sleep'), patch.object(web,'_download',side_effect=AssertionError('must not download')):
            text,images=web._wait_answer(until=lambda x:'"reactions"' in x)
        self.assertEqual(text,ANSWER);self.assertEqual(images,[]);self.assertTrue(received)
    def test_no_native_file_dialog_if_interception_fails(self):
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        class Tab:
            def __init__(self):self.clicks=0
            def js(self,expr):
                if 'data-twl-file' in expr: return 0
                self.clicks+=1;return False
            def call(self,*args,**kwargs):raise RuntimeError('interception unavailable')
        web.tab=Tab();web._check_upload_quota=lambda:None
        with patch('chatgpt_web.time.sleep'):
            self.assertFalse(web._attach('test.txt'))
        self.assertEqual(web.tab.clicks,0)
    def test_file_picker_menu_clicked_at_most_once(self):
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        class Tab:
            def __init__(self):self.menu_clicks=0;self.intercepts=0
            def js(self,expr):
                if 'data-twl-file' in expr:return 0
                if '[role="menuitem"]' in expr:self.menu_clicks+=1;return True
                return None
            def call(self,*args,**kwargs):self.intercepts+=1
        web.tab=Tab();web._check_upload_quota=lambda:None
        with patch('chatgpt_web.time.sleep'):
            self.assertFalse(web._attach('test.txt'))
        self.assertEqual(web.tab.menu_clicks,1);self.assertEqual(web.tab.intercepts,1)
    def test_manual_answer_skips_browser(self):
        finder=ChatGPTFinder(self.root,web_factory=lambda **kw: (_ for _ in ()).throw(AssertionError('browser used')))
        rows=finder('v.mp4',[{'start':0,'end':60,'text':'speech'}],60,'name',{'min_reaction_minutes':0,'_manual_reactions':ANSWER},lambda *a:None)
        self.assertEqual([(r['start'],r['end']) for r in rows],[(10,30)])
    def test_save_and_use_manual_answer_for_matching_failed_job(self):
        settings=Settings(self.root/'config.json');service=Service(self.root,settings,None)
        job,_=service.store.add('123','name');claimed=service.store.claim();service.store.update_stage(job['id'],'Поиск реакций');service.store.finish(job['id'],'failed','interrupted')
        self.assertEqual(service.save_manual_reactions(job['id'],ANSWER),1)
        row=service.store.list_jobs()[0];seen=[]
        def finder(src,segs,duration,login,cfg,progress):
            seen.append(cfg.get('_manual_reactions')); return []
        def download(vod,quality,out,**kw):Path(out).write_bytes(b'x'*2000)
        def speech(*a,**kw):return ([{'start':0,'end':2,'text':'x'}],60)
        engine=pipeline.Pipeline(self.root,finder,download=download,speech=speech)
        with patch.object(pipeline,'media_duration',return_value=60):engine.run(row,{'min_reaction_minutes':0})
        self.assertEqual(seen,[ANSWER])
        # A response cannot leak into another job even if ID and cache filename are reused.
        other=dict(row,streamer='different')
        with patch.object(pipeline,'media_duration',return_value=60):engine.run(other,{'min_reaction_minutes':0})
        self.assertIsNone(seen[-1])
    def test_interrupted_response_becomes_failed_not_replayed(self):
        svc=Service(self.root,Settings(self.root/'config.json'),None);job,_=svc.store.add('123','name')
        svc.store.claim();svc.store.update_stage(job['id'],'Поиск реакций')
        self.assertEqual(svc.recover_interrupted(),1)
        self.assertEqual(svc.store.list_jobs()[0]['state'],'failed')
        self.assertEqual(svc.save_manual_reactions(job['id'],ANSWER),1)
        self.assertFalse(svc.worker_once())
    def test_rejects_invalid_response_and_live_job(self):
        svc=Service(self.root,Settings(self.root/'config.json'),None);job,_=svc.store.add('123','name')
        with self.assertRaises(ValueError):svc.save_manual_reactions(job['id'],ANSWER)
        svc.store.claim();svc.store.finish(job['id'],'failed','x')
        for text in ('','{"reactions":[', '{"reactions":[{"start":"bad","end":"00:00:30"}]}', '{"reactions":[{"start":1,"end":9},{"start":8,"end":15}]}'):
            with self.assertRaises(ValueError):svc.save_manual_reactions(job['id'],text)
        self.assertFalse(pipeline.manual_answer_path(self.root,job['id']).exists())
    def test_out_of_bounds_manual_answer_rejected_before_render(self):
        finder=ChatGPTFinder(self.root,web_factory=lambda **kw: (_ for _ in ()).throw(AssertionError('browser used')))
        answer='{"reactions":[{"start":1,"end":100,"title":"x"}]}'
        with self.assertRaises(Exception):finder('v',[{'start':0,'end':60,'text':'speech'}],60,'n',{'min_reaction_minutes':0,'_manual_reactions':answer},lambda *a:None)
