import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from html.parser import HTMLParser
from unittest.mock import patch
try:
    import websocket
except ModuleNotFoundError:
    import sys, types
    fake=types.ModuleType('websocket')
    fake.WebSocketConnectionClosedException=type('WebSocketConnectionClosedException',(Exception,),{})
    fake.WebSocketTimeoutException=type('WebSocketTimeoutException',(Exception,),{})
    sys.modules['websocket']=fake
from chatgpt_finder import ChatGPTFinder
from chatgpt_errors import ChatGPTIncompleteAnswer
from chatgpt_web import ChatGPTWeb, ChatGPTQuotaError, ChatGPTLoginError

class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.attempts=[];self.closed=[];self.events=[];self.outcomes={}
        outer=self
        class Web:
            def __init__(self,**kw):self.account=Path(kw['profile']).parent.name;self._send_uncertain=False
            def ask(self,*a,**kw):
                outer.attempts.append(self.account)
                outcome=outer.outcomes.get(self.account,'ok')
                if outcome=='quota':raise ChatGPTQuotaError('quota')
                if outcome=='login':raise ChatGPTLoginError('login')
                if outcome=='uncertain':self._send_uncertain=True;raise ChatGPTQuotaError('quota')
                if outcome=='network':raise RuntimeError('network')
                if outcome=='sent_timeout': self._sent_confirmed=True; raise TimeoutError('answer timed out')
                if outcome=='sent_limit': self._sent_confirmed=True; raise ChatGPTQuotaError('quota')
                if outcome=='incomplete': self._sent_confirmed=True; raise ChatGPTIncompleteAnswer('no JSON')
                if outcome=='placeholder':return {'text':'Анализ завершен'}
                if outcome=='image':
                    image=outer.root/'result.png';image.write_bytes(b'test image');return {'images':[str(image)]}
                return {'text':'{"reactions":[]}'}
            def close(self):outer.closed.append(self.account)
        self.finder=ChatGPTFinder(self.root,web_factory=Web)
    def add(self,name): (self.root/'gpt_accounts'/name/'profile').mkdir(parents=True)
    def ask(self):return self.finder._ask_document('instruction','document','x.txt',100,log=self.events.append)
    def test_quota_switches_and_then_cache_reused(self):
        self.add('acc_02');self.outcomes['acc_01']='quota'
        self.assertEqual(self.ask(),'{"reactions":[]}')
        self.assertEqual(self.attempts,['acc_01','acc_02']);self.assertEqual(self.closed,self.attempts)
        self.ask();self.assertEqual(len(self.attempts),2)
        self.assertTrue(any('лимит' in x and 'acc_01' in x for x in self.events))
    def test_completed_but_unusable_reply_switches_once_per_profile(self):
        self.add('acc_02');self.add('acc_03')
        self.outcomes={'acc_01':'incomplete','acc_02':'placeholder'}
        self.assertEqual(self.ask(),'{"reactions":[]}');self.ask()
        self.assertEqual(self.attempts,['acc_01','acc_02','acc_03']);self.assertEqual(self.closed,self.attempts)

    def test_exhausted_after_incomplete_answers_no_cache(self):
        self.add('acc_02');self.outcomes={'acc_01':'placeholder','acc_02':'incomplete'}
        with self.assertRaisesRegex(ChatGPTIncompleteAnswer,'неполный ответ'):self.ask()
        self.assertEqual(self.attempts,['acc_01','acc_02']);self.assertEqual(self.closed,self.attempts)
        self.assertFalse((self.root/'gpt_uploads'/'response_cache').exists())

    def test_preview_saved_and_browser_closed(self):
        self.outcomes={'acc_01':'image'}
        path,template=self.finder.generate_image('preview',['sample.png'])
        self.assertEqual(template,'sample.png');self.assertTrue(Path(path).is_file())
        self.assertEqual(self.closed,['acc_01']);self.assertIsNone(self.finder._web)

    def test_all_limited_stop_once(self):
        self.add('acc_02');self.outcomes={'acc_01':'quota','acc_02':'quota'}
        with self.assertRaisesRegex(ChatGPTQuotaError,'нет доступных'):self.ask()
        self.assertEqual(self.attempts,['acc_01','acc_02'])
    def test_signed_out_profile_skipped(self):
        self.add('acc_02'); self.add('acc_03')
        self.outcomes={'acc_01':'quota','acc_02':'login'}
        self.ask();self.assertEqual(self.attempts,['acc_01','acc_02','acc_03'])
    def test_other_failures_and_uncertain_delivery_do_not_rotate(self):
        self.add('acc_02')
        for outcome in ('network','uncertain'):
            with self.subTest(outcome=outcome):
                self.attempts.clear();self.outcomes['acc_01']=outcome
                with self.assertRaises(RuntimeError):self.ask()
                self.assertEqual(self.attempts,['acc_01'])
    def test_confirmed_send_never_replayed_on_second_profile(self):
        self.add('acc_02')
        for outcome,error in [('sent_timeout',TimeoutError),('sent_limit',RuntimeError)]:
            with self.subTest(outcome=outcome):
                self.attempts.clear(); self.outcomes['acc_01']=outcome
                with self.assertRaises(error):self.ask()
                self.assertEqual(self.attempts,['acc_01'])

    def test_no_unregistered_profile(self):
        (self.root/'gpt_accounts'/'acc_02').mkdir(parents=True)
        self.outcomes['acc_01']='quota'
        with self.assertRaises(ChatGPTQuotaError):self.ask()
        self.assertEqual(self.attempts,['acc_01'])

    def test_quota_seen_after_opening_menu_prevents_file_upload(self):
        web=ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        class Tab:
            def __init__(self):self.checks=0;self.calls=[]
            def call(self,method,**kw):self.calls.append(method)
            def js(self,expression):
                if expression==web.JS_QUOTA:
                    self.checks+=1
                    return {'blocked':self.checks>=2}
                if 'composer-plus-btn' in expression:return True
                return False
            def clear_file_chooser(self):pass
            def wait_file_chooser(self,*a):return None
        web.tab=Tab()
        with patch('chatgpt_attach.time.sleep'),self.assertRaises(ChatGPTQuotaError):web._attach('test.txt')
        self.assertNotIn('DOM.setFileInputFiles',web.tab.calls)

    def test_busy_port_prevents_attaching_to_wrong_profile(self):
        web=ChatGPTWeb(str(self.root/'cookies'),str(self.root/'out'),profile=str(self.root/'profile'),verbose=False)
        with patch.object(web,'_debug_port_busy',return_value=True), patch.object(web,'_close_stale_chrome',return_value=False), patch('chatgpt_web.subprocess.Popen') as popen:
            with self.assertRaisesRegex(Exception,'занят'): web.start()
            popen.assert_not_called()

    @unittest.skipUnless(shutil.which('chromium'),'requires Chromium')
    def test_visible_limit_message_and_exclusions(self):
        script=ChatGPTWeb.JS_QUOTA
        html='''<form><textarea></textarea><div role="menu"><div>Перейти на Plus, чтобы загружать больше</div><div>Или подождите 21 час, чтобы повторить загрузку</div></div></form>
        <script>const first=QUOTA;document.querySelector('[role=menu]').remove();
        const m=document.createElement('div');m.setAttribute('data-message-author-role','assistant');m.innerText='Перейти на Plus, чтобы загружать больше';document.body.appendChild(m);
        const second=QUOTA; const p=document.createElement('pre');p.id='result';p.textContent=JSON.stringify([first,second]);document.body.appendChild(p);</script>'''.replace('QUOTA',script)
        page=self.root/'quota.html';page.write_text(html,encoding='utf-8')
        result=subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30,check=True)
        class Parser(HTMLParser):
            def __init__(self):super().__init__();self.inside=False;self.data=''
            def handle_starttag(self,t,a):
                if t=='pre' and dict(a).get('id')=='result':self.inside=True
            def handle_endtag(self,t):
                if t=='pre':self.inside=False
            def handle_data(self,d):
                if self.inside:self.data+=d
        p=Parser();p.feed(result.stdout);self.assertEqual(json.loads(p.data),[{'blocked':True},{'blocked':False}])

if __name__=='__main__':unittest.main()
