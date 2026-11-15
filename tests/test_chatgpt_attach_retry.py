import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import subprocess
try:
    import websocket
except ModuleNotFoundError:
    import sys, types
    websocket=types.ModuleType('websocket')
    websocket.WebSocketConnectionClosedException=type('WebSocketConnectionClosedException',(Exception,),{})
    websocket.WebSocketTimeoutException=type('WebSocketTimeoutException',(Exception,),{})
    sys.modules['websocket']=websocket
import chatgpt_attach
from chatgpt_web import ChatGPTWeb, ChatGPTWebError, ChatGPTQuotaError, Tab

class AttachRetryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        self.web=ChatGPTWeb(str(root/'cookies'),str(root/'out'),profile=str(root/'profile'),verbose=False)
        self.events=[];self.web.status_callback=self.events.append
        self.web._wait_attached=lambda *args:True

    def test_delayed_composer_and_menu_choose_actual_file_input(self):
        class Fake:
            def __init__(self):self.plus=0;self.menu=0;self.calls=[];self.queue=0
            def call(self,m,**kw):self.calls.append((m,kw));return {}
            def clear_file_chooser(self):pass
            def wait_file_chooser(self,seconds):
                self.queue+=1
                return {'backendNodeId':71} if self.menu>=3 else None
            def js(self,expr):
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if expr==chatgpt_attach.JS_OPEN_ATTACH:self.plus+=1;return self.plus>=4
                if expr==chatgpt_attach.JS_CLICK_UPLOAD:self.menu+=1;return self.menu>=3
                if 'data-twl-file' in expr:return 0
                raise AssertionError(expr)
        self.web.tab=Fake()
        with patch('chatgpt_attach.time.sleep'):self.assertTrue(self.web._attach('test.txt'))
        self.assertEqual(self.web.tab.plus,4);self.assertEqual(self.web.tab.menu,3)
        writes=[x for x in self.web.tab.calls if x[0]=='DOM.setFileInputFiles']
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][1]['backendNodeId'],71)

    def test_late_page_input_used_as_alternate_once(self):
        class Fake:
            def __init__(self):self.fallback=0;self.calls=[]
            def call(self,m,**kw):self.calls.append((m,kw));return {}
            def js(self,e):
                if e==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if e==chatgpt_attach.JS_OPEN_ATTACH:return True
                if e==chatgpt_attach.JS_CLICK_UPLOAD:return False
                if 'data-twl-file' in e:self.fallback+=1;return 1 if self.fallback>=3 else 0
            def node_id(self,s):return 45
        self.web.tab=Fake()
        with patch('chatgpt_attach.time.sleep'):self.assertTrue(self.web._attach('test.txt'))
        writes=[x for x in self.web.tab.calls if x[0]=='DOM.setFileInputFiles']
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][1]['nodeId'],45)
        self.assertTrue(any('запасной путь' in x for x in self.events))

    def test_quota_during_wait_never_uploads(self):
        class Fake:
            def __init__(self):self.checks=0;self.calls=[]
            def call(self,m,**kw):self.calls.append(m)
            def js(self,e):
                if e==chatgpt_attach.JS_QUOTA:self.checks+=1;return {'blocked':self.checks>=6}
                if e==chatgpt_attach.JS_OPEN_ATTACH:return True
                return False
        self.web.tab=Fake()
        with patch('chatgpt_attach.time.sleep'),self.assertRaises(ChatGPTQuotaError):self.web._attach('test.txt')
        self.assertNotIn('DOM.setFileInputFiles',self.web.tab.calls)

    def test_no_repeat_after_uncertain_click(self):
        class Fake:
            def __init__(self):self.clicks=0;self.calls=[]
            def call(self,m,**kw):self.calls.append(m)
            def js(self,e):
                if e==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if e==chatgpt_attach.JS_OPEN_ATTACH:self.clicks+=1;raise RuntimeError('timeout')
        self.web.tab=Fake()
        with patch('chatgpt_attach.time.sleep'):self.assertFalse(self.web._attach('test.txt'))
        self.assertEqual(self.web.tab.clicks,1);self.assertNotIn('DOM.setFileInputFiles',self.web.tab.calls)

    def test_attachment_can_complete_after_old_45_second_limit(self):
        clock=[0.0]
        class Fake:
            def js(self,expr):
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                return {'named':clock[0]>=75, 'busy':False}
        self.web.tab=Fake()
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_web.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
            self.assertTrue(ChatGPTWeb._wait_attached(self.web,'transcript.txt',False))
        self.assertGreaterEqual(clock[0],75)
        self.assertTrue(any('ещё не отправлен' in e for e in self.events))

    def test_attachment_wait_respects_global_deadline_and_never_assumes_ready(self):
        clock=[0.0]; self.web._request_deadline=70
        class Fake:
            def js(self,expr):
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                return {'named':True, 'busy':True}
        self.web.tab=Fake()
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_web.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
            self.assertFalse(ChatGPTWeb._wait_attached(self.web,'transcript.txt',False))
        self.assertEqual(clock[0],70)

    def test_late_menu_load_has_more_than_20_seconds(self):
        clock=[0.0]
        class Fake:
            def __init__(self):self.writes=0
            def call(self,method,**kw):
                if method=='DOM.setFileInputFiles':self.writes+=1
                return {}
            def js(self,expr):
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if expr==chatgpt_attach.JS_OPEN_ATTACH:return clock[0]>=30
                if expr==chatgpt_attach.JS_CLICK_UPLOAD:return False
                if 'data-twl-file' in expr:return 1 if clock[0]>=30 else 0
            def node_id(self,selector):return 5
        self.web.tab=Fake()
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_attach.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_attach.time.sleep',side_effect=sleep):
            self.assertTrue(self.web._attach('test.txt'))
        self.assertGreaterEqual(clock[0],30);self.assertEqual(self.web.tab.writes,1)

    def test_never_reattaches_if_send_is_not_ready(self):
        class Fake:
            def js(self,expr):return [] if expr==self_web.JS_IMGS else {}
        self_web=self.web;self.web.tab=Fake();calls=[]
        self.web.new_chat=lambda **kw:True;self.web._page_id=lambda:1
        self.web._read_answer_state=lambda:{'texts':[],'notices':[]}
        self.web._attach=lambda p:calls.append(p) or True
        self.web._type_prompt=lambda p:True;self.web._send=lambda:False
        with patch('chatgpt_web.time.sleep'),self.assertRaises(ChatGPTWebError):
            self.web._ask_locked('prompt',['transcript.txt'])
        self.assertEqual(calls,['transcript.txt'])

    def _send_with_fake(self,text_after_click):
        clock=[0.0]
        class Fake:
            def __init__(self):self.clicks=0
            def call(self,*a,**kw):return {}
            def js(self,expr):
                if expr==self_web.JS_SEND_STATE:
                    text='prompt' if not self.clicks else text_after_click
                    return {'has':True,'disabled':False,'stop':False,'uploading':False,'text':text,'key':'a','hit':False,'users':0,'assistants':0}
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if self_web.JS_DISMISS in expr:return {'items':[]}  # popup check, not a send click
                self.clicks+=1;return True
        self_web=self.web;self.web.tab=Fake();self.web._pending_prompt='prompt'
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_web.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
            with self.assertRaises(ChatGPTWebError):self.web._send(wait_upload=60)
        return self.web.tab.clicks

    def test_never_clicks_send_twice_when_delivery_uncertain(self):
        # The field was cleared after the click: the request may have been sent — never press again.
        self.assertEqual(self._send_with_fake(''),1)
        self.assertTrue(self.web._send_uncertain)
        self.assertTrue(any('повторного клика не будет' in event for event in self.events))

    def test_reclicks_when_click_did_not_register(self):
        # The full prompt is still in the field and nothing new appeared: the click did not register.
        self.assertEqual(self._send_with_fake('prompt'),3)
        self.assertTrue(any('нажимаю «Отправить» ещё раз' in event for event in self.events))

    def _send_with_probe(self,landed,send_after=None):
        # landed: does the page register the press on the button; send_after: press number that finally sends
        clock=[0.0]
        class Fake:
            def __init__(self):self.clicks=0;self.pclick=0;self.times=[]
            def call(self,*a,**kw):return {}
            def js(self,expr):
                if expr==self_web.JS_SEND_STATE:
                    sent=send_after is not None and self.clicks>=send_after
                    return {'has':True,'disabled':False,'stop':False,'uploading':False,'text':'' if sent else 'prompt',
                            'key':'a','hit':False,'users':1 if sent else 0,'assistants':0,'pclick':self.pclick,'pdown':self.pclick}
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if self_web.JS_DISMISS in expr:return {'items':[]}
                self.clicks+=1;self.times.append(clock[0])
                if landed:self.pclick+=1
                return True
        self_web=self.web;self.web.tab=Fake();self.web._pending_prompt='prompt';self.web._send_baseline={'users':0,'assistants':0}
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_web.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
            if send_after:result=self.web._send(wait_upload=60)
            else:
                with self.assertRaises(ChatGPTWebError):self.web._send(wait_upload=60)
                result=None
        return self.web.tab,result

    def test_click_that_missed_button_is_retried_quickly(self):
        tab,_=self._send_with_probe(landed=False)
        self.assertEqual(tab.clicks,4)  # first press + 3 quick retries, then stop
        self.assertLess(tab.times[1]-tab.times[0],3)
        self.assertTrue(any('клик не дошёл до кнопки' in e for e in self.events))

    def test_landed_click_without_send_waits_before_retry(self):
        tab,_=self._send_with_probe(landed=True)
        self.assertEqual(tab.clicks,3)
        self.assertGreaterEqual(tab.times[1]-tab.times[0],8)
        self.assertTrue(any('клик дошёл до кнопки' in e for e in self.events))
        self.assertTrue(any('повторного клика не будет' in e for e in self.events))

    def test_retry_stops_once_sent(self):
        tab,result=self._send_with_probe(landed=True,send_after=2)
        self.assertTrue(result);self.assertEqual(tab.clicks,2)

    def test_probe_is_injected_into_send_state(self):
        self.assertIn('__twlProbe',self.web.JS_SEND_STATE)
        self.assertIn('pclick:pr.click',self.web.JS_SEND_STATE)

    def test_upload_failure_is_retryable(self):
        from chatgpt_errors import ChatGPTUploadFailed
        class Fake:
            def call(self,*a,**kw):return {}
            def js(self,expr):
                if expr==self_web.JS_SEND_STATE:return {'has':True,'disabled':True,'stop':False,'uploading':False,'uploadError':True,'text':'prompt','users':0,'assistants':0}
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                return True
        self_web=self.web;self.web.tab=Fake();self.web._pending_prompt='prompt'
        with patch('chatgpt_web.time.sleep'):
            with self.assertRaises(ChatGPTUploadFailed):self.web._send(wait_upload=60)

    def test_ask_reloads_and_resends_after_upload_failure(self):
        from chatgpt_errors import ChatGPTUploadFailed
        calls=[]
        def ask_locked(prompt,files,until):
            calls.append(getattr(self.web,'_force_new_chat',False))
            if len(calls)<3:raise ChatGPTUploadFailed('ChatGPT: «Загрузка не удалась»')
            return {'text':'ok','images':[]}
        self.web._ask_locked=ask_locked;self.web.timeout=900
        with patch('chatgpt_web.time.sleep'):res=self.web.ask('prompt',['t.jpg'])
        self.assertEqual(res['text'],'ok')
        self.assertEqual(calls,[False,True,True])  # повторы — с перезагрузкой страницы

    def test_ready_send_with_replaced_button_does_not_stall(self):
        clock=[0.0]
        class Fake:
            def __init__(self):self.polls=0;self.clicks=0
            def call(self,*a,**kw):return {}
            def js(self,expr):
                if expr==self_web.JS_SEND_STATE:
                    self.polls+=1
                    return {'has':True,'disabled':False,'stop':False,'uploading':False,'text':'prompt','key':str(self.polls),'hit':False,'users':1 if self.clicks else 0,'assistants':0}
                if expr==chatgpt_attach.JS_QUOTA:return {'blocked':False}
                if self_web.JS_DISMISS in expr:return {'items':[]}  # popup check, not a send click
                self.clicks+=1;return True
        self_web=self.web;self.web.tab=Fake();self.web._pending_prompt='prompt';self.web._send_baseline={'users':0,'assistants':0}
        def sleep(delay):clock[0]+=delay
        with patch('chatgpt_web.time.monotonic',side_effect=lambda:clock[0]),patch('chatgpt_web.time.sleep',side_effect=sleep):
            self.assertTrue(self.web._send(wait_upload=20))
        self.assertEqual(self.web.tab.clicks,1)
        self.assertTrue(any('кнопка нажата' in event for event in self.events))

    @unittest.skipUnless(shutil.which('chromium'),'requires Chromium')
    def test_dom_recognizes_numbered_transcript_and_upload_progress(self):
        from html.parser import HTMLParser
        with tempfile.TemporaryDirectory() as d:
            page=Path(d)/'composer.html'
            expr=chatgpt_attach.JS_ATTACHED % json.dumps('transcript.txt')
            page.write_text('<form><div><span>transcript(2).txt</span><div id="progress" role="progressbar">upload</div></div><textarea id="prompt-textarea"></textarea></form>'+                '<script>const a='+expr+';document.getElementById("progress").remove();const b='+expr+';'+                'const p=document.createElement("pre");p.id="out";p.textContent=JSON.stringify([a,b]);document.body.appendChild(p);</script>',encoding='utf8')
            result=subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30,check=True)
            class Parser(HTMLParser):
                def __init__(self):super().__init__();self.inside=False;self.data=''
                def handle_starttag(self,t,a):
                    if t=='pre' and dict(a).get('id')=='out':self.inside=True
                def handle_endtag(self,t):
                    if t=='pre':self.inside=False
                def handle_data(self,d):
                    if self.inside:self.data+=d
            parser=Parser();parser.feed(result.stdout);first,second=json.loads(parser.data)
            self.assertTrue(first['named']);self.assertTrue(first['busy'])
            self.assertTrue(second['named']);self.assertFalse(second['busy'])

    @unittest.skipUnless(shutil.which('chromium'),'requires Chromium')
    def test_real_dom_menu_then_unique_fallback_input(self):
        from html.parser import HTMLParser
        with tempfile.TemporaryDirectory() as d:
            page=Path(d)/'composer.html'
            scripts=('const opened='+chatgpt_attach.JS_OPEN_ATTACH+';'+
                     'const clicked='+chatgpt_attach.JS_CLICK_UPLOAD+';'+
                     'const selected='+chatgpt_attach.JS_PICK_FILE_INPUT % json.dumps('any')+';'+
                     'const out=document.createElement("pre");out.id="out";out.textContent=JSON.stringify({opened,clicked,selected,id:document.querySelector("[data-twl-file]")?.id});document.body.appendChild(out);')
            page.write_text('''<form><textarea id="prompt-textarea"></textarea><button type="button" data-testid="composer-plus-btn" onclick="document.getElementById('menu').hidden=false">+</button><input type="file" id="upload" accept=".txt"></form><div role="menu" id="menu" hidden><button role="menuitem" onclick="window.clicked=true">Добавить фото и файлы</button></div><script>'''+scripts+'</script>',encoding='utf8')
            proc=subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30,check=True)
            class Parser(HTMLParser):
                def __init__(self):super().__init__();self.inside=False;self.result=''
                def handle_starttag(self,t,a):
                    if t=='pre' and dict(a).get('id')=='out':self.inside=True
                def handle_endtag(self,t):
                    if t=='pre':self.inside=False
                def handle_data(self,d):
                    if self.inside:self.result+=d
            parser=Parser();parser.feed(proc.stdout);result=json.loads(parser.result)
            self.assertEqual(result,{'opened':True,'clicked':True,'selected':1,'id':'upload'})

    def test_no_retry_after_cdp_timeout_in_nonreplayable_operations(self):
        for method,params in [('DOM.setFileInputFiles',{'nodeId':5,'files':['test.txt']}),('Runtime.evaluate',{'expression':chatgpt_attach.JS_OPEN_ATTACH})]:
            with self.subTest(method=method):
                tab=Tab.__new__(Tab);tab._id=0;tab.timeout=5;tab._file_chooser_events=[]
                class WS:
                    def __init__(self):self.sent=0
                    def settimeout(self,t):pass
                    def send(self,v):self.sent+=1
                    def recv(self):raise websocket.WebSocketTimeoutException('timeout')
                tab.ws=WS();tab._reconnect=lambda: self.fail('replayed uncertain action')
                with self.assertRaises(ChatGPTWebError):tab.call(method,**params)
                self.assertEqual(tab.ws.sent,1)

if __name__=='__main__':unittest.main()
