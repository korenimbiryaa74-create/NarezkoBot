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
