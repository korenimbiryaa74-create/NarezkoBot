# Ported from narezkobot/narezka-restored; comments and docstrings omitted for transport.
import base64
import json
import math
import threading
import unicodedata
import os
import subprocess
import time
import urllib.request
import websocket
import proxy_setup
import proxy_bridge
DEFAULT_CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
BASE = 'https://chatgpt.com'
_PROFILE_LOCKS = {}
_PROFILE_LOCKS_GUARD = threading.Lock()

def _cookie_domain(value):
    value = str(value or '').lstrip('.').lower()
    return any((value == d or value.endswith('.' + d) for d in ('chatgpt.com', 'openai.com')))

class ChatGPTWebError(RuntimeError):
    pass

class ChatGPTSafetyError(ChatGPTWebError):
    safety_blocked = True

class ChatGPTQuotaError(ChatGPTWebError):
    quota_blocked = True

class ChatGPTTechnicalError(ChatGPTWebError):
    pass
JS_EXPIRED_RE = '/Срок действия вашего сеанса истек|сеанс истек|Session has expired|session has expired|Войдите снова|войдите снова|Log in again/i'

def _http_json(url, data=None, method=None, timeout=10):
    req = urllib.request.Request(url, data=data, method=method or ('PUT' if data is not None else 'GET'))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
_NOISE_LABELS = {'Редактировать', 'Edit', 'Edit message', 'Поделиться', 'Share', 'Read aloud', 'Воспроизвести', 'Copy', 'Скопировать', 'Regenerate', 'Создать заново', 'Good response', 'Bad response', 'Хороший ответ', 'Плохой ответ', 'Этот диалог был полезен?', 'Was this helpful?', 'Ответ слишком короткий', 'Ответ слишком длинный'}

def _clean_text(text: str) -> str:
    if not text:
        return ''
    lines = [ln for ln in (s.strip() for s in text.splitlines()) if ln and ln not in _NOISE_LABELS]
    out = '\n'.join(lines).strip()
    return out

class Tab:

    def __init__(self, ws_url, timeout=30, port=None, target_id=None):
        self.ws_url = ws_url
        self.port = port
        self.target_id = target_id
        self.timeout = timeout
        self._id = 0
        self.ws = None
        self._connect()

    def _connect(self):
        self.ws = websocket.create_connection(self.ws_url, timeout=self.timeout, suppress_origin=True)

    def _reconnect(self):
        if self.port and self.target_id:
            try:
                for t in _http_json(f'http://127.0.0.1:{self.port}/json/list'):
                    if t.get('id') == self.target_id:
                        self.ws_url = t['webSocketDebuggerUrl']
                        break
            except Exception:
                pass
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        self._connect()
        for domain in ('Page', 'Runtime', 'DOM', 'Network'):
            try:
                self.call(f'{domain}.enable')
            except Exception:
                pass

    def call(self, method, **params):
        last = None
        for attempt in (0, 1):
            try:
                if self.ws is None:
                    self._connect()
                self._id += 1
                self.ws.settimeout(self.timeout)
                self.ws.send(json.dumps({'id': self._id, 'method': method, 'params': params}))
                while True:
                    msg = json.loads(self.ws.recv())
                    if msg.get('id') == self._id:
                        if 'error' in msg:
                            raise ChatGPTWebError(f"{method}: {msg['error'].get('message')}")
                        return msg.get('result', {})
            except (websocket.WebSocketConnectionClosedException, websocket.WebSocketTimeoutException, OSError, ValueError) as e:
                last = e
                if attempt == 0:
                    self._reconnect()
                    continue
                raise ChatGPTWebError(f'CDP-соединение умерло ({method}): {str(e)[:120]}')
        raise ChatGPTWebError(f'CDP: {last}')

    def js(self, expr, timeout=None):
        old = self.timeout
        if timeout:
            self.timeout = timeout
        try:
            r = self.call('Runtime.evaluate', expression=expr, returnByValue=True, awaitPromise=True)
        finally:
            self.timeout = old
        if r.get('exceptionDetails'):
            details = r['exceptionDetails']
            text = details.get('text') or (details.get('exception') or {}).get('description')
            raise ChatGPTWebError(f'JS: {text}')
        return r.get('result', {}).get('value')

    def node_id(self, selector):
        doc = self.call('DOM.getDocument', depth=0)
        root = doc['root']['nodeId']
        found = self.call('DOM.querySelector', nodeId=root, selector=selector)
        return found.get('nodeId')

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

class ChatGPTWeb:
    DEFAULT_LOAD_TIMEOUT = 90

    def __init__(self, cookies_path: str, out_dir: str='generated_images', headless: bool=True, port: int=9222, profile: str=None, timeout: int=300, verbose: bool=True, offscreen: bool=False, load_timeout: int=None):
        self.cookies_path = cookies_path
        self.out_dir = out_dir
        self.headless = headless
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.load_timeout = load_timeout or self.DEFAULT_LOAD_TIMEOUT
        self.offscreen = offscreen
        self.profile = profile or os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'TWLoader', 'chrome-gpt')
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.profile, exist_ok=True)
        self.proc = None
        self.tab = None
        self._request_deadline = None
        with _PROFILE_LOCKS_GUARD:
            self._profile_lock = _PROFILE_LOCKS.setdefault(os.path.normcase(os.path.realpath(self.profile)), threading.RLock())

    @staticmethod
    def _chrome_path() -> str:
        for p in (DEFAULT_CHROME, EDGE):
            if os.path.exists(p):
                return p
        raise ChatGPTWebError('не нашёл ни Chrome, ни Edge')

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if self.proc:
            self.proc = None
            self.tab = None
        args = [self._chrome_path(), f'--remote-debugging-port={self.port}', '--remote-debugging-address=127.0.0.1', f'--user-data-dir={self.profile}', '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble', '--disable-blink-features=AutomationControlled', '--remote-allow-origins=*', '--window-size=1400,1000']
        if self.headless:
            args.append('--headless=new')
        elif self.offscreen:
            args.append('--window-position=-32000,-32000')
        bridge_args = proxy_bridge.chrome_args()
        if bridge_args:
            args.extend(bridge_args)
            self._log(f'прокси: {proxy_setup.describe()}')
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._log(f'Chrome pid {self.proc.pid}')
        for _ in range(60):
            try:
                _http_json(f'http://127.0.0.1:{self.port}/json/version')
                return
            except Exception:
                time.sleep(0.5)
        raise ChatGPTWebError('Chrome не поднял CDP-порт')

    def _open_tab(self, url: str) -> Tab:
        try:
            info = _http_json(f'http://127.0.0.1:{self.port}/json/new?{url}', data=b'', method='PUT')
            tab = Tab(info['webSocketDebuggerUrl'], timeout=60, port=self.port, target_id=info.get('id'))
            tab.call('Page.enable')
            tab.call('Runtime.enable')
            return tab
        except Exception:
            info = _http_json(f'http://127.0.0.1:{self.port}/json/new', data=b'', method='PUT')
            tab = Tab(info['webSocketDebuggerUrl'], timeout=60, port=self.port, target_id=info.get('id'))
            tab.call('Page.enable')
            tab.call('Runtime.enable')
            tab.call('Page.navigate', url=url)
            time.sleep(3)
            return tab

    def _auth_cookie(self):
        try:
            rows = self.tab.call('Network.getAllCookies').get('cookies') or []
            return any((_cookie_domain(c.get('domain')) and 'session-token' in c.get('name', '') and (float(c.get('expires', -1)) < 0 or float(c.get('expires', 0)) > time.time()) for c in rows))
        except Exception:
            return False

    def ensure_login(self, wait: int=None) -> bool:
        self.start()
        wait = self._remaining(wait or self.load_timeout)
        fresh = self.tab is None
        imported = False
        if fresh:
            self.tab = self._open_tab('about:blank')
            self.tab.call('Network.enable')
            if not self._auth_cookie() and os.path.isfile(self.cookies_path):
                self._log('пустой профиль: подгружаю cookies ДО открытия ChatGPT')
                imported = bool(self._inject_cookies())
            self.tab.call('Page.navigate', url=f'{BASE}/')
        elif self._has_session():
            return True
        deadline = time.monotonic() + wait
        navigated_at = time.monotonic() if fresh else 0.0
        last_log = time.monotonic()
        mark = None
        since = 0.0
        while time.monotonic() < deadline:
            try:
                state = self.tab.js(self.JS_LOGIN_STATE) or {}
                if state.get('expired') or state.get('login'):
                    if time.monotonic() - navigated_at < 2:
                        time.sleep(0.25)
                        continue
                    if not imported and os.path.isfile(self.cookies_path):
                        imported = True
                        if self._inject_cookies():
                            self.tab.call('Page.navigate', url=f'{BASE}/')
                            navigated_at = time.monotonic()
                            mark = None
                            continue
                    self._log('ChatGPT просит ручной вход — этот профиль не авторизован')
                    return False
                if state.get('ready') and (not state.get('guest')) and self._auth_cookie():
                    current = state.get('mark')
                    if mark != current:
                        mark = current
                        since = time.monotonic()
                    elif time.monotonic() - since >= 1.5:
                        self._log('ChatGPT: авторизованный интерфейс готов')
                        self.dump_cookies()
                        return True
                else:
                    mark = None
                if state.get('guest') and state.get('ready') and (not self._auth_cookie()):
                    self._log('открыт гостевой ChatGPT — требуется вход в аккаунт')
                    return False
            except ChatGPTWebError:
                raise
            except Exception:
                pass
            if time.monotonic() - last_log >= 15:
                last_log = time.monotonic()
                self._log('жду интерфейс ChatGPT; страницу повторно НЕ перезагружаю')
            time.sleep(0.5)
        raise ChatGPTWebError('не дождался интерфейса ChatGPT; проверь загрузку страницы и прокси, а не транскрипцию')

    def _reload_with_cookies(self):
        if self._inject_cookies():
            self.tab.call('Page.navigate', url=f'{BASE}/')

    def _has_session(self) -> bool:
        try:
            state = self.tab.js(self.JS_LOGIN_STATE) or {}
            return bool(state.get('ready') and (not any((state.get(k) for k in ('expired', 'login', 'guest')))) and self._auth_cookie())
        except Exception:
            return False
    JS_LOGIN_STATE = '(()=>{\nconst e=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable="true"],form textarea\');\nconst visible=n=>!!n&&n.getClientRects().length>0;\nconst body=document.body?document.body.innerText:\'\';\nconst hydrated=!!e&&(!!e.pmViewDesc||Object.keys(e).some(k=>k.startsWith(\'__reactProps$\')||k.startsWith(\'__reactFiber$\')));\nconst expired=' + JS_EXPIRED_RE + '.test(body);\nconst login=/\\/auth\\/login|\\/login/.test(location.pathname);\nconst guest=Array.from(document.querySelectorAll(\'button[data-testid="login-button"],a[href*="/auth/login"]\')).some(visible);\nif(e&&!e.dataset.twl)e.dataset.twl=String(Math.random());\nreturn {expired,login,guest,ready:hydrated&&visible(e)&&!e.disabled&&e.getAttribute(\'aria-disabled\')!==\'true\',mark:e?e.dataset.twl:null};\n})()'

    def _remaining(self, limit):
        deadline = getattr(self, '_request_deadline', None)
        return max(0, min(float(limit), deadline - time.monotonic())) if deadline else float(limit)

    def dump_cookies(self) -> int:
        try:
            res = self.tab.call('Network.getAllCookies')
            raw = res.get('cookies') or []
        except Exception as e:
            self._log(f'куки не выгрузились: {str(e)[:80]}')
            return 0
        if not any((_cookie_domain(c.get('domain')) and 'session-token' in c.get('name', '') for c in raw)):
            return 0
        out = []
        for c in raw:
            domain = c.get('domain') or ''
            if not _cookie_domain(domain):
                continue
            expires = c.get('expires', -1)
            session = bool(c.get('session')) or expires is None or expires < 0
            row = {'domain': domain, 'hostOnly': not domain.startswith('.'), 'httpOnly': bool(c.get('httpOnly', False)), 'name': c.get('name'), 'path': c.get('path', '/'), 'sameSite': c.get('sameSite') or 'lax', 'secure': bool(c.get('secure', True)), 'session': session, 'storeId': None, 'value': c.get('value')}
            if not session:
                row['expirationDate'] = float(expires)
            out.append(row)
        if not out:
            self._log('куки пустые — файл не трогаю')
            return 0
        tmp = self.cookies_path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=4, ensure_ascii=False)
        os.replace(tmp, self.cookies_path)
        self._log(f'куки обновлены: {len(out)} шт → {self.cookies_path}')
        return len(out)

    def _inject_cookies(self):
        try:
            with open(self.cookies_path, encoding='utf-8-sig') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            self._log('не удалось прочитать JSON cookies')
            return 0
        if isinstance(raw, dict):
            raw = raw.get('cookies', [])
        if not isinstance(raw, list):
            return 0
        ok = 0
        for c in raw:
            if not isinstance(c, dict) or not _cookie_domain(c.get('domain')):
                continue
            if not isinstance(c.get('name'), str) or not isinstance(c.get('value'), str):
                continue
            try:
                expires = float(c.get('expires', c.get('expirationDate', -1)) or -1)
                if expires > 1000000000000.0:
                    expires /= 1000
                if not math.isfinite(expires):
                    continue
                session = bool(c.get('session')) or expires < 0
                if not session and expires <= time.time():
                    continue
                same = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None', 'no_restriction': 'None'}.get(str(c.get('sameSite', '')).lower())
                args = {'name': c['name'], 'value': c['value'], 'domain': c['domain'], 'path': c.get('path') or '/', 'secure': bool(c.get('secure', True)) or same == 'None', 'httpOnly': bool(c.get('httpOnly', False))}
                if not session:
                    args['expires'] = expires
                if same:
                    args['sameSite'] = same
                result = self.tab.call('Network.setCookie', **args)
                if result.get('success', True):
                    ok += 1
            except Exception:
                pass
        self._log(f'подгружено действующих cookies: {ok}')
        return ok
    JS_IMGS = "(()=>{const rx=/oaiusercontent|estuary\\/content|\\/backend-api\\/files|persistent\\.oaistatic|videos\\.openai/;return Array.from(document.querySelectorAll('img')).map(i=>i.currentSrc||i.src||'').filter(u=>rx.test(u));})()"
    JS_COMPOSER = "document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea')"
    JS_READY = '(()=>{\nconst e=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst hydrated=!!e&&(!!e.pmViewDesc||Object.keys(e).some(k=>k.startsWith(\'__reactProps$\')||k.startsWith(\'__reactFiber$\')));\nif(!hydrated||document.readyState===\'loading\'||!e.getClientRects().length||e.disabled)return {ok:false};\nif(!e.dataset.twl){e.dataset.twl=String(Math.random());}\nconst busy=!!document.querySelector(\'button[data-testid="stop-button"]\');\nreturn {ok:true,mark:e.dataset.twl,busy:busy,\n page:window.__twlPage||(window.__twlPage=String(Math.random())),\n path:location.pathname,\n msgs:document.querySelectorAll(\'[data-message-author-role],[data-turn="user"],[data-turn="assistant"]\').length};\n})()'
    JS_COMPOSER_TEXT = "(()=>{const e=document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea');return e?(e.innerText||e.value||''):null;})()"
    JS_USER_MSGS = 'document.querySelectorAll(\'[data-message-author-role="user"],[data-turn="user"]\').length'
    JS_SEND_STATE = '(()=>{\nconst e=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst visible=n=>!!n&&n.getClientRects().length>0&&getComputedStyle(n).visibility!==\'hidden\';\nconst label=n=>(n.getAttribute(\'aria-label\')||n.getAttribute(\'title\')||\'\').trim();\nconst stopLabel=n=>/^(?:stop(?:\\s|$)|остановить(?:\\s|$)|прекратить(?:\\s|$))/i.test(label(n));\nconst sendLabel=n=>/^(?:send(?:\\s|$)|submit(?:\\s|$)|отправить(?:\\s|$))/i.test(label(n));\nconst known=\'button[data-testid="send-button"],button[data-testid="composer-submit-button"],button#composer-submit-button\';\nlet scope=e?(e.closest(\'form\')||e.parentElement):null;\nif(scope&&!scope.matches(\'form\')){\n  for(let i=0;i<5&&scope;i++){\n    if(scope.querySelector(known)||Array.from(scope.querySelectorAll(\'button\')).some(sendLabel))break;\n    scope=scope.parentElement;\n  }\n}\nscope=scope||document;\nconst buttons=Array.from(scope.querySelectorAll(\'button\')).filter(visible);\nconst stop=Array.from(document.querySelectorAll(\'button[data-testid="stop-button"],button[data-testid="composer-stop-button"],button[data-testid="stop-generating"]\')).some(visible)||buttons.some(stopLabel);\nconst b=buttons.find(n=>n.matches(known)&&!stopLabel(n))||buttons.find(n=>sendLabel(n)&&!stopLabel(n))||\n  (scope.tagName===\'FORM\'?buttons.find(n=>n.type===\'submit\'&&!stopLabel(n)):null);\nconst uploading=Array.from(scope.querySelectorAll(\'[role="progressbar"],[aria-busy="true"],.animate-spin,[data-state="uploading"]\')).some(visible);\nconst alerts=Array.from(scope.querySelectorAll(\'[role="alert"],[data-testid="file-upload-error"],[data-testid="upload-error"],[data-state="error"]\')).filter(visible).map(n=>n.innerText||\'\').join(\' \');\nconst uploadError=/(?:upload|file|файл|загруз)/i.test(alerts)&&/(?:error|fail|ошиб|не удалось|невозможно)/i.test(alerts);\nconst text=e?(e.value!==undefined?e.value:(e.innerText||e.textContent||\'\')):\'\';\nlet x=null,y=null,hit=false,key=null;\nif(b){\n  if(!b.dataset.twlSend)b.dataset.twlSend=String(Math.random());key=b.dataset.twlSend;\n  const r=b.getBoundingClientRect();x=r.x+r.width/2;y=r.y+r.height/2;\n  const top=document.elementFromPoint(x,y);hit=!!top&&(top===b||b.contains(top));\n}\nreturn {has:!!b,disabled:!b||b.disabled||b.getAttribute(\'aria-disabled\')===\'true\',stop,\n uploading,uploadError,text,x,y,hit,key,\n users:document.querySelectorAll(\'[data-message-author-role="user"],[data-turn="user"]\').length,\n assistants:document.querySelectorAll(\'[data-message-author-role="assistant"],[data-turn="assistant"]\').length};\n})()'
    JS_CLICK_SEND = "(()=>{\nconst state=%s;\nconst b=Array.from(document.querySelectorAll('button')).find(n=>n.dataset.twlSend===state.key);\nif(!b||b.disabled||b.getAttribute('aria-disabled')==='true'||!b.getClientRects().length)return false;\nb.click();return true;\n})()"

    def _wait_ready(self, wait: int=None, settle: float=2.5) -> dict | None:
        deadline = time.time() + self._remaining(wait or self.load_timeout)
        mark, since = (None, 0.0)
        while time.time() < deadline:
            try:
                st = self.tab.js(self.JS_READY) or {}
            except Exception:
                st = {}
            if st.get('ok'):
                if st.get('mark') != mark:
                    mark, since = (st.get('mark'), time.time())
                elif time.time() - since >= settle:
                    return st
            else:
                mark = None
            time.sleep(0.5)
        return None

    def _page_id(self) -> str | None:
        try:
            return self.tab.js('window.__twlPage||null')
        except Exception:
            return None

    def new_chat(self, wait: int=None, force: bool=False) -> bool:
        if not self.ensure_login():
            return False
        wait = wait or self.load_timeout
        st = self._wait_ready(min(wait, 60), settle=1.5) or {}
        if not force and st.get('ok') and (st.get('path') == '/') and (not st.get('msgs')):
            self._log('уже на пустом чате — без перезагрузки')
            return True
        clicked = False
        if not force:
            try:
                clicked = bool(self.tab.js('(()=>{const b=document.querySelector(\'[data-testid="create-new-chat-button"],a[href="/"][data-discover],nav a[href="/"]\');if(!b)return false;b.click();return true;})()'))
            except Exception:
                clicked = False
        if clicked:
            deadline = time.time() + 15
            while time.time() < deadline:
                st = self._wait_ready(10, settle=1.5) or {}
                if st.get('path') == '/' and (not st.get('msgs')):
                    self._log('чистый чат открыт (кнопкой)')
                    return True
                time.sleep(1)
            self._log('кнопка «новый чат» не сработала — перезагружаю')
        self.tab.call('Page.navigate', url=f'{BASE}/')
        time.sleep(2)
        if self._wait_ready(wait):
            self._log('чистый чат открыт')
            return True
        self._log('чистый чат не открылся')
        return False

    @staticmethod
    def _norm(s: str) -> str:
        s = unicodedata.normalize('NFC', s or '')
        for char in ('\u200b', '\u200c', '\u200d', '\ufeff', '\u2060'):
            s = s.replace(char, '')
        return ''.join(s.split()).lower()

    def _type_prompt(self, prompt: str) -> bool:
        tab = self.tab
        want = self._norm(prompt)
        for attempt in range(3):
            box = tab.js("(()=>{const e=document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea');if(!e)return null;e.scrollIntoView({block:'center'});e.focus();const r=e.getBoundingClientRect();return {x:r.x+Math.min(r.width/2,200),y:r.y+r.height/2};})()")
            if not box:
                self._wait_ready(30)
                continue
            tab.call('Input.dispatchMouseEvent', type='mousePressed', x=box['x'], y=box['y'], button='left', clickCount=1)
            tab.call('Input.dispatchMouseEvent', type='mouseReleased', x=box['x'], y=box['y'], button='left', clickCount=1)
            tab.js("(()=>{const e=document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea');if(e&&(e.innerText||e.value||'').trim()){e.focus();document.execCommand('selectAll',false,null);}})()")
            tab.call('Input.insertText', text=prompt)
            time.sleep(0.8 + attempt)
            got = self._norm(tab.js(self.JS_COMPOSER_TEXT) or '')
            if want and want == got:
                self._pending_prompt = prompt
                self._log(f'полный текст в поле подтверждён: {len(prompt)} знаков')
                return True
            self._log(f'текст не встал в поле (попытка {attempt + 1}/3) — повторяю')
            self._wait_ready(30, settle=1.5)
        return False

    def _send(self, wait_upload: int=180) -> bool:
        tab = self.tab
        initial = tab.js(self.JS_SEND_STATE) or {}
        baseline = getattr(self, '_send_baseline', None) or initial
        before_users = int(baseline.get('users', 0))
        before_assistants = int(baseline.get('assistants', 0))
        deadline = time.monotonic() + self._remaining(wait_upload)
        expected = self._norm(getattr(self, '_pending_prompt', '') or initial.get('text', ''))
        payload_consumed = False
        clicks = 0
        stable_key = None
        stable = 0
        next_log = 0.0
        last_click = 0.0
        self._log('текст в поле; жду завершения загрузки файла и активную кнопку отправки')
        try:
            tab.call('Page.bringToFront')
        except Exception:
            pass
        while time.monotonic() < deadline:
            st = tab.js(self.JS_SEND_STATE) or {}
            if int(st.get('users', 0)) > before_users or int(st.get('assistants', 0)) > before_assistants or (st.get('stop') and (clicks or not baseline.get('stop'))):
                self._send_uncertain = False
                self._log('отправка подтверждена: новый ход чата / генерация')
                return True
            self._check_upload_quota()
            if st.get('uploadError'):
                raise ChatGPTWebError('ChatGPT сообщил об ошибке загрузки файла; запрос НЕ отправлен')
            current = self._norm(st.get('text', ''))
            if clicks and (not current):
                payload_consumed = True
            if payload_consumed:
                if time.monotonic() >= next_log:
                    self._log('поле очистилось после клика; жду подтверждения, повторно НЕ отправляю')
                    next_log = time.monotonic() + 10
                time.sleep(0.5)
                continue
            ready = st.get('has') and (not st.get('stop')) and (not st.get('disabled')) and (not st.get('uploading')) and current and (not expected or current == expected)
            fingerprint = (st.get('key'), current)
            if ready:
                stable = stable + 1 if fingerprint == stable_key else 1
                stable_key = fingerprint
            else:
                stable = 0
                stable_key = None
            if time.monotonic() >= next_log:
                reason = 'идёт загрузка файла' if st.get('uploading') else 'кнопка ещё неактивна' if st.get('disabled') else 'кнопка не найдена' if not st.get('has') else 'проверяю полный текст' if current != expected else 'кнопка готова'
                self._log('отправка: ' + reason)
                next_log = time.monotonic() + 10
            can_click = stable >= 3 and (clicks == 0 or (clicks == 1 and time.monotonic() - last_click >= 15))
            if can_click:
                previous_uncertainty = getattr(self, '_send_uncertain', False)
                self._send_uncertain = True
                if clicks == 0 and st.get('hit') and (st.get('x') is not None):
                    x, y = (st['x'], st['y'])
                    tab.call('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y)
                    tab.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
                    tab.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y, button='left', clickCount=1)
                    self._log('нажал кнопку отправки мышью; проверяю доставку')
                else:
                    if not tab.js(self.JS_CLICK_SEND % json.dumps({'key': st.get('key')})):
                        self._send_uncertain = previous_uncertainty
                        stable = 0
                        time.sleep(0.5)
                        continue
                    self._log('нажал кнопку отправки через DOM; проверяю доставку')
                clicks += 1
                last_click = time.monotonic()
                stable = 0
            time.sleep(0.5)
        if clicks:
            self._send_uncertain = True
            raise ChatGPTWebError('на кнопку отправки нажал, но доставка не подтверждена; чат оставлен открытым, автоматически повторять запрос не буду')
        self._log('не удалось дождаться готового файла/кнопки; запрос НЕ отправлен')
        return False
    JS_PICK_FILE_INPUT = '((want)=>{\nconst all=Array.from(document.querySelectorAll(\'input[type="file"]\'));\nall.forEach(i=>i.removeAttribute(\'data-twl-file\'));\nif(!all.length)return 0;\nconst ok=i=>{const a=(i.getAttribute(\'accept\')||\'\').toLowerCase().trim();\n  if(!a)return 3; if(a.includes(\'text\')||a.includes(\'.txt\')||a.includes(\'*/*\'))return 2;\n  return a.split(\',\').every(x=>x.trim().startsWith(\'image\'))?0:1;};\nconst img=want===\'image\';\nconst best=all.map(i=>[img?(ok(i)===0?3:1):ok(i),i]).sort((x,y)=>y[0]-x[0])[0];\nif(!img&&best[0]===0)return 0;\nbest[1].setAttribute(\'data-twl-file\',\'1\');return all.length;})(%s)'
    JS_ATTACHED = '((name)=>{\nconst a=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst f=a?(a.closest(\'form\')||a.parentElement.parentElement.parentElement||document):document;\nconst blob=Array.from(f.querySelectorAll(\'img\'))\n .filter(i=>(i.currentSrc||i.src||\'\').startsWith(\'blob:\')).length;\nconst txt=(f.innerText||\'\');\nconst stem=name.replace(/\\.[^.]+$/,\'\');\nreturn {blob:blob,named:txt.includes(name)||(stem.length>3&&txt.includes(stem)),\n busy:!!f.querySelector(\'[role="progressbar"],circle[stroke-dasharray],.animate-spin\')};})(%s)'
    JS_DROP_FILE = '((name,b64)=>{\nconst i=document.querySelector(\'input[data-twl-file="1"]\')||document.querySelector(\'input[type="file"]\');\nif(!i)return false;\nconst bin=atob(b64);const u=new Uint8Array(bin.length);\nfor(let k=0;k<bin.length;k++)u[k]=bin.charCodeAt(k);\nconst file=new File([u],name,{type:name.toLowerCase().endsWith(\'.txt\')?\'text/plain\':\'application/octet-stream\'});\nconst dt=new DataTransfer();dt.items.add(file);\ni.files=dt.files;i.dispatchEvent(new Event(\'change\',{bubbles:true}));\nreturn true;})(%s,%s)'
    JS_QUOTA = '(()=>{\nconst visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!==\'hidden\';\nconst patterns=[/file upload limit reached/i,/you(?:[\'’]ve| have) reached (?:your |the )?(?:file |upload |attachment )*(?:limit|quota)/i,\n /too many (?:file )?uploads/i,/(?:upload|attachment|file).{0,35}(?:limit|quota).{0,25}(?:reached|exceeded)/i,\n /(?:достигнут|исчерпан|превышен).{0,30}лимит.{0,30}(?:загруз|файл|вложен)/i,\n /лимит.{0,25}(?:загруз|файл|вложен).{0,25}(?:достигнут|исчерпан|превышен)/i,\n /вы (?:достигли|исчерпали).{0,30}лимит.{0,30}(?:загруз|файл|вложен)/i];\nfor(const e of document.querySelectorAll(\'[role="alert"],[role="dialog"],[role="tooltip"],[data-testid*="toast"],button[aria-disabled="true"],p,span\')){\n if(!visible(e)||e.closest(\'[data-message-author-role="user"],[data-turn="user"],pre,code\'))continue;\n const text=((e.innerText||\'\')+\' \'+(e.getAttribute(\'title\')||\'\')+\' \'+(e.getAttribute(\'aria-label\')||\'\')).trim();\n if(text.length<=1000&&patterns.some(p=>p.test(text)))return {blocked:true,text:text.slice(0,350)};\n}\nreturn {blocked:false};})()'

    def _check_upload_quota(self):
        try:
            state = self.tab.js(self.JS_QUOTA) or {}
        except Exception:
            return
        if isinstance(state, dict) and state.get('blocked'):
            self._quota_blocked = True
            raise ChatGPTQuotaError('ChatGPT: лимит загрузок исчерпан. ' + str(state.get('text') or ''))

    def _wait_attached(self, name: str, image: bool, seconds: int=45) -> bool:
        deadline = time.monotonic() + self._remaining(seconds)
        stable = 0
        while time.monotonic() < deadline:
            self._check_upload_quota()
            try:
                state = self.tab.js(self.JS_ATTACHED % json.dumps(name)) or {}
                exists = state.get('blob') or state.get('named') if image else state.get('named')
                stable = stable + 1 if exists and (not state.get('busy')) else 0
                if stable >= 2:
                    return True
            except Exception:
                stable = 0
            time.sleep(0.5)
        return False

    def _attach(self, path: str) -> bool:
        tab = self.tab
        name = os.path.basename(path)
        image = os.path.splitext(name)[1].lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif')
        n_inputs = 0
        intercepted = False
        menu_clicked = False
        for attempt in range(20):
            self._check_upload_quota()
            try:
                n_inputs = tab.js(self.JS_PICK_FILE_INPUT % json.dumps('image' if image else 'any')) or 0
            except Exception:
                n_inputs = 0
            if n_inputs:
                break
            if not intercepted:
                try:
                    # Never activate a native file dialog unless CDP intercepts it.
                    tab.call('Page.setInterceptFileChooserDialog', enabled=True)
                    intercepted = True
                    tab.js("""(()=>{const b=document.querySelector('button[data-testid="composer-plus-btn"],button[aria-label="Add files and more"],button[aria-label="Добавить файлы и другое"]');if(b)b.click();})()""")
                except Exception:
                    self._log('не удалось безопасно перехватить окно выбора файла')
                    return False
            elif not menu_clicked:
                try:
                    menu_clicked = bool(tab.js("""(()=>{const item=Array.from(document.querySelectorAll('[role="menuitem"]')).find(e=>/Add photos.*files|Upload.*file|Добавить.*файл|Загрузить.*файл/i.test(e.innerText||''));if(!item)return false;item.click();return true;})()"""))
                except Exception:
                    return False
            time.sleep(1)
        if not n_inputs:
            self._log('не нашёл input[type=file] — вложение пропущено')
            return False
        nid = tab.node_id('input[data-twl-file="1"]')
        if nid:
            tab.call('DOM.setFileInputFiles', files=[os.path.abspath(path)], nodeId=nid)
            if self._wait_attached(name, image):
                self._log(f'приложил файл: {name}')
                return True
            self._log(f'файл {name} не появился в поле — пробую другим способом')
        try:
            import base64
            with open(path, 'rb') as fh:
                b64 = base64.b64encode(fh.read()).decode('ascii')
            tab.js(self.JS_PICK_FILE_INPUT % json.dumps('image' if image else 'any'))
            if tab.js(self.JS_DROP_FILE % (json.dumps(name), json.dumps(b64)), timeout=120) and self._wait_attached(name, image, 60):
                self._log(f'приложил файл (через страницу): {name}')
                return True
        except ChatGPTQuotaError:
            raise
        except Exception as e:
            self._log(f'запасное вложение упало: {str(e)[:100]}')
        self._log(f'вложение НЕ прикрепилось: {name}')
        return False

    def ask(self, prompt: str, files: list=None, until=None) -> dict:
        with self._profile_lock:
            self._send_uncertain = False
            self._answer_unreadable = False
            self.last_answer = ''
            self._pending_prompt = ''
            self._answer_baseline = []
            self._notice_baseline = []
            self._safety_blocked = False
            self._quota_blocked = False
            self._request_deadline = time.monotonic() + float(self.timeout)
            try:
                return self._ask_locked(prompt, files, until)
            finally:
                self._request_deadline = None

    def _ask_locked(self, prompt: str, files: list=None, until=None) -> dict:
        last = 'неизвестно'
        for attempt in range(3):
            if self._remaining(1) <= 0:
                raise ChatGPTWebError('время отправки запроса истекло')
            if not self.new_chat(force=attempt > 0):
                raise ChatGPTWebError('не залогинились в ChatGPT — куки протухли, переэкспортируй их из браузера')
            tab = self.tab
            page = self._page_id()
            self._send_baseline = tab.js(self.JS_SEND_STATE) or {}
            known = set(tab.js(self.JS_IMGS) or [])
            initial = self._read_answer_state()
            self._answer_baseline = list(initial.get('texts') or [])
            self._notice_baseline = list(initial.get('notices') or [])
            attached = True
            for path in files or []:
                attached = self._attach(path) and attached
                time.sleep(1.5)
            docs = [p for p in files or [] if os.path.splitext(p)[1].lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.gif')]
            if docs and (not attached):
                raise ChatGPTWebError('транскрипт не прикрепился — запрос без файла НЕ отправлен; проверьте браузер и повторите вручную')
            self._log('файл готов; ввожу критерии поиска')
            if not self._type_prompt(prompt):
                last = 'текст не вводится в поле'
                continue
            if self._page_id() != page:
                last = 'страница перезагрузилась во время ввода'
                self._log(last + ' — заново')
                continue
            if not self._send():
                last = 'файл или кнопка отправки не готовы; Enter не используется'
                continue
            self._log(f'отправил запрос: {prompt[:80]!r}')
            try:
                text, images = self._wait_answer(known, page, until)
            except ChatGPTTechnicalError:
                if files:
                    self._log('Техническая ошибка: файл уже отправлен. Повторно не загружаю, чтобы не расходовать лимит вложений')
                    raise
                if attempt >= 1 or self._remaining(1) < 20:
                    raise
                self._log('Явная техническая ошибка генерации — один повтор в новом чате на том же аккаунте')
                continue
            self.dump_cookies()
            return {'text': text, 'images': images}
        raise ChatGPTWebError(f'не удалось отправить запрос: {last}')
    JS_STATE = '(()=>{\nconst pending="";\nconst prior=[];\nconst priorNotices=[];\nconst norm=t=>(t||\'\').normalize(\'NFC\').replace(/[\\u200B-\\u200D\\uFEFF\\s]+/g,\'\').toLowerCase();\nconst promptNorm=norm(pending);\nconst previous=new Set(prior.map(norm));\nconst allowed=t=>{\n const n=norm(t);\n if(!n||previous.has(n)||Array.from(previous).some(p=>p.length>20&&n.includes(p)))return false;\n // Отдельный пример JSON в сообщении пользователя — не ответ ассистента.\n if(promptNorm&&(promptNorm.includes(n)||n.includes(promptNorm)))return false;\n return true;\n};\nconst visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!==\'hidden\';\nconst user=\'[data-message-author-role="user"],[data-turn="user"]\';\nconst excluded=\'[data-testid*="advert"],[data-testid*="sponsor"],[data-testid*="recommend"],\'+\n \'[data-ad],.adsbygoogle,[aria-label="Advertisement"],[aria-label="Реклама"],\'+\n \'[data-testid*="thinking"],[data-testid*="reasoning"],.result-thinking\';\nconst skip=e=>!!e.closest(user+\',\'+excluded+\',form,[contenteditable="true"]\');\nconst article=\'article[data-testid^="conversation-turn"],article[data-turn-id],article[data-turn]\';\nconst content=\'.markdown,.prose,[data-testid="markdown"],[data-testid="message-content"]\';\nconst users=Array.from(document.querySelectorAll(user)).filter(visible);\nconst lastUser=users.length?users[users.length-1]:null;\nconst roots=[];\nconst add=e=>{\n if(!visible(e)||skip(e))return;\n const root=e.closest(article)||e;\n if(skip(root)||root.matches(\'[data-turn="user"]\')||root.querySelector(user))return;\n // Не возвращаем старый ответ, пока новый запрос ещё обрабатывается.\n if(lastUser && !(lastUser.compareDocumentPosition(root)&Node.DOCUMENT_POSITION_FOLLOWING))return;\n if(!roots.includes(root))roots.push(root);\n};\nfor(const e of document.querySelectorAll(\'[data-message-author-role="assistant"],[data-turn="assistant"],\'+article)){\n if(e.matches(article)&&!e.matches(\'[data-turn="assistant"]\')&&\n    !e.querySelector(\'[data-message-author-role="assistant"],\'+content))continue;\n add(e);\n}\nif(!roots.length){\n for(const e of document.querySelectorAll(\'main \'+content.split(\',\').join(\',main \')+\',main [class*="agent-turn"]\')){\n  if(e.matches(\'[class*="agent-turn"]\')&&!e.querySelector(content)&&!e.innerText.trim())continue;\n  add(e);\n }\n}\nroots.sort((a,b)=>a===b?0:(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1));\nconst el=roots.length?roots[roots.length-1]:null;\nconst texts=[];\nconst put=t=>{t=(t||\'\').trim();if(allowed(t)&&!texts.includes(t))texts.push(t);};\nconst read=e=>{\n const clone=e.cloneNode(true);\n for(const x of clone.querySelectorAll(\'button,svg,[role="button"],\'+excluded))x.remove();\n // innerText у detached DOM не учитывает переносы; заменяем разделители.\n for(const x of clone.querySelectorAll(\'br\'))x.replaceWith(document.createTextNode(\'\\n\'));\n for(const x of clone.querySelectorAll(\'p,div,pre,li\'))x.append(document.createTextNode(\'\\n\'));\n return clone.textContent||\'\';\n};\nif(el){\n const blocks=Array.from(el.querySelectorAll(content)).filter(e=>visible(e)&&!skip(e));\n const outer=blocks.filter(e=>!blocks.some(x=>x!==e&&x.contains(e)));\n if(outer.length)put(outer.map(read).join(\'\\n\'));\n const pres=Array.from(el.querySelectorAll(\'pre\')).filter(e=>visible(e)&&!skip(e));\n if(pres.length)put(pres.map(e=>(e.querySelector(\'code\')||e).textContent||\'\').join(\'\\n\'));\n for(const e of pres)put((e.querySelector(\'code\')||e).textContent);\n if(!texts.length)put(read(el));\n}\n// Новая разметка может не содержать main/article/markdown вообще:\n// пользовательский XPath указывает прямо на p/text(). Ищем текстовые\n// узлы с ключом reactions, а не React ID _r_8b_ (он меняется).\nconst fallback=[];\nconst safe=e=>visible(e)&&!skip(e)&&!e.closest(\'aside,nav,header,button,[role="button"]\')&&\n (!lastUser||!!(lastUser.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING));\nconst walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);\nconst seeds=[];\nwhile(walker.nextNode()){\n const node=walker.currentNode;\n if(/"(?:reactions|chapters)"\\s*:/.test(node.nodeValue||\'\')&&safe(node.parentElement))seeds.push(node.parentElement);\n}\n// Подсветка кода может разделить сам ключ на несколько span/text().\nfor(const e of document.querySelectorAll(\'p,pre,code\')){\n if(safe(e)&&/"(?:reactions|chapters)"\\s*:/.test(e.textContent||\'\')&&!seeds.includes(e))seeds.push(e);\n}\nseeds.sort((a,b)=>a===b?0:(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1));\nfor(const seed of seeds.reverse()){\n let e=seed;\n for(let depth=0;e&&e!==document.body&&depth<7;depth++,e=e.parentElement){\n  if(!safe(e)||e.querySelector(user+\',form,[contenteditable="true"]\'))break;\n  const t=read(e).trim();\n  // Не захватываем область с полным запросом, меню или всем диалогом.\n  if(promptNorm&&norm(t).includes(promptNorm))break;\n  if(t.length>300000)break;\n  if(/"(?:reactions|chapters)"\\s*:/.test(t)&&allowed(t)&&!fallback.includes(t))fallback.push(t);\n }\n}\n// Резервные кандидаты доступны даже если основные селекторы нашли\n// пустой блок. Полный JSON выбирает Python-парсер, частичный не принимается.\nfor(const t of fallback.slice(0,64))put(t);\nconst stops=Array.from(document.querySelectorAll(\'button[data-testid="stop-button"],button[data-testid="composer-stop-button"],\'+\n \'[data-testid="stop-generating"],button[aria-label]\')).filter(e=>visible(e)&&(\n /stop-button|composer-stop-button|stop-generating/.test(e.getAttribute(\'data-testid\')||\'\')||\n /^(stop( generating| streaming| response)?|остановить( генерацию| ответ)?|прекратить генерацию)$/i.test(e.getAttribute(\'aria-label\')||\'\')));\nconst busy=stops.length>0||!!(el&&Array.from(el.querySelectorAll(\'[data-is-streaming="true"],[data-testid="streaming-cursor"]\')).some(visible));\nconst rx=/oaiusercontent|estuary\\/content|\\/backend-api\\/files|persistent\\.oaistatic|videos\\.openai/;\nconst own=el?Array.from(el.querySelectorAll(\'img\')).filter(i=>!skip(i)):[];\nconst doc=Array.from(document.querySelectorAll(\'img\')).filter(i=>!skip(i)&&rx.test(i.currentSrc||i.src||\'\'));\nconst seen=[];\nfor(const im of own.concat(doc)){\n const u=im.currentSrc||im.src||\'\';\n if(!u||u.startsWith(\'data:\')||seen.some(x=>x.u===u))continue;\n seen.push({u:u,ok:!!(im.complete&&im.naturalWidth>0)});\n}\n// Only explicit UI notices in the current answer, not transcript/prompt/reasoning text.\nconst noticeRE=/^(?:этот контент нельзя показать из соображений безопасности|this content (?:isn[\'’]t|cannot be|can[\'’]t be) (?:shown|displayed)(?: for safety reasons)?|something went wrong while generating the response|a network error occurred|network error|произошла ошибка при создании ответа|ошибка сети)(?:[.\\s]|$)/i;\nconst safetyRE=/^(?:этот контент нельзя показать из соображений безопасности|this content)/i;\nconst notices=[];\nconst noticeWalker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);\nwhile(noticeWalker.nextNode()){\n const node=noticeWalker.currentNode;\n let t=(node.nodeValue||\'\').trim();\n if(!t||t.length>600||!safe(node.parentElement))continue;\n if(!noticeRE.test(t)&&/^(?:этот контент|this content|something went wrong|a network error)/i.test(t)){\n  let parent=node.parentElement;\n  for(let depth=0;parent&&depth<3;depth++,parent=parent.parentElement){\n   if(!safe(parent)||parent.querySelector(user+\',form,[contenteditable="true"]\'))break;\n   const joined=(parent.textContent||\'\').trim();\n   if(joined.length>600)break;\n   if(noticeRE.test(joined)){t=joined;break;}\n  }\n }\n if(!noticeRE.test(t)||!allowed(t)||priorNotices.includes(t))continue;\n if(!notices.includes(t))notices.push(t);\n}\nconst blocked=notices.find(t=>safetyRE.test(t))||\'\';\nconst technical=notices.find(t=>!safetyRE.test(t))||\'\';\nreturn {blocked:blocked,technical_error:technical,notices:notices,n:roots.length||seeds.length,fallbacks:fallback.length,text:texts[0]||\'\',texts:texts,imgs:seen,busy:busy,\n source:el?(el.getAttribute(\'data-message-author-role\')||el.getAttribute(\'data-testid\')||el.tagName):(fallback.length?\'dom-text/json\':\'none\'),\n expired:' + JS_EXPIRED_RE + ".test(document.body.innerText||'')};\n})()"

    def _read_answer_state(self):
        expression = self.JS_STATE.replace('const pending="";', 'const pending=' + json.dumps(getattr(self, '_pending_prompt', ''), ensure_ascii=True) + ';', 1)
        expression = expression.replace('const prior=[];', 'const prior=' + json.dumps(getattr(self, '_answer_baseline', []), ensure_ascii=True) + ';', 1)
        expression = expression.replace('const priorNotices=[];', 'const priorNotices=' + json.dumps(getattr(self, '_notice_baseline', []), ensure_ascii=True) + ';', 1)
        state = self.tab.js(expression)
        return state if isinstance(state, dict) else {}
    IMG_GRACE_SECONDS = 120
    IDLE_GRACE_SECONDS = 60

    def _wait_answer(self, known: set=None, page: str=None, until=None) -> tuple[str, list]:
        tab = self.tab
        known = set(known or ())
        deadline = time.time() + self._remaining(self.timeout)
        started = time.time()
        stable, last_key, text = (0, None, '')
        complete = False
        self._answer_unreadable = False
        images: list[str] = []
        stuck_since = None
        idle_since = None
        t_log = time.time()
        status_at = 0
        empty_idle_since = None
        self._log('читатель ответа v26: сообщения + реакции/главы в DOM')
        while time.time() < deadline:
            try:
                state = self._read_answer_state()
            except Exception as e:
                self._log(f'страница не ответила ({str(e)[:60]}) — жду')
                time.sleep(5)
                continue
            if time.time() - t_log >= 60:
                t_log = time.time()
                st = state or {}
                self._log(f"жду ответ: {int(time.time() - started)} с, {('пишет/думает' if st.get('busy') else 'тихо')}, текста {len(st.get('text') or '')} зн., источник {st.get('source', 'none')}, резервных {st.get('fallbacks', 0)}")
            if page and (not (state or {}).get('n')) and (time.time() - started > 20) and (self._page_id() != page):
                raise ChatGPTWebError('страница перезагрузилась — запрос потерялся')
            if state:
                if time.time() - status_at >= 15:
                    status_at = time.time()
                    callback = getattr(self, 'status_callback', None)
                    if callback:
                        try:
                            callback(f"ChatGPT: ожидание ответа {int(time.time()-started)} с; " +
                                     ('генерация' if state.get('busy') else 'нет признаков генерации') +
                                     f", найдено символов: {len(state.get('text') or '')}")
                        except Exception: pass
                if state.get('blocked'):
                    self._safety_blocked = True
                    self._answer_unreadable = True
                    self.last_answer = str(state['blocked'])
                    raise ChatGPTSafetyError('ChatGPT скрыл ответ из соображений безопасности. Запрос остановлен; повтора и смены аккаунта не будет. Транскрипт сохранён в кэше')
                if state.get('technical_error') and (not state.get('busy')):
                    self._answer_unreadable = True
                    self.last_answer = str(state['technical_error'])
                    raise ChatGPTTechnicalError('ChatGPT сообщил о технической ошибке генерации')
                if state.get('expired'):
                    raise ChatGPTWebError('сессия ChatGPT истекла — аккаунт надо перелогинить')
                candidates = [_clean_text(t) for t in state.get('texts') or [state.get('text', '')]]
                cur_text = candidates[0] if candidates else ''
                if until is not None:
                    for candidate in candidates:
                        try:
                            if candidate and until(candidate):
                                cur_text = candidate
                                break
                        except Exception:
                            pass
                self.last_answer = cur_text
                # A structured text request must not wait for or download unrelated UI images.
                items = [] if until is not None else [it for it in state.get('imgs') or [] if it.get('u') and it['u'] not in known]
                cur_imgs = [it['u'] for it in items]
                img_ready = all((it.get('ok') for it in items))
                busy = bool(state.get('busy'))
                if until is not None and not busy and not cur_text:
                    empty_idle_since = empty_idle_since or time.time()
                    if time.time() - empty_idle_since > 300:
                        self._answer_unreadable = True
                        raise ChatGPTWebError('ChatGPT не удалось прочитать ответ за 5 минут без признаков генерации; повторной отправки не будет')
                else:
                    empty_idle_since = None
                if cur_imgs and (not img_ready):
                    if stuck_since is None:
                        stuck_since = time.time()
                    if time.time() - stuck_since > self.IMG_GRACE_SECONDS:
                        self._log(f'картинка не догрузилась за {self.IMG_GRACE_SECONDS} с — беру как есть')
                        img_ready = True
                if busy or (cur_imgs and (not img_ready)):
                    stable = 0
                    last_key = None
                    idle_since = None
                    text = cur_text
                elif cur_imgs or cur_text:
                    key = (cur_text, tuple(cur_imgs))
                    if key == last_key:
                        stable += 1
                    else:
                        stable = 0
                        last_key = key
                    text, images = (cur_text, cur_imgs)
                    if stable >= (3 if cur_imgs else 2):
                        if until is None:
                            complete = True
                            break
                        try:
                            ready = bool(until(cur_text))
                        except Exception:
                            ready = False
                        if ready:
                            complete = True
                            self._log(f"прочитан полный ответ: {len(cur_text)} знаков, источник {state.get('source', 'assistant')}")
                            break
                        idle_since = idle_since or time.time()
                        if time.time() - idle_since > self.IDLE_GRACE_SECONDS:
                            self.last_answer = cur_text
                            self._answer_unreadable = True
                            raise ChatGPTWebError('ChatGPT закончил ответ, но полного JSON с таймингами нет')
                else:
                    stable = 0
                    last_key = None
            time.sleep(2)
        if until is not None and (not complete):
            self.last_answer = text
            self._answer_unreadable = True
            raise ChatGPTWebError('истекло время ожидания полного ответа ChatGPT; транскрипт остаётся в кэше')
        if not images and until is None:
            try:
                extra = self._read_answer_state()
                images = [it['u'] for it in extra.get('imgs') or [] if it.get('u') and it['u'] not in known]
                text = text or _clean_text(extra.get('text', ''))
            except Exception:
                pass
        paths = []
        for url in images:
            try:
                paths.append(self._download(url))
            except Exception as e:
                self._log(f'картинка не скачалась: {str(e)[:80]}')
        return (text, paths)
    JS_FETCH_IMAGE = '(async()=>{const r=await fetch(%s);const b=await r.blob();return await new Promise(res=>{const f=new FileReader();f.onload=()=>res(f.result);f.readAsDataURL(b);});})()'
    JS_IMG_READY = '(()=>{const im=Array.from(document.images).find(i=>(i.currentSrc||i.src)===%s);return !!im&&im.complete&&im.naturalWidth>0;})()'
    JS_IMG_BOX = "(()=>{const im=Array.from(document.images).find(i=>(i.currentSrc||i.src)===%s);if(!im)return null;im.scrollIntoView({block:'center'});const r=im.getBoundingClientRect();return {x:r.x+window.scrollX,y:r.y+window.scrollY,w:r.width,h:r.height};})()"

    def _download(self, url: str) -> str:
        if not self._wait_img_loaded(url):
            self._log('картинка не отметилась загруженной — пробую вслепую')
        last = None
        for attempt in range(3):
            try:
                data_url = self.tab.js(self.JS_FETCH_IMAGE % json.dumps(url), timeout=120)
                if data_url and ',' in data_url:
                    return self._save_data_url(data_url)
                last = ChatGPTWebError('пустой data-url')
            except Exception as e:
                last = e
                self._log(f'fetch не удался ({attempt + 1}/3): {str(e)[:70]}')
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
        try:
            return self._shoot_image(url)
        except Exception as e:
            last = e
        raise ChatGPTWebError(f'картинка не скачалась: {str(last)[:120]}')

    def _wait_img_loaded(self, url: str, timeout: int=90) -> bool:
        expr = self.JS_IMG_READY % json.dumps(url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.tab.js(expr, timeout=15):
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _shoot_image(self, url: str) -> str:
        box = self.tab.js(self.JS_IMG_BOX % json.dumps(url), timeout=30)
        if not box or box.get('w', 0) < 16 or box.get('h', 0) < 16:
            raise ChatGPTWebError('элемент картинки не нашли')
        res = self.tab.call('Page.captureScreenshot', format='png', clip={'x': box['x'], 'y': box['y'], 'width': box['w'], 'height': box['h'], 'scale': 1}, captureBeyondViewport=True)
        data = (res or {}).get('data')
        if not data:
            raise ChatGPTWebError('пустой снимок')
        self._log('картинка снята с элемента (fetch не сработал)')
        return self._save_data_url(f'data:image/png;base64,{data}', '_shot')

    def _save_data_url(self, data_url: str, suffix: str='') -> str:
        if ',' not in data_url:
            raise ChatGPTWebError('пустой data-url')
        raw = base64.b64decode(data_url.split(',', 1)[1])
        path = os.path.join(self.out_dir, f'cgpt_{int(time.time())}_{len(raw) % 10000}{suffix}.png')
        with open(path, 'wb') as fh:
            fh.write(raw)
        self._log(f'сохранено {path} ({len(raw)} байт)')
        return path

    def _log(self, msg: str):
        if self.verbose:
            print(f'[cgpt-web] {msg}', flush=True)

    def close(self):
        with self._profile_lock:
            try:
                if self.tab:
                    self.dump_cookies()
                    self.tab.close()
            except Exception:
                pass
            self.tab = None
            if self.proc:
                proc, self.proc = (self.proc, None)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self._log('браузер закрыт, профиль сохранён')
