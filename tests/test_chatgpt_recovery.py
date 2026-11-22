import json
import tempfile
import shutil
import subprocess
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
    def test_finished_placeholder_is_retryable_but_active_generation_is_not(self):
        from chatgpt_errors import ChatGPTIncompleteAnswer, ChatGPTWebError
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),timeout=100,verbose=False)
        for busy in (False,True):
            now=[1000];web._read_answer_state=lambda:{'text':'Анализ завершен','busy':busy,'n':1}
            def sleep(dt):now[0]+=dt
            with self.subTest(busy=busy),patch('chatgpt_web.time.time',side_effect=lambda:now[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
                with self.assertRaises(ChatGPTWebError) as error:web._wait_answer(until=lambda text:False)
                self.assertEqual(isinstance(error.exception,ChatGPTIncompleteAnswer),not busy)

    def test_send_steps_are_visible_without_sensitive_prompt(self):
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        class Tab:
            def js(self,expr): return [] if expr==web.JS_IMGS else {}
        web.tab=Tab();events=[];web.status_callback=events.append
        web.new_chat=lambda **kw:True;web._page_id=lambda:None
        web._read_answer_state=lambda:{'texts':[],'notices':[]}
        web._attach=lambda p:True;web._type_prompt=lambda p:True;web._send=lambda:True
        web._wait_answer=lambda *a:('answer',[]);web.dump_cookies=lambda:None
        with patch('chatgpt_web.time.sleep'):
            web._ask_locked('секретный текст', ['file.txt'],lambda text:True)
        joined=' '.join(events)
        for word in ('проверяю вход','прикрепляю транскрипт','ввожу запрос','отправка подтверждена'):
            self.assertIn(word,joined)
        self.assertNotIn('секретный текст',joined)

    @unittest.skipUnless(shutil.which('chromium'),'requires Chromium')
    def test_donation_answer_detected_in_fallback_dom(self):
        from html.parser import HTMLParser
        page=self.root/'test.html'
        page.write_text('<main><p>{&quot;donation_cuts&quot;:[{&quot;reaction&quot;:1}]}</p></main>'+
            '<script>const r='+chatgpt_web.ChatGPTWeb.JS_STATE+'; const p=document.createElement("pre");'+
            'p.id="result";p.textContent=JSON.stringify(r);document.body.appendChild(p);</script>',encoding='utf8')
        proc=subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30,check=True)
        class ResultParser(HTMLParser):
            def __init__(self):super().__init__();self.reading=False;self.result=''
            def handle_starttag(self,tag,attrs):
                if tag=='pre' and dict(attrs).get('id')=='result':self.reading=True
            def handle_endtag(self,tag):
                if tag=='pre':self.reading=False
            def handle_data(self,data):
                if self.reading:self.result+=data
        parser=ResultParser();parser.feed(proc.stdout);state=json.loads(parser.result)
        self.assertIn('donation_cuts',state['text']);self.assertGreater(state['fallbacks'],0)

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
                if 'composer-plus-btn' in expr:return True
                return None
            def call(self,*args,**kwargs):self.intercepts+=1
        web.tab=Tab();web._check_upload_quota=lambda:None
        with patch('chatgpt_web.time.sleep'):
            self.assertFalse(web._attach('test.txt'))
        self.assertEqual(web.tab.menu_clicks,1);self.assertEqual(web.tab.intercepts,1)
    def test_file_chooser_node_used_and_never_uploaded_twice(self):
        web=chatgpt_web.ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        class Tab:
            def __init__(self):self.calls=[];self.waits=0
            def clear_file_chooser(self):pass
            def wait_file_chooser(self,seconds):
                self.waits+=1
                return None if self.waits==1 else {'backendNodeId':77}
            def js(self,expr):
                if 'composer-plus-btn' in expr or '[role="menuitem"]' in expr:return True
                raise AssertionError('must not guess input')
            def call(self,method,**kw):self.calls.append((method,kw));return {}
        web.tab=Tab();web._check_upload_quota=lambda:None
        events=[];web.status_callback=events.append;web._wait_attached=lambda *a:False
        self.assertFalse(web._attach('test.txt'))
        writes=[c for c in web.tab.calls if c[0]=='DOM.setFileInputFiles']
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][1]['backendNodeId'],77)
        self.assertTrue(any('повторная загрузка не выполняется' in m for m in events))

    def test_chooser_event_survives_command_reply(self):
        webtab=chatgpt_web.Tab.__new__(chatgpt_web.Tab);webtab._id=0;webtab.timeout=5;webtab._file_chooser_events=[]
        class WS:
            def __init__(self): self.messages=[json.dumps({'method':'Page.fileChooserOpened','params':{'backendNodeId':123}}),json.dumps({'id':1,'result':{'value':True}})]
            def settimeout(self,t):pass
            def send(self,v):pass
            def recv(self):return self.messages.pop(0)
        webtab.ws=WS()
        self.assertEqual(webtab.call('Runtime.evaluate')['value'],True)
        self.assertEqual(webtab.wait_file_chooser()['backendNodeId'],123)

    @unittest.skipUnless(shutil.which('chromium'),'requires Chromium')
    def test_composer_file_input_preferred_and_attachment_near_form(self):
        from html.parser import HTMLParser
        page=self.root/'composer.html'
        js_pick=chatgpt_web.ChatGPTWeb.JS_PICK_FILE_INPUT % json.dumps('any')
        js_attached=chatgpt_web.ChatGPTWeb.JS_ATTACHED % json.dumps('transcript.txt')
        page.write_text('<input id="other" type="file"><section><form><textarea id="prompt-textarea"></textarea><input id="right" type="file" accept=".txt"></form><span>transcript.txt</span></section>'+
            '<script>const count='+js_pick+';const seen='+js_attached+'; const p=document.createElement("pre");'+
            'p.id="result";p.textContent=JSON.stringify({count,selected:document.querySelector("[data-twl-file]").id,seen});document.body.appendChild(p);</script>',encoding='utf8')
        proc=subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30,check=True)
        class P(HTMLParser):
            def __init__(self):super().__init__();self.reading=False;self.result=''
            def handle_starttag(self,tag,attrs):
                if tag=='pre' and dict(attrs).get('id')=='result':self.reading=True
            def handle_endtag(self,tag):
                if tag=='pre':self.reading=False
            def handle_data(self,data):
                if self.reading:self.result+=data
        parser=P();parser.feed(proc.stdout);data=json.loads(parser.result)
        self.assertEqual(data['selected'],'right');self.assertTrue(data['seen']['named'])

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
