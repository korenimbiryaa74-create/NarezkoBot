# Ported from narezkobot/narezka-restored; comments and docstrings omitted for transport.
import base64
import json
import math
import threading
import unicodedata
import os
import subprocess
import socket
import time
import urllib.request
import websocket
import proxy_setup
import proxy_bridge
import chatgpt_attach
DEFAULT_CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
BASE = 'https://chatgpt.com'
_PROFILE_LOCKS = {}
_PROFILE_LOCKS_GUARD = threading.Lock()

def _cookie_domain(value):
    value = str(value or '').lstrip('.').lower()
    return any((value == d or value.endswith('.' + d) for d in ('chatgpt.com', 'openai.com')))

from chatgpt_errors import ChatGPTWebError, ChatGPTSafetyError, ChatGPTLoginError, ChatGPTQuotaError, ChatGPTTechnicalError, ChatGPTUploadFailed, ChatGPTIncompleteAnswer

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
        self._file_chooser_events = []
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
                    if msg.get('method') == 'Page.fileChooserOpened':
                        self._file_chooser_events.append(msg.get('params') or {})
                    if msg.get('id') == self._id:
                        if 'error' in msg:
                            raise ChatGPTWebError(f"{method}: {msg['error'].get('message')}")
                        return msg.get('result', {})
            except (websocket.WebSocketConnectionClosedException, websocket.WebSocketTimeoutException, OSError, ValueError) as e:
                last = e
                non_replayable=(method=='DOM.setFileInputFiles' or
                    (method=='Runtime.evaluate' and params.get('expression') in
                     (chatgpt_attach.JS_OPEN_ATTACH,chatgpt_attach.JS_CLICK_UPLOAD)))
                if attempt == 0 and not non_replayable:
                    self._reconnect()
                    continue
                raise ChatGPTWebError(f'CDP-соединение умерло ({method}): {str(e)[:120]}')
        raise ChatGPTWebError(f'CDP: {last}')

    def clear_file_chooser(self):
        self._file_chooser_events.clear()

    def wait_file_chooser(self, seconds=3):
        deadline=time.monotonic()+seconds
        try:
            while True:
                if self._file_chooser_events:
                    return self._file_chooser_events.pop(0)
                remaining=deadline-time.monotonic()
                if remaining<=0:return None
                self.ws.settimeout(remaining)
                try: msg=json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException: return None
                if msg.get('method')=='Page.fileChooserOpened':
                    self._file_chooser_events.append(msg.get('params') or {})
        finally:
            self.ws.settimeout(self.timeout)

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

_OWN_CHROME_PIDS = set()  # Chrome processes started by this app run (never closed as «stale»)


def _patch_js(js, pairs):
    """Apply exact text replacements to an injected script; a missing fragment is a programming error."""
    for old, new in pairs:
        if old not in js:
            raise RuntimeError('chatgpt_web: не найден фрагмент скрипта для обновления: ' + old[:60])
        js = js.replace(old, new, 1)
    return js


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

    def _close_stale_chrome(self):
        """The port is held by a ChatGPT Chrome left over from a previous run of the app (same gpt_accounts
        folder, not started by this process): close it so the requested account can start."""
        if os.name != 'nt':
            return False
        base = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(self.profile))))
        try:
            out = subprocess.run(['powershell', '-NoProfile', '-Command',
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
                "ForEach-Object { [string]$_.ProcessId + [char]9 + $_.CommandLine }"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout or ''
        except (OSError, subprocess.SubprocessError):
            return False
        killed = False
        for line in out.splitlines():
            pid, _, cmd = line.partition('\t')
            if not pid.strip().isdigit() or int(pid) in _OWN_CHROME_PIDS or '--type=' in cmd:
                continue
            if f'--remote-debugging-port={self.port}' in cmd and base in os.path.normcase(cmd.replace('"', '')):
                self._log(f'порт {self.port} занят ChatGPT-браузером от прошлого запуска (PID {pid}) — закрываю его')
                try:
                    subprocess.run(['taskkill', '/PID', pid.strip(), '/T', '/F'], capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    killed = True
                except (OSError, subprocess.SubprocessError):
                    pass
        if not killed:
            return False
        for _ in range(20):
            if not self._debug_port_busy():
                return True
            time.sleep(.5)
        return False

    def _debug_port_busy(self):
        try:
            with socket.create_connection(('127.0.0.1',self.port),timeout=.3): return True
        except OSError:
            return False

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if self.proc:
            self.proc = None
            self.tab = None
        if self._debug_port_busy() and not self._close_stale_chrome():
            raise ChatGPTWebError('Порт браузера ChatGPT занят; закройте старое окно приложения и повторите')
        args = [self._chrome_path(), f'--remote-debugging-port={self.port}', '--remote-debugging-address=127.0.0.1', f'--user-data-dir={self.profile}', '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble', '--disable-blink-features=AutomationControlled', '--remote-allow-origins=*', '--window-size=1400,1000']
        if self.headless:
            args.append('--headless=new')
        elif self.offscreen:
            args.append('--window-position=-32000,-32000')
        bridge_args = proxy_bridge.chrome_args()
        if bridge_args:
            args.extend(bridge_args)
            self._log(f'прокси: {proxy_setup.describe()}')
        self._mark_clean_exit()
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _OWN_CHROME_PIDS.add(self.proc.pid)
        self._log(f'Chrome pid {self.proc.pid}')
        for _ in range(60):
            try:
                _http_json(f'http://127.0.0.1:{self.port}/json/version')
                return
            except Exception:
                time.sleep(0.5)
        raise ChatGPTWebError('Chrome не поднял CDP-порт')

    def _mark_clean_exit(self):
        """Chrome was killed last time -> it shows «Восстановить страницы?». Mark the profile as cleanly closed."""
        prefs = os.path.join(self.profile or '', 'Default', 'Preferences')
        try:
            with open(prefs, encoding='utf-8') as f:
                data = json.load(f)
            prof = data.setdefault('profile', {})
            if prof.get('exit_type') == 'Normal' and prof.get('exited_cleanly') is True:
                return
            prof['exit_type'] = 'Normal'
            prof['exited_cleanly'] = True
            with open(prefs, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

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
                self._status('ожидаю загрузки и проверки входа; страницу не перезагружаю')
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
    _EDITOR = "Array.from(document.querySelectorAll('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea')).find(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.disabled&&!e.readOnly&&(e.isContentEditable||e.tagName==='TEXTAREA'))"
    JS_COMPOSER_TEXT = "(()=>{const e="+_EDITOR+";return e?(e.tagName==='TEXTAREA'?e.value:e.innerText):null;})()"
    JS_FOCUS_EDITOR = "(()=>{const e="+_EDITOR+";if(!e)return false;e.scrollIntoView({block:'center'});e.focus();if(!e.contains(document.activeElement))return false;if(e.tagName==='TEXTAREA')e.select();else{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r);}return true;})()"
    JS_USER_MSGS = 'document.querySelectorAll(\'[data-message-author-role="user"],[data-turn="user"]\').length'
    JS_SEND_STATE = '(()=>{\nconst e=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst visible=n=>!!n&&n.getClientRects().length>0&&getComputedStyle(n).visibility!==\'hidden\';\nconst label=n=>(n.getAttribute(\'aria-label\')||n.getAttribute(\'title\')||\'\').trim();\nconst stopLabel=n=>/^(?:stop(?:\\s|$)|остановить(?:\\s|$)|прекратить(?:\\s|$))/i.test(label(n));\nconst sendLabel=n=>/^(?:send(?:\\s|$)|submit(?:\\s|$)|отправить(?:\\s|$))/i.test(label(n));\nconst known=\'button[data-testid="send-button"],button[data-testid="composer-submit-button"],button#composer-submit-button\';\nlet scope=e?(e.closest(\'form\')||e.parentElement):null;\nif(scope&&!scope.matches(\'form\')){\n  for(let i=0;i<5&&scope;i++){\n    if(scope.querySelector(known)||Array.from(scope.querySelectorAll(\'button\')).some(sendLabel))break;\n    scope=scope.parentElement;\n  }\n}\nscope=scope||document;\nconst buttons=Array.from(scope.querySelectorAll(\'button\')).filter(visible);\nconst stop=Array.from(document.querySelectorAll(\'button[data-testid="stop-button"],button[data-testid="composer-stop-button"],button[data-testid="stop-generating"]\')).some(visible)||buttons.some(stopLabel);\nconst b=buttons.find(n=>n.matches(known)&&!stopLabel(n))||buttons.find(n=>sendLabel(n)&&!stopLabel(n))||\n  (scope.tagName===\'FORM\'?buttons.find(n=>n.type===\'submit\'&&!stopLabel(n)):null);\nconst uploading=Array.from(scope.querySelectorAll(\'[role="progressbar"],[aria-busy="true"],.animate-spin,[data-state="uploading"]\')).some(visible);\nconst alerts=Array.from(scope.querySelectorAll(\'[role="alert"],[data-testid="file-upload-error"],[data-testid="upload-error"],[data-state="error"]\')).filter(visible).map(n=>n.innerText||\'\').join(\' \');\nconst uploadError=/(?:upload|file|файл|загруз)/i.test(alerts)&&/(?:error|fail|ошиб|не удалось|невозможно)/i.test(alerts);\nconst text=e?(e.value!==undefined?e.value:(e.innerText||e.textContent||\'\')):\'\';\nlet x=null,y=null,hit=false,key=null;\nif(b){\n  if(!b.dataset.twlSend)b.dataset.twlSend=String(Math.random());key=b.dataset.twlSend;\n  const r=b.getBoundingClientRect();x=r.x+r.width/2;y=r.y+r.height/2;\n  const top=document.elementFromPoint(x,y);hit=!!top&&(top===b||b.contains(top));\n}\nreturn {has:!!b,disabled:!b||b.disabled||b.getAttribute(\'aria-disabled\')===\'true\',stop,\n uploading,uploadError,text,x,y,hit,key,\n users:document.querySelectorAll(\'[data-message-author-role="user"],[data-turn="user"]\').length,\n assistants:document.querySelectorAll(\'[data-message-author-role="assistant"],[data-turn="assistant"]\').length};\n})()'
    # 2026-11 markup: user turns are data-user-message-bubble / ...:user; network upload failures are shown as a
    # «Загрузка не удалась» tile in the composer and a toast at the top of the page, not as role=alert in the form.
    JS_SEND_STATE = _patch_js(JS_SEND_STATE, (
        ('const uploadError=',
         'const failText=/загрузка не удалась|не удалось загрузить|проблем[аы]? с сетью|upload failed|failed to upload/i;\n'
         'const pageFail=Array.from(document.querySelectorAll(\'[role="alert"],[role="status"],[data-sonner-toast],li[data-type="error"]\'))'
         '.filter(visible).some(n=>failText.test(n.innerText||\'\'))||failText.test((scope&&scope.innerText)||\'\');\n'
         'const uploadError=pageFail||'),
        ('users:document.querySelectorAll(\'[data-message-author-role="user"],[data-turn="user"]\').length',
         # Only visible turns outside the composer: ChatGPT keeps hidden copies of earlier chats in the DOM,
         # and counting them made a never-sent prompt look «sent».
         'users:(()=>{const s=\'[data-message-author-role="user"],[data-turn="user"],[data-user-message-bubble="true"],'
         '[data-content-search-unit-key$=":user"]\';return Array.from(document.querySelectorAll(s)).filter(n=>visible(n)&&'
         '!n.closest(\'form\')&&!(n.parentElement&&n.parentElement.closest(s))).length;})()'),
    ))
    # Click probe: a capturing listener counts every pointerdown/click that really reaches the send button.
    # After a press we then know whether the button got the click (the page just ignored it) or the click
    # never landed on it at all (overlay, re-render) — and can safely press again.
    JS_SEND_STATE = _patch_js(JS_SEND_STATE, (
        ('let x=null,y=null,hit=false,key=null;',
         "const pr=window.__twlProbe||(()=>{const p=window.__twlProbe={down:0,click:0};"
         "const sel='button[data-testid=\"send-button\"],button[data-testid=\"composer-submit-button\"],button#composer-submit-button,button[data-twl-send]';"
         "const on=k=>ev=>{const t=ev.target&&ev.target.closest&&ev.target.closest('button');if(t&&t.matches(sel))p[k]++;};"
         "document.addEventListener('pointerdown',on('down'),true);document.addEventListener('click',on('click'),true);return p;})();\n"
         'let x=null,y=null,hit=false,key=null;'),
        ('uploading,uploadError,text,x,y,hit,key,', 'uploading,uploadError,text,x,y,hit,key,pdown:pr.down,pclick:pr.click,'),
    ))

    @staticmethod
    def _click_landed(st: dict, base) -> bool | None:
        """True — the page received the click on the send button, False — it did not, None — unknown."""
        try:
            now, before = int(st['pclick']), int(base)
        except (KeyError, TypeError, ValueError):
            return None
        if now < before:  # page reloaded, the counter restarted
            return None
        return now > before

    JS_CLICK_SEND = "(()=>{\nconst state=%s;\nconst b=Array.from(document.querySelectorAll('button')).find(n=>n.dataset.twlSend===state.key);\nif(!b||b.disabled||b.getAttribute('aria-disabled')==='true'||!b.getClientRects().length)return false;\nb.click();return true;\n})()"

    # Promo/upsell modals («Получите месяц ChatGPT Plus бесплатно» и т.п.) steal focus from the composer, so the
    # prompt can't be typed. Close every such window with its own close button (крестик / «Не сейчас»).
    # Windows about limits, login or upload errors are left alone: other checks read them.
    # Choice windows without a close button (memory onboarding «Использовать память / Не использовать память»)
    # are answered with the preferred option, otherwise Escape does not close them and the composer stays blocked.
    JS_DISMISS = r'''((dry)=>{
const vis=n=>{if(!n||!n.getClientRects().length)return false;const s=getComputedStyle(n);return s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0';};
const composer=document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea');
const keep=/лимит|limit|quota|квот|загрузка не удалась|upload failed|войд|войти|log ?in|sign ?in|сеанс|session|ошибк|error|подтверд|verify|captcha/i;
const sel='[role="dialog"],[role="alertdialog"],[aria-modal="true"],[data-testid^="modal"],[data-testid*="-modal"]';
let roots=Array.from(document.querySelectorAll(sel)).filter(vis).filter(d=>!(composer&&d.contains(composer))&&!d.closest('form'));
if(composer){
  const r=composer.getBoundingClientRect();
  if(r.width&&r.height){
    const h=document.elementFromPoint(r.x+Math.min(r.width/2,200),r.y+r.height/2);
    if(h&&h!==composer&&!composer.contains(h)&&!h.contains(composer)&&!h.closest('form')){
      let p=h,fix=null;
      while(p&&p!==document.body){const cs=getComputedStyle(p);if(cs.position==='fixed')fix=p;p=p.parentElement;}
      if(fix&&!fix.contains(composer)&&!roots.includes(fix))roots.push(fix);
    }
  }
}
roots=roots.filter(d=>!roots.some(o=>o!==d&&o.contains(d)));
const lab=n=>(n.getAttribute('aria-label')||n.getAttribute('title')||'').trim();
const closeLabel=/^(?:close|закрыть|dismiss|скрыть|hide)(?![a-zа-яё])/i;
const prefer=/^(?:использовать память|включить память|use memory|turn on memory|enable memory)(?![a-zа-яё])/i;
const first=n=>((n.innerText||'').trim().split('\n')[0]||'').trim();
const closeText=/^(?:close|закрыть|dismiss|maybe later|not now|не сейчас|позже|может быть,? позже|нет,? спасибо|no,? thanks|skip|пропустить|got it|понятно|ок|ok)$/i;
const items=[];
for(const d of roots){
  const text=(d.innerText||'').trim().replace(/\s+/g,' ');
  if(keep.test(text)){items.push({text:text.slice(0,90),kept:true});continue;}
  const btns=Array.from(d.querySelectorAll('button,[role="button"]')).filter(vis);
  let b=btns.find(n=>/memory-choice-true/i.test(n.getAttribute('aria-labelledby')||'')||prefer.test(first(n)));
  if(!b)b=btns.find(n=>closeLabel.test(lab(n))||/close/i.test(n.getAttribute('data-testid')||''));
  if(!b)b=btns.find(n=>closeText.test((n.innerText||'').trim()));
  if(!b){const r=d.getBoundingClientRect();b=btns.find(n=>{const q=n.getBoundingClientRect();return !(n.innerText||'').trim()&&n.querySelector('svg')&&q.top-r.top<80&&r.right-q.right<80;});}
  if(b&&!dry)b.click();
  items.push({text:text.slice(0,90),button:b?(lab(b)||first(b)||'крестик'):null});
}
return {items};
})'''

    def _dismiss_popups(self, force: bool=False) -> int:
        """Close promo/upsell popups that block the composer. Returns how many were closed."""
        now = time.monotonic()
        if not force and now - getattr(self, '_last_dismiss', 0.0) < 2.0:
            return 0
        self._last_dismiss = now
        try:
            res = self.tab.js('(' + self.JS_DISMISS + ')(false)') or {}
        except Exception:
            return 0
        items = res.get('items') or [] if isinstance(res, dict) else []
        closed = [i for i in items if i.get('button')]
        stuck = [i for i in items if not i.get('button') and not i.get('kept')]
        for i in closed:
            self._log(f'закрыл всплывающее окно «{i.get("text", "")[:70]}» (кнопка: {i.get("button")})')
        if stuck:
            try:
                for kind in ('keyDown', 'keyUp'):
                    self.tab.call('Input.dispatchKeyEvent', type=kind, key='Escape', code='Escape',
                                  windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
                self._log(f'окно без крестика «{stuck[0].get("text", "")[:70]}» — нажал Escape')
            except Exception:
                pass
        if closed or stuck:
            time.sleep(0.8)
        return len(closed)

    def _wait_ready(self, wait: int=None, settle: float=2.5) -> dict | None:
        deadline = time.time() + self._remaining(wait or self.load_timeout)
        mark, since = (None, 0.0)
        next_status=time.monotonic()+15
        while time.time() < deadline:
            if time.monotonic() >= next_status:
                self._status('ожидаю готовности поля ввода в браузере')
                next_status=time.monotonic()+15
            self._dismiss_popups()
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
            raise ChatGPTLoginError('В этом профиле требуется вход в ChatGPT')
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
            self._dismiss_popups(force=True)
            if not tab.js(self.JS_FOCUS_EDITOR):
                self._wait_ready(30)
                continue
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
        clicks = 0
        last_click = 0.0
        missed = 0          # presses that never reached the button
        probe_base = None   # click counter of the button right before the last press
        probe_down = None
        confirmed = False   # the last press reached the button
        use_dom = False     # alternate the press method on retries
        stable_key = None
        stable = 0
        next_log = 0.0
        self._log('текст в поле; жду завершения загрузки файла и активную кнопку отправки')
        try:
            tab.call('Page.bringToFront')
        except Exception:
            pass
        while time.monotonic() < deadline:
            self._dismiss_popups()
            st = tab.js(self.JS_SEND_STATE) or {}
            if int(st.get('users', 0)) > before_users or int(st.get('assistants', 0)) > before_assistants or (st.get('stop') and (clicks or not baseline.get('stop'))):
                self._send_uncertain = False
                self._log('отправка подтверждена: новый ход чата / генерация')
                self._sent_confirmed = True
                return True
            self._check_upload_quota()
            if st.get('uploadError'):
                if not clicks:
                    raise ChatGPTUploadFailed('ChatGPT: «Загрузка не удалась» — файл не принят, запрос НЕ отправлен')
                raise ChatGPTWebError('Ошибка загрузки после клика; доставка неизвестна. Проверьте открытый чат')
            current = self._norm(st.get('text', ''))
            # Nothing was sent: no new turn, no generation, the same full text is still in the field, button active.
            unsent = bool(st.get('has') and not st.get('stop') and not st.get('disabled') and not st.get('uploading')
                          and current and (not expected or current == expected))
            reclick = False
            if clicks:
                since = time.monotonic() - last_click
                landed = self._click_landed(st, probe_base)
                if landed and not confirmed:
                    confirmed = True
                    self._log('клик дошёл до кнопки «Отправить» — страница получила нажатие')
                real = clicks - missed  # presses that reached the button (or unknown)
                if unsent and landed is False and since >= 1.0 and missed < 3:
                    reclick = True
                    missed += 1
                    use_dom = not use_dom
                    redraw = self._click_landed({'pclick': st.get('pdown')}, probe_down)
                    why = 'кнопка перерисовалась во время нажатия' if redraw else 'нажатие перехватил другой элемент'
                    self._status('клик не дошёл до кнопки — текст всё ещё в поле; нажимаю «Отправить» ещё раз')
                    self._log(f'клик не дошёл до кнопки ({why}); запрос точно не ушёл — '
                              f'нажимаю ещё раз {"через DOM" if use_dom else "мышью"} ({missed}/3)')
                if not reclick:
                    if time.monotonic() >= next_log:
                        seen = ' (клик дошёл до кнопки)' if landed else ' (проверяю, дошёл ли клик)' if landed is False else ''
                        can_retry = landed is False and missed < 3
                        if unsent and can_retry:
                            left = max(1, int(8 - since)) if landed is not False else 1
                            self._status(f'кнопка нажата{seen}; жду отправку, если не уйдёт — через {left} с нажму ещё раз ({real + 1}/3)')
                        else:
                            why = 'попытки нажатия исчерпаны' if unsent else 'поле изменилось — запрос, вероятно, ушёл'
                            self._status(f'кнопка нажата{seen}; жду подтверждения отправки, повторного клика не будет ({why})')
                        self._log('кнопка нажата; жду подтверждения отправки')
                        next_log = time.monotonic() + 10
                    time.sleep(0.5)
                    continue
            ready = unsent
            # The page may replace the button on every render. Stabilize the payload,
            # then use the current button identity for the single click.
            fingerprint = current
            if ready:
                stable = stable + 1 if fingerprint == stable_key else 1
                stable_key = fingerprint
            else:
                stable = 0
                stable_key = None
            if time.monotonic() >= next_log:
                reason = 'идёт загрузка файла' if st.get('uploading') else 'кнопка ещё неактивна' if st.get('disabled') else 'кнопка не найдена' if not st.get('has') else 'проверяю полный текст' if current != expected else 'кнопка готова'
                self._status('подготовка отправки: '+reason)
                self._log('отправка: ' + reason)
                next_log = time.monotonic() + 10
            can_click = (stable >= 3 and clicks == 0) or reclick
            if can_click:
                previous_uncertainty = getattr(self, '_send_uncertain', False)
                self._send_uncertain = True
                base_click, base_down = st.get('pclick'), st.get('pdown')
                mouse = st.get('hit') and (st.get('x') is not None)
                done = False
                if mouse and not use_dom:
                    x, y = (st['x'], st['y'])
                    tab.call('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y)
                    tab.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
                    tab.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y, button='left', clickCount=1)
                    self._log('нажал кнопку отправки мышью; проверяю, дошёл ли клик')
                    done = True
                elif tab.js(self.JS_CLICK_SEND % json.dumps({'key': st.get('key')})):
                    self._log('нажал кнопку отправки через DOM; проверяю, дошёл ли клик')
                    done = True
                elif mouse:
                    x, y = (st['x'], st['y'])
                    tab.call('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y)
                    tab.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
                    tab.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y, button='left', clickCount=1)
                    self._log('кнопка через DOM не нажалась — нажал мышью; проверяю, дошёл ли клик')
                    done = True
                if not done:
                    self._send_uncertain = previous_uncertainty
                    stable = 0
                    time.sleep(0.5)
                    continue
                self._status('кнопка нажата; проверяю, дошёл ли клик и ушёл ли запрос')
                clicks += 1
                last_click = time.monotonic()
                probe_base, probe_down, confirmed = base_click, base_down, False
                next_log = 0.0
                stable = 0
            time.sleep(0.5)
        if clicks:
            self._send_uncertain = True
            raise ChatGPTWebError('на кнопку отправки нажал, но доставка не подтверждена; чат оставлен открытым, автоматически повторять запрос не буду')
        self._log('не удалось дождаться готового файла/кнопки; запрос НЕ отправлен')
        return False
    JS_PICK_FILE_INPUT = chatgpt_attach.JS_PICK_FILE_INPUT
    # A failed upload keeps its file name in the tile, so «named» alone is not success.
    JS_ATTACHED = _patch_js(chatgpt_attach.JS_ATTACHED, ((
        'if(named||blob)return {blob,named,busy};',
        'if(named||blob)return {blob,named,busy,failed:/загрузка не удалась|не удалось загрузить|upload failed|failed to upload/i.test(txt)};'),))
    JS_QUOTA = chatgpt_attach.JS_QUOTA

    def _check_upload_quota(self):
        try:
            state = self.tab.js(self.JS_QUOTA) or {}
        except Exception:
            return
        if isinstance(state, dict) and state.get('blocked'):
            self._quota_blocked = True
            raise ChatGPTQuotaError('ChatGPT: обнаружен лимит загрузки файлов')

    def _wait_attached(self, name: str, image: bool, seconds: int=300) -> bool:
        deadline = time.monotonic() + self._remaining(seconds)
        stable = 0
        next_status = time.monotonic()+15
        while time.monotonic() < deadline:
            if time.monotonic() >= next_status:
                self._status('ожидаю подтверждения прикреплённого файла (до 5 минут; запрос ещё не отправлен)')
                next_status=time.monotonic()+15
            self._check_upload_quota()
            try:
                state = self.tab.js(self.JS_ATTACHED % json.dumps(name)) or {}
                if state.get('failed') or (self.tab.js(self.JS_SEND_STATE) or {}).get('uploadError'):
                    raise ChatGPTUploadFailed('ChatGPT: «Загрузка не удалась» — файл не принят, запрос НЕ отправлен')
                exists = state.get('blob') or state.get('named') if image else state.get('named')
                stable = stable + 1 if exists and (not state.get('busy')) else 0
                if stable >= 2:
                    return True
            except ChatGPTUploadFailed:
                raise
            except Exception:
                stable = 0
            time.sleep(0.5)
        return False

    _attach = chatgpt_attach.attach

    def ask(self, prompt: str, files: list=None, until=None) -> dict:
        with self._profile_lock:
            self._send_uncertain = False
            self._sent_confirmed = False
            self._answer_unreadable = False
            self.last_answer = ''
            self._pending_prompt = ''
            self._answer_baseline = []
            self._notice_baseline = []
            self._safety_blocked = False
            self._quota_blocked = False
            self._request_deadline = time.monotonic() + float(self.timeout)
            try:
                for upload_try in range(3):
                    try:
                        return self._ask_locked(prompt, files, until)
                    except ChatGPTUploadFailed as exc:
                        # Nothing was sent: reload the page and attach + send again.
                        if self._send_uncertain or self._sent_confirmed or upload_try == 2 or self._remaining(60) < 30:
                            raise
                        self._log(f'{exc}; перезагружаю страницу и отправляю заново ({upload_try + 2}/3)')
                        self._status('загрузка файла не удалась — перезагружаю страницу и отправляю заново')
                        self._force_new_chat = True
                        self._send_uncertain = False
                        time.sleep(3)
            finally:
                self._request_deadline = None

    def _ask_locked(self, prompt: str, files: list=None, until=None) -> dict:
        last = 'неизвестно'
        for attempt in range(3):
            if self._remaining(1) <= 0:
                raise ChatGPTWebError('время отправки запроса истекло')
            self._status('подготавливаю браузер и проверяю вход')
            force, self._force_new_chat = attempt > 0 or getattr(self, '_force_new_chat', False), False
            if not self.new_chat(force=force):
                raise ChatGPTWebError('не залогинились в ChatGPT — куки протухли, переэкспортируй их из браузера')
            self._status('чат готов, проверяю интерфейс')
            tab = self.tab
            self._dismiss_popups(force=True)
            page = self._page_id()
            self._send_baseline = tab.js(self.JS_SEND_STATE) or {}
            known = set(tab.js(self.JS_IMGS) or [])
            initial = self._read_answer_state()
            self._answer_baseline = list(initial.get('texts') or [])
            self._notice_baseline = list(initial.get('notices') or [])
            attached = True
            for path in files or []:
                self._status('прикрепляю транскрипт; дождитесь загрузки')
                attached = self._attach(path) and attached
                time.sleep(1.5)
            docs = [p for p in files or [] if os.path.splitext(p)[1].lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.gif')]
            if docs and (not attached):
                raise ChatGPTWebError('транскрипт не прикрепился — запрос без файла НЕ отправлен; проверьте браузер и повторите вручную')
            self._status('файл прикреплён; ввожу запрос')
            self._log('файл готов; ввожу критерии поиска')
            if not self._type_prompt(prompt):
                last = 'текст не вводится в поле'
                if files: raise ChatGPTWebError(last + '; файл повторно не загружается, проверьте чат вручную')
                continue
            if self._page_id() != page:
                last = 'страница перезагрузилась во время ввода'
                if files: raise ChatGPTWebError(last + '; файл повторно не загружается, проверьте чат вручную')
                self._log(last + ' — заново')
                continue
            self._status('проверяю готовность файла и отправку запроса')
            if not self._send():
                last = 'файл или кнопка отправки не готовы; Enter не используется'
                if files: raise ChatGPTWebError(last + '; файл повторно не загружается, проверьте чат вручную')
                continue
            self._status('отправка подтверждена; ожидаю ответ')
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
    JS_STATE = '(()=>{\nconst pending="";\nconst prior=[];\nconst priorNotices=[];\nconst norm=t=>(t||\'\').normalize(\'NFC\').replace(/[\\u200B-\\u200D\\uFEFF\\s]+/g,\'\').toLowerCase();\nconst promptNorm=norm(pending);\nconst previous=new Set(prior.map(norm));\nconst allowed=t=>{\n const n=norm(t);\n if(!n||previous.has(n)||Array.from(previous).some(p=>p.length>20&&n.includes(p)))return false;\n if(promptNorm&&(promptNorm.includes(n)||n.includes(promptNorm)))return false;\n return true;\n};\nconst visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!==\'hidden\';\nconst user=\'[data-message-author-role="user"],[data-turn="user"]\';\nconst excluded=\'[data-testid*="advert"],[data-testid*="sponsor"],[data-testid*="recommend"],\'+\n \'[data-ad],.adsbygoogle,[aria-label="Advertisement"],[aria-label="Реклама"],\'+\n \'[data-testid*="thinking"],[data-testid*="reasoning"],.result-thinking\';\nconst skip=e=>!!e.closest(user+\',\'+excluded+\',form,[contenteditable="true"]\');\nconst article=\'article[data-testid^="conversation-turn"],article[data-turn-id],article[data-turn]\';\nconst content=\'.markdown,.prose,[data-testid="markdown"],[data-testid="message-content"]\';\nconst users=Array.from(document.querySelectorAll(user)).filter(visible);\nconst lastUser=users.length?users[users.length-1]:null;\nconst roots=[];\nconst add=e=>{\n if(!visible(e)||skip(e))return;\n const root=e.closest(article)||e;\n if(skip(root)||root.matches(\'[data-turn="user"]\')||root.querySelector(user))return;\n if(lastUser && !(lastUser.compareDocumentPosition(root)&Node.DOCUMENT_POSITION_FOLLOWING))return;\n if(!roots.includes(root))roots.push(root);\n};\nfor(const e of document.querySelectorAll(\'[data-message-author-role="assistant"],[data-turn="assistant"],\'+article)){\n if(e.matches(article)&&!e.matches(\'[data-turn="assistant"]\')&&\n    !e.querySelector(\'[data-message-author-role="assistant"],\'+content))continue;\n add(e);\n}\nif(!roots.length){\n for(const e of document.querySelectorAll(\'main \'+content.split(\',\').join(\',main \')+\',main [class*="agent-turn"]\')){\n  if(e.matches(\'[class*="agent-turn"]\')&&!e.querySelector(content)&&!e.innerText.trim())continue;\n  add(e);\n }\n}\nroots.sort((a,b)=>a===b?0:(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1));\nconst el=roots.length?roots[roots.length-1]:null;\nconst texts=[];\nconst put=t=>{t=(t||\'\').trim();if(allowed(t)&&!texts.includes(t))texts.push(t);};\nconst read=e=>{\n const clone=e.cloneNode(true);\n for(const x of clone.querySelectorAll(\'button,svg,[role="button"],\'+excluded))x.remove();\n for(const x of clone.querySelectorAll(\'br\'))x.replaceWith(document.createTextNode(\'\\n\'));\n for(const x of clone.querySelectorAll(\'p,div,pre,li\'))x.append(document.createTextNode(\'\\n\'));\n return clone.textContent||\'\';\n};\nif(el){\n const blocks=Array.from(el.querySelectorAll(content)).filter(e=>visible(e)&&!skip(e));\n const outer=blocks.filter(e=>!blocks.some(x=>x!==e&&x.contains(e)));\n if(outer.length)put(outer.map(read).join(\'\\n\'));\n const pres=Array.from(el.querySelectorAll(\'pre\')).filter(e=>visible(e)&&!skip(e));\n if(pres.length)put(pres.map(e=>(e.querySelector(\'code\')||e).textContent||\'\').join(\'\\n\'));\n for(const e of pres)put((e.querySelector(\'code\')||e).textContent);\n if(!texts.length)put(read(el));\n}\nconst fallback=[];\nconst safe=e=>visible(e)&&!skip(e)&&!e.closest(\'aside,nav,header,button,[role="button"]\')&&\n (!lastUser||!!(lastUser.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING));\nconst walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);\nconst seeds=[];\nwhile(walker.nextNode()){\n const node=walker.currentNode;\n if(/"(?:reactions|chapters|donation_cuts)"\\s*:/.test(node.nodeValue||\'\')&&safe(node.parentElement))seeds.push(node.parentElement);\n}\nfor(const e of document.querySelectorAll(\'p,pre,code\')){\n if(safe(e)&&/"(?:reactions|chapters|donation_cuts)"\\s*:/.test(e.textContent||\'\')&&!seeds.includes(e))seeds.push(e);\n}\nseeds.sort((a,b)=>a===b?0:(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1));\nfor(const seed of seeds.reverse()){\n let e=seed;\n for(let depth=0;e&&e!==document.body&&depth<7;depth++,e=e.parentElement){\n  if(!safe(e)||e.querySelector(user+\',form,[contenteditable="true"]\'))break;\n  const t=read(e).trim();\n  if(promptNorm&&norm(t).includes(promptNorm))break;\n  if(t.length>300000)break;\n  if(/"(?:reactions|chapters|donation_cuts)"\\s*:/.test(t)&&allowed(t)&&!fallback.includes(t))fallback.push(t);\n }\n}\nfor(const t of fallback.slice(0,64))put(t);\nconst stops=Array.from(document.querySelectorAll(\'button[data-testid="stop-button"],button[data-testid="composer-stop-button"],\'+\n \'[data-testid="stop-generating"],button[aria-label]\')).filter(e=>visible(e)&&(\n /stop-button|composer-stop-button|stop-generating/.test(e.getAttribute(\'data-testid\')||\'\')||\n /^(stop( generating| streaming| response)?|остановить( генерацию| ответ)?|прекратить генерацию)$/i.test(e.getAttribute(\'aria-label\')||\'\')));\nconst busy=stops.length>0||!!(el&&Array.from(el.querySelectorAll(\'[data-is-streaming="true"],[data-testid="streaming-cursor"]\')).some(visible));\nconst rx=/oaiusercontent|estuary\\/content|\\/backend-api\\/files|persistent\\.oaistatic|videos\\.openai/;\nconst own=el?Array.from(el.querySelectorAll(\'img\')).filter(i=>!skip(i)):[];\nconst doc=Array.from(document.querySelectorAll(\'img\')).filter(i=>!skip(i)&&rx.test(i.currentSrc||i.src||\'\'));\nconst seen=[];\nfor(const im of own.concat(doc)){\n const u=im.currentSrc||im.src||\'\';\n if(!u||u.startsWith(\'data:\')||seen.some(x=>x.u===u))continue;\n seen.push({u:u,ok:!!(im.complete&&im.naturalWidth>0)});\n}\nconst noticeRE=/^(?:этот контент нельзя показать из соображений безопасности|this content (?:isn[\'’]t|cannot be|can[\'’]t be) (?:shown|displayed)(?: for safety reasons)?|something went wrong while generating the response|a network error occurred|network error|произошла ошибка при создании ответа|ошибка сети)(?:[.\\s]|$)/i;\nconst safetyRE=/^(?:этот контент нельзя показать из соображений безопасности|this content)/i;\nconst notices=[];\nconst noticeWalker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);\nwhile(noticeWalker.nextNode()){\n const node=noticeWalker.currentNode;\n let t=(node.nodeValue||\'\').trim();\n if(!t||t.length>600||!safe(node.parentElement))continue;\n if(!noticeRE.test(t)&&/^(?:этот контент|this content|something went wrong|a network error)/i.test(t)){\n  let parent=node.parentElement;\n  for(let depth=0;parent&&depth<3;depth++,parent=parent.parentElement){\n   if(!safe(parent)||parent.querySelector(user+\',form,[contenteditable="true"]\'))break;\n   const joined=(parent.textContent||\'\').trim();\n   if(joined.length>600)break;\n   if(noticeRE.test(joined)){t=joined;break;}\n  }\n }\n if(!noticeRE.test(t)||!allowed(t)||priorNotices.includes(t))continue;\n if(!notices.includes(t))notices.push(t);\n}\nconst blocked=notices.find(t=>safetyRE.test(t))||\'\';\nconst technical=notices.find(t=>!safetyRE.test(t))||\'\';\nreturn {blocked:blocked,technical_error:technical,notices:notices,n:roots.length||seeds.length,fallbacks:fallback.length,text:texts[0]||\'\',texts:texts,imgs:seen,busy:busy,\n source:el?(el.getAttribute(\'data-message-author-role\')||el.getAttribute(\'data-testid\')||el.tagName):(fallback.length?\'dom-text/json\':\'none\'),\n expired:' + JS_EXPIRED_RE + ".test(document.body.innerText||'')};\n})()"

    # ChatGPT markup from 2026-11 (new message renderer): there is no data-message-author-role any more.
    # Messages are marked with data-user-message-bubble / data-content-search-unit-key="…:user|…:assistant" and
    # data-markdown-text-style="assistant-message"; the turn state is data-talvt-turn-state.
    JS_STATE = _patch_js(JS_STATE, (
        ("const user='[data-message-author-role=\"user\"],[data-turn=\"user\"]';",
         "const user='[data-message-author-role=\"user\"],[data-turn=\"user\"],[data-user-message-bubble=\"true\"],"
         "[data-content-search-unit-key$=\":user\"]';"),
        ("const content='.markdown,.prose,[data-testid=\"markdown\"],[data-testid=\"message-content\"]';",
         "const content='.markdown,.prose,[data-testid=\"markdown\"],[data-testid=\"message-content\"],"
         "[data-markdown-text-style=\"assistant-message\"]';"),
        ("for(const e of document.querySelectorAll('[data-message-author-role=\"assistant\"],[data-turn=\"assistant\"],'+article)){",
         "for(const e of document.querySelectorAll('[data-message-author-role=\"assistant\"],[data-turn=\"assistant\"],"
         "[data-content-search-unit-key$=\":assistant\"],'+article)){"),
        ("const busy=stops.length>0||",
         "const busy=stops.length>0||Array.from(document.querySelectorAll('[data-talvt-turn-state]')).some(e=>"
         "/stream|progress|pending|generat|running/i.test(e.getAttribute('data-talvt-turn-state')||''))||"),
    ))

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
        gen_last, gen_stable = None, 0
        while time.time() < deadline:
            try:
                state = self._read_answer_state()
            except Exception as e:
                self._log(f'страница не ответила ({str(e)[:60]}) — жду')
                time.sleep(5)
                continue
            if until is None:
                # Image request: the picture is final when its source and size stay unchanged for ~6 s.
                try:
                    gen = [g for g in (tab.js(self.JS_GENERATED) or []) if g.split('#')[0] not in known]
                except Exception:
                    gen = []
                if gen:
                    key = tuple(gen)
                    gen_stable = gen_stable + 1 if key == gen_last else 0
                    gen_last = key
                    if gen_stable >= 3:
                        images = [g.split('#')[0] for g in gen]
                        text = _clean_text((state or {}).get('text', '') or '')
                        complete = True
                        self._log(f'сгенерированная картинка готова: {gen[-1].split("#")[1]}')
                        break
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
                    raise ChatGPTSafetyError('ChatGPT скрыл ответ из соображений безопасности')
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
                if not busy and not cur_text and not cur_imgs and not (until is None and gen_last):
                    empty_idle_since = empty_idle_since or time.time()
                    if until is not None and time.time() - empty_idle_since > 300:
                        self._answer_unreadable = True
                        raise ChatGPTWebError('ChatGPT не удалось прочитать ответ за 5 минут без признаков генерации; повторной отправки не будет')
                    if until is None and time.time() - empty_idle_since > 150:
                        # Image request with no picture, no text and no generation: e.g. an unreadable limit notice.
                        self._answer_unreadable = True
                        raise ChatGPTWebError('ChatGPT 2,5 минуты не генерирует картинку и ничего не отвечает')
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
                        idle_since = None
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
                            raise ChatGPTIncompleteAnswer('ChatGPT закончил ответ, но полного JSON с таймингами нет')
                else:
                    stable = 0
                    last_key = None
                    idle_since = None
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
    # Current UI: generated pictures live in a gallery with blob: sources (not caught by JS_IMGS).
    JS_GENERATED = "(()=>{const imgs=[...document.querySelectorAll('[data-testid=\"generated-image-gallery\"] img, img[alt^=\"Сгенерированное изображение\"], img[alt^=\"Generated image\"]')];return imgs.filter(i=>i.complete&&i.naturalWidth>256&&i.getClientRects().length).map(i=>(i.currentSrc||i.src)+'#'+i.naturalWidth+'x'+i.naturalHeight).filter(Boolean);})()"
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

    def _status(self, msg: str):
        # Only fixed, non-sensitive status messages, not page text or credentials.
        callback = getattr(self, 'status_callback', None)
        if callback:
            try: callback('ChatGPT: '+msg)
            except Exception: pass

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
                _OWN_CHROME_PIDS.discard(proc.pid)
                deadline=time.monotonic()+5
                while self._debug_port_busy() and time.monotonic()<deadline:
                    time.sleep(.1)
                self._log('браузер закрыт, профиль сохранён')
