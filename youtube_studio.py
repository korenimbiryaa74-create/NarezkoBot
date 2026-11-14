"""Experimental headed YouTube Studio integration using local Chrome profiles.

The user signs in in a normal browser window. No credential, session export,
CAPTCHA bypass, or automatic repeat of an uncertain upload is performed.
Studio is an unsupported, changing interface: fail closed on any ambiguity.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from youtube_queue import profile_key

CHANNEL = re.compile(r'^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)')
VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')
_LOCK = threading.Lock()  # устарел: больше не используется, оставлен для совместимости
# У каждого канала свой профиль Chrome, поэтому каналы работают параллельно. Замок канала держится
# только пока вкладка Studio активно управляется (окно загрузки, настройки канала, заявки); долгое
# ожидание обработки YouTube идёт без него, поэтому Shorts и другие задачи канала не ждут часами.
_PROFILE_LOCKS = {}
_PROFILE_GUARD = threading.Lock()
_BROWSER_USERS = {}


def _named_lock(name, factory=threading.RLock):
    with _PROFILE_GUARD:
        lock = _PROFILE_LOCKS.get(name)
        if lock is None:
            lock = _PROFILE_LOCKS[name] = factory()
        return lock


def profile_lock(profile):
    """Замок активного управления Studio одного канала (повторно входимый в том же потоке)."""
    return _named_lock('drive:' + profile_key(profile))


def _video_limit(profile):
    """Канал на паузе из-за дневного лимита полных видео (upload_limits.py)."""
    try:
        import upload_limits
        return upload_limits.active(profile, 'video')
    except Exception:  # noqa: BLE001
        return False


def batch_guard(profile):
    """Не больше одного пакета загрузки нарезок на канал одновременно."""
    return _named_lock('batch:' + profile_key(profile), threading.Lock)

class StudioError(RuntimeError): pass
class UnsafeUploadState(StudioError): pass


def channel_from_url(url):
    parsed = urlparse(str(url))
    match = CHANNEL.match(parsed.path)
    return match.group(1) if parsed.scheme == 'https' and parsed.hostname == 'studio.youtube.com' and match else None


def video_from_url(url):
    from urllib.parse import parse_qs
    p = urlparse(str(url))
    if p.scheme != 'https' or p.hostname not in ('youtu.be','youtube.com','www.youtube.com'):
        return None
    if p.hostname == 'youtu.be':
        value = p.path.strip('/')
    else:
        # Shorts dialogs link to youtube.com/shorts/<id>, not watch?v=<id>.
        parts = [x for x in p.path.split('/') if x]
        value = parts[1] if len(parts) == 2 and parts[0] in ('shorts','live','embed') else parse_qs(p.query).get('v',[''])[0]
    return value if VIDEO.fullmatch(value) else None


def chrome_path():
    paths = [os.environ.get('NAREZKO_CHROME',''),
        os.path.join(os.environ.get('PROGRAMFILES','C:\\Program Files'),'Google','Chrome','Application','chrome.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)','C:\\Program Files (x86)'),'Microsoft','Edge','Application','msedge.exe'),
        shutil.which('chromium') or '', shutil.which('google-chrome') or '']
    for p in paths:
        if p and os.path.isfile(p): return p
    raise StudioError('Не найден Google Chrome или Microsoft Edge')


def local_json(url, *, method='GET'):
    # CDP runs only on loopback. Never inherit system proxy credentials.
    req=urllib.request.Request(url, data=b'' if method == 'PUT' else None, method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=2) as r:
        return json.load(r)


class CDP:
    def __init__(self, websocket_url):
        import websocket
        self.websocket_url=websocket_url
        self.ws=websocket.create_connection(websocket_url,timeout=30,suppress_origin=True)
        self.seq=0
    def reconnect(self):
        import websocket
        self.close()
        self.ws=websocket.create_connection(self.websocket_url,timeout=30,suppress_origin=True)
        self.seq=0
    @staticmethod
    def is_disconnect(exc):
        import websocket
        error=getattr(websocket,'WebSocketException',None)
        return isinstance(exc,(OSError,EOFError,TimeoutError)) or (error is not None and isinstance(exc,error))
    def read_js(self, expression):
        # Only callers with read-only expressions may use this. Never replay clicks,
        # keystrokes, file selection, or publication after a lost connection.
        try:return self.js(expression)
        except Exception as exc:
            if not self.is_disconnect(exc):raise
            self.reconnect()
            return self.js(expression)
    def reload_monitor(self):
        # This is only used on the read-only content-list tab.
        try:self.call('Page.reload',ignoreCache=True)
        except Exception as exc:
            if not self.is_disconnect(exc):raise
            self.reconnect()
            self.call('Page.reload',ignoreCache=True)
    def call(self, method, **params):
        # Do not reconnect or replay a possibly delivered action.
        self.seq+=1
        current=self.seq
        self.ws.send(json.dumps({'id':current,'method':method,'params':params}))
        while True:
            result=json.loads(self.ws.recv())
            if result.get('id') != current: continue
            if 'error' in result: raise StudioError('Ошибка управления браузером: '+method)
            return result.get('result',{})
    def js(self, expression):
        data=self.call('Runtime.evaluate',expression=expression,returnByValue=True,awaitPromise=True)
        if data.get('exceptionDetails'): raise StudioError('Ошибка интерфейса YouTube Studio')
        return data.get('result',{}).get('value')
    def key(self,key, code, modifiers=0):
        self.call('Input.dispatchKeyEvent',type='keyDown',key=key,code=code,modifiers=modifiers,
                  windowsVirtualKeyCode=ord(key.upper()) if len(key)==1 else {'Enter':13,'Escape':27}.get(key,0))
        self.call('Input.dispatchKeyEvent',type='keyUp',key=key,code=code,modifiers=modifiers,
                  windowsVirtualKeyCode=ord(key.upper()) if len(key)==1 else {'Enter':13,'Escape':27}.get(key,0))
    def fill(self, selector, value):
        found=self.js('''(()=>{const es=[...document.querySelectorAll(%s)].filter(e=>e.getClientRects().length&&!e.disabled);if(es.length!==1)return false;es[0].focus();return document.activeElement===es[0]})()''' % json.dumps(selector))
        if not found: raise StudioError('Не удалось однозначно выбрать поле в YouTube Studio')
        self.key('a','KeyA',2); self.call('Input.insertText',text=str(value))
    def close(self):
        try:self.ws.close()
        except Exception:pass


class Browser:
    def __init__(self, root, profile):
        self.profile=Path(root)/'youtube_accounts'/profile_key(profile)/'profile'
        self.proc=None; self.tab=None; self.monitor_tab=None; self.port=None; self.keep_open=False; self.upload_tabs=[]; self.attempt_may_upload=False; self.attached=False
    def _running_port(self):
        """Debug port of a Chrome that already uses this profile (kept open for running uploads)."""
        if os.name!='nt':return None
        try:
            # The profile path contains Cyrillic: force UTF-8 output from PowerShell.
            out=subprocess.run(['powershell','-NoProfile','-Command',
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Select -Expand CommandLine"],
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout or ''
        except (OSError,subprocess.SubprocessError):
            return None
        prof=str(self.profile).lower().rstrip('\\')
        found=False
        for line in out.splitlines():
            low=line.lower().replace('"','')
            if prof in low and '--type=' not in low:
                found=True
                m=re.search(r'--remote-debugging-port=(\d+)',line)
                if m:return int(m.group(1))
        return 0 if found else None

    def _attach(self,port):
        """Use the already open Chrome: open a fresh Studio tab, never touch tabs with running uploads."""
        page=local_json(f'http://127.0.0.1:{port}/json/new?https://studio.youtube.com/?hl=en',method='PUT')
        if not page.get('webSocketDebuggerUrl'):raise StudioError('Не удалось открыть вкладку в уже запущенном Chrome YouTube')
        self.port=port;self.attached=True
        self.tab=CDP(page['webSocketDebuggerUrl']);self.tab.call('Runtime.enable');self.tab.call('DOM.enable')
        return self.tab

    def _user_key(self):
        return os.path.normcase(str(self.profile))

    def __enter__(self):
        # Запуск/подключение к Chrome профиля — строго по одному, чтобы две задачи не запустили
        # два Chrome на одном профиле. Счётчик пользователей не даёт одной задаче закрыть Chrome,
        # которым ещё пользуется другая (например, Shorts во время ожидания обработки нарезок).
        with _named_lock('launch:' + self._user_key(), threading.Lock):
            tab = self._enter()
            with _PROFILE_GUARD:
                _BROWSER_USERS[self._user_key()] = _BROWSER_USERS.get(self._user_key(), 0) + 1
            self._counted = True
            return tab

    def _enter(self):
        self.profile.mkdir(parents=True,exist_ok=True)
        self.attached=False
        running=self._running_port()
        if running:
            try:return self._attach(running)
            except (OSError,ValueError,KeyError):pass
        if running==0:
            raise StudioError('Профиль YouTube уже открыт в окне Chrome без связи с ботом. Закройте это окно Chrome (если в нём не идут загрузки) и повторите')
        # Reserve a random local port; Chrome refuses an already used port.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        self.port=port
        args=[chrome_path(),f'--user-data-dir={self.profile}',f'--remote-debugging-port={port}',
              '--remote-debugging-address=127.0.0.1','--no-first-run','--no-default-browser-check',
              # Chrome stops rendering a window that Windows reports as covered (a game, another app):
              # Studio menus never open and upload fields stay invisible. Keep the bot's window always rendering.
              '--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding',
              '--disable-background-timer-throttling','--disable-features=CalculateNativeWinOcclusion',
              '--lang=en-US','https://studio.youtube.com/?hl=en']
        self.proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        end=time.monotonic()+35
        try:
            while time.monotonic()<end:
                if self.proc.poll() is not None: raise StudioError('Браузер закрылся; возможно, профиль уже открыт')
                try:
                    pages=local_json(f'http://127.0.0.1:{port}/json/list')
                    page=next((p for p in pages if p.get('type')=='page' and p.get('webSocketDebuggerUrl') and (p.get('url','').startswith('https://studio.youtube.com/') or p.get('url','').startswith('https://accounts.google.com/'))),None)
                    if page:
                        self.tab=CDP(page['webSocketDebuggerUrl']); self.tab.call('Runtime.enable'); self.tab.call('DOM.enable')
                        return self.tab
                except (OSError,ValueError,KeyError):pass
                time.sleep(.5)
            raise StudioError('Не удалось запустить браузер YouTube')
        except Exception:
            self.__exit__(None,None,None)
            raise
    def open_monitor(self,channel_id,section='upload'):
        """Open Studio's content page in a separate tab; do not navigate the upload tab.

        section: 'upload' = the «Видео» list, 'short' = the «Shorts» list (Shorts never appear under «Видео»)."""
        result=self.tab.call('Target.createTarget',url=f'https://studio.youtube.com/channel/{channel_id}/videos/{section}?hl=en',background=True)
        target=result.get('targetId')
        if not target:raise StudioError('Не удалось открыть страницу проверки обработки')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                pages=local_json(f'http://127.0.0.1:{self.port}/json/list')
                page=next((p for p in pages if p.get('id')==target and p.get('webSocketDebuggerUrl')),None)
                if page:
                    self.monitor_tab=CDP(page['webSocketDebuggerUrl'])
                    self.monitor_tab.call('Runtime.enable')
                    return self.monitor_tab
            except (OSError,ValueError,KeyError):pass
            time.sleep(.5)
        raise StudioError('Не удалось подключиться к странице проверки обработки')

    def open_upload_tab(self,channel_id):
        # Each submitted upload stays open while its file is transferred.
        result=self.tab.call('Target.createTarget',url=f'https://studio.youtube.com/channel/{channel_id}?hl=en',background=False)
        target=result.get('targetId')
        if not target:raise StudioError('Не удалось открыть вкладку следующей загрузки')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                pages=local_json(f'http://127.0.0.1:{self.port}/json/list')
                page=next((p for p in pages if p.get('id')==target and p.get('webSocketDebuggerUrl')),None)
                if page:
                    tab=CDP(page['webSocketDebuggerUrl']);tab.call('Runtime.enable');tab.call('DOM.enable')
                    self.upload_tabs.append(tab)
                    # Studio menus can fail to initialise in a background page.
                    # Activate only the new tab; earlier transfers stay open.
                    tab.call('Page.bringToFront')
                    return tab
            except (OSError,ValueError,KeyError):pass
            time.sleep(.5)
        raise StudioError('Не удалось подключиться к вкладке загрузки')

    def __exit__(self,*args):
        others = 0
        if getattr(self, '_counted', False):
            with _PROFILE_GUARD:
                others = max(0, _BROWSER_USERS.get(self._user_key(), 1) - 1)
                _BROWSER_USERS[self._user_key()] = others
            self._counted = False
        for tab in self.upload_tabs:tab.close()
        self.upload_tabs=[]
        if self.monitor_tab:self.monitor_tab.close();self.monitor_tab=None
        if self.tab:self.tab.close();self.tab=None
        if self.proc:
            if not self.keep_open and not others:
                self.proc.terminate()
                try:self.proc.wait(timeout=6)
                except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=6)
            # On uncertain upload, leave Chrome open so it cannot be aborted by us.
            self.proc=None


def wait(tab, expression, timeout, message):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=tab.js(expression)
        if value: return value
        time.sleep(.6)
    raise StudioError(message)


# Restrict fallbacks to the title container: never mistake description for title.
_TITLE_SELECTORS = (
    'ytcp-uploads-dialog #title-textarea #textbox',
    'ytcp-uploads-dialog #title-textarea [contenteditable="true"]',
    'ytcp-uploads-dialog #title-textarea textarea',
    'ytcp-uploads-dialog #title-textarea input',
)

_DESCRIPTION_BOX="ytcp-uploads-dialog #description-textarea #textbox"


def _focus_end(tab, selector):
    """Focus the single visible field and put the caret at its end."""
    return bool(tab.js('''(()=>{const es=[...document.querySelectorAll(%s)].filter(e=>e.getClientRects().length);
        if(es.length!==1)return false;const e=es[0];
        if(document.activeElement!==e)e.focus();
        if(document.activeElement!==e)return false;
        const s=getSelection(),r=document.createRange();r.selectNodeContents(e);r.collapse(false);
        s.removeAllRanges();s.addRange(r);return true})()''' % json.dumps(selector)))


def ensure_title(tab, selector, title, progress=None):
    """Re-type the title if anything changed it (e.g. text typed while Studio moved the focus)."""
    norm=lambda s:' '.join(str(s or '').split())
    for _ in range(3):
        got=tab.js('''(()=>{const e=[...document.querySelectorAll(%s)].find(e=>e.getClientRects().length);
            return e?(e.value!==undefined&&e.tagName!=='DIV'?e.value:e.innerText):null})()''' % json.dumps(selector))
        if norm(got)==norm(title):return True
        if progress:progress('Название изменилось в Studio — ввожу заново')
        tab.fill(selector,title);time.sleep(.6)
    raise StudioError('Не удалось ввести название в YouTube Studio')


def fill_description(tab, text, progress=None):
    """Type the description line by line (Enter between lines keeps the line breaks).

    A failure here never blocks the upload: the video is still published without it."""
    text=str(text or '').replace('\r\n','\n').replace('<','').replace('>','').strip('\n')
    if not text:return False
    norm=lambda s:[l.strip() for l in str(s).replace('\r','').split('\n') if l.strip()]
    try:
        for attempt in range(2):
            if not _focus_end(tab,_DESCRIPTION_BOX):
                if progress:progress('Поле описания не найдено — видео будет без описания')
                return False
            tab.key('a','KeyA',2)
            tab.call('Input.dispatchKeyEvent',type='keyDown',key='Delete',code='Delete',windowsVirtualKeyCode=46)
            tab.call('Input.dispatchKeyEvent',type='keyUp',key='Delete',code='Delete',windowsVirtualKeyCode=46)
            lines=text.split('\n');ok=True
            for i,line in enumerate(lines):
                # Studio may move focus to the title while the draft loads: re-check before every
                # keystroke so no text ever lands in the title field.
                if not _focus_end(tab,_DESCRIPTION_BOX):ok=False;break
                if line:tab.call('Input.insertText',text=line)
                if i<len(lines)-1:
                    if not _focus_end(tab,_DESCRIPTION_BOX):ok=False;break
                    tab.call('Input.dispatchKeyEvent',type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r')
                    tab.call('Input.dispatchKeyEvent',type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13)
            time.sleep(.8)
            got=tab.js('''(()=>{const e=document.querySelector(%s);return e?e.innerText:null})()''' % json.dumps(_DESCRIPTION_BOX)) or ''
            if ok and norm(got)==norm(text):
                if progress:progress('Описание заполнено')
                return True
            time.sleep(1)
        if progress:progress('Описание заполнено не полностью — проверьте его в Studio')
        return False
    except Exception:
        if progress:progress('Не удалось заполнить описание — видео будет без описания')
        return False


class VerificationRequired(StudioError):
    """YouTube refused a >15 min video before creating a draft: the channel needs phone verification."""


class UploadLimitReached(StudioError):
    """Channel's daily upload limit: Studio keeps a draft but will not upload it for 24 hours."""


_LIMIT_TEXT=r"""(()=>{const d=[...document.querySelectorAll('ytcp-uploads-dialog')].find(e=>e.getClientRects().length);
  const t=(d?d.innerText:'')||'';
  return /(?:загрузка недоступна|можно будет через 24 часа|daily upload limit|upload limit reached|try again in 24 hours)/i.test(t)})()"""


_VERIFY_DIALOG=r"""(()=>{const vis=e=>!!e.getClientRects().length;
  return [...document.querySelectorAll('ytcp-dialog,tp-yt-paper-dialog,[role=dialog],ytcp-confirmation-dialog')].some(e=>vis(e)&&
    /(?:длиннее 15 минут|longer than 15 minutes|подтвердите (?:номер телефона|личность)|verify (?:your )?(?:phone|identity))/i.test(e.innerText||''))})()"""


def wait_for_title(tab, progress=None, timeout=180):
    """Wait for one visible title field. Do not re-select the file or dismiss errors."""
    deadline=time.monotonic()+timeout
    next_report=time.monotonic()+20
    script="""(()=>{const selectors=%s;
        const visible=e=>!!e.getClientRects().length && !e.disabled && e.getAttribute('aria-disabled')!=='true';
        for(const selector of selectors){const matches=[...document.querySelectorAll(selector)].filter(visible);
            if(matches.length===1)return {selector:selector};
            if(matches.length>1)return {ambiguous:true};}
        const dialogs=[...document.querySelectorAll('ytcp-uploads-dialog')].filter(visible);
        return {dialog:dialogs.length, title:dialogs.some(d=>!!d.querySelector('#title-textarea'))};})()""" % json.dumps(_TITLE_SELECTORS)
    while time.monotonic()<deadline:
        state=tab.js(script) or {}
        if state.get('selector') in _TITLE_SELECTORS:return state['selector']
        if tab.js(_VERIFY_DIALOG):
            raise VerificationRequired('YouTube требует подтвердить номер телефона, чтобы загружать видео длиннее 15 минут. '
                                       'Подтвердите в Studio (Настройки → Канал → Доступность функций) и повторите')
        if state.get('ambiguous'):
            raise StudioError('Найдено несколько полей названия; проверьте окно YouTube Studio')
        if progress and time.monotonic()>=next_report:
            progress('Ожидаю поле названия в YouTube Studio; проверьте окно браузера и не закрывайте его')
            next_report=time.monotonic()+20
        time.sleep(.6)
    raise StudioError('Не появилось поле названия после выбора файла. Проверьте окно Studio, сообщения об ошибке и черновики; браузер оставлен открытым')


def channel_ready(tab, expected=None, timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        data=tab.js("({url:location.href,ready:!!document.querySelector('ytcp-navigation-drawer')})")
        actual=channel_from_url((data or {}).get('url',''))
        if actual and (data or {}).get('ready'):
            if expected and actual!=expected:
                raise StudioError('В браузере выбран другой канал YouTube; загрузка не выполнялась')
            return actual
        time.sleep(1)
    raise StudioError('Войдите в Google и откройте нужный канал YouTube Studio')


def click(tab, selector, timeout=30, message=None):
    script='''(()=>{const es=[...document.querySelectorAll(%s)].filter(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');if(es.length!==1)return false;es[0].scrollIntoView({block:'center'});es[0].click();return true})()''' % json.dumps(selector)
    # A false return means no click was issued. A CDP error must never be replayed.
    return wait(tab,script,timeout,message or 'Не найден однозначный элемент YouTube Studio')


_CREATE_BUTTON='ytcp-header .ytcpAppHeaderCreateIcon'


def _upload_item_js(do_click):
    """Find the single visible «Добавить видео» item of the open «Создать» menu (test-id first, then exact text)."""
    action="es[0].scrollIntoView({block:'center'});es[0].click();return 'clicked'" if do_click else "return 'ready'"
    return r"""(()=>{const vis=e=>!!e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true';
 let es=[...document.querySelectorAll('tp-yt-paper-item[test-id="upload"]')].filter(vis);
 if(!es.length)es=[...document.querySelectorAll('ytcp-text-menu tp-yt-paper-item,tp-yt-paper-item[role=menuitem]')]
   .filter(e=>vis(e)&&/^(?:добавить видео|загрузить видео|upload videos?)$/i.test((e.innerText||'').trim()));
 if(es.length!==1)return es.length;%s})()""" % action


def open_upload_menu(tab, timeout=45, create_message=None, item_message=None):
    """Open «Создать» → «Добавить видео».

    Right after Studio loads, the «Создать» button can exist before its handler is attached, so the
    first click may not open the menu. Re-click «Создать» only while the menu is closed (never toggles
    an open menu shut) and click the menu item only when exactly one is visible.
    """
    deadline=time.monotonic()+timeout; create_clicks=0
    create_js='''(()=>{const es=[...document.querySelectorAll(%s)].filter(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');if(es.length!==1)return false;es[0].scrollIntoView({block:'center'});es[0].click();return true})()''' % json.dumps(_CREATE_BUTTON)
    while time.monotonic()<deadline:
        state=tab.js(_upload_item_js(True))
        if state=='clicked':return True
        if state==0 and create_clicks<4:
            if tab.js(create_js):
                create_clicks+=1
                menu_deadline=min(deadline,time.monotonic()+5)
                while time.monotonic()<menu_deadline:
                    if tab.js(_upload_item_js(False))=='ready':break
                    time.sleep(.4)
                continue
        time.sleep(.6)
    if create_clicks==0:
        raise StudioError(create_message or 'Не удалось однозначно найти кнопку «Создать» в Studio')
    raise StudioError(item_message or 'Не удалось однозначно найти пункт «Добавить видео» в меню Studio')



# Read only the precise Studio message shown when access already exists and
# Studio itself asks for a reload.  Never reload an active/ambiguous upload.
_PRE_UPLOAD_STATE = r"""(()=>{
 const visible=e=>!!e.getClientRects().length;
 const access=/(?:у вас уже есть доступ к этой функции|you already have access to this feature)/i;
 const refresh=/(?:попробуйте обновить страницу|try refreshing (?:the )?page)/i;
 const ds=[...document.querySelectorAll('ytcp-dialog,ytcp-confirm-dialog,tp-yt-paper-dialog,[role="dialog"]')]
  .filter(e=>visible(e)&&access.test(e.innerText||'')&&refresh.test(e.innerText||''));
 const leaf=ds.filter(e=>!ds.some(x=>x!==e&&e.contains(x)));
 const uploads=[...document.querySelectorAll('ytcp-uploads-dialog')].filter(visible);
 const inputs=[...document.querySelectorAll('input[type="file"]')];
 const files=inputs.some(e=>e.files&&e.files.length);
 const links=uploads.some(d=>[...d.querySelectorAll('a[href]')].some(a=>
   /(?:youtu\.be\/[A-Za-z0-9_-]{11}|\/video\/[A-Za-z0-9_-]{11}(?:\/|$)|[?&]v=[A-Za-z0-9_-]{11})/.test(a.href)));
 return {popup:leaf.length, safe:leaf.length===1&&uploads.length===1&&!files&&!links,
         ready:uploads.length===1&&!!uploads[0].querySelector('input[type="file"]'), origin:performance.timeOrigin};
})()"""
_PRE_UPLOAD_RELOAD = """(()=>{const s=__STATE__;if(!s.popup||!s.safe)return 'unsafe';
 setTimeout(()=>{const next=__STATE__;if(next.safe)location.reload()},50);return 'refreshing'})()""".replace('__STATE__',_PRE_UPLOAD_STATE)


def prepare_upload_dialog(tab, expected, progress=None, timeout=30):
    """Recover one exact pre-file Studio prompt by refreshing only this tab."""
    refreshed=False
    while True:
        channel_ready(tab,expected)
        if progress:progress('Открываю меню «Создать» → «Добавить видео» в новой вкладке Studio')
        # Studio does not open its menus in a background/minimised window: bring the tab forward and
        # make the page believe it has focus (also for a Chrome started before the anti-throttling flags).
        if hasattr(tab,'call'):
            for method,params in (('Page.bringToFront',{}),('Emulation.setFocusEmulationEnabled',{'enabled':True})):
                try:tab.call(method,**params)
                except Exception:pass
        try:
            open_upload_menu(tab,create_message='Не удалось однозначно найти кнопку «Создать» в Studio; файл ещё не выбран',
                             item_message='Не удалось однозначно найти пункт «Добавить видео» в меню Studio; файл ещё не выбран')
        except StudioError:
            if not expected or not hasattr(tab,'call'):raise
            # No file is selected yet, so opening the upload dialog by its direct link is safe.
            if progress:progress('Меню «Создать» не открылось — открываю окно загрузки по прямой ссылке')
            tab.call('Page.navigate',url=f'https://studio.youtube.com/channel/{expected}/videos/upload?d=ud&hl=en')
            wait(tab,"!!document.querySelector('ytcp-uploads-dialog input[type=file]')",45,
                 'Не открылось окно загрузки Studio ни через меню, ни по прямой ссылке; файл ещё не выбран')
        deadline=time.monotonic()+timeout;ready_since=None;restart=False
        while time.monotonic()<deadline:
            state=tab.js(_PRE_UPLOAD_STATE) or {}
            if state.get('popup'):
                if not state.get('safe'):
                    raise UnsafeUploadState('Studio просит обновить страницу, но текущая вкладка может содержать загрузку; проверьте Studio')
                if refreshed:
                    raise StudioError('Studio повторно просит обновить страницу; файл не выбран')
                if tab.js(_PRE_UPLOAD_RELOAD)!='refreshing':
                    raise UnsafeUploadState('Не удалось безопасно обновить вкладку Studio; проверьте текущую загрузку')
                refreshed=True;restart=True
                if progress:progress('Studio попросила обновить страницу; обновляю только вкладку до выбора файла')
                old_origin=state.get('origin');load_deadline=time.monotonic()+45
                while time.monotonic()<load_deadline:
                    try:
                        info=tab.js('({origin:performance.timeOrigin,ready:document.readyState})') or {}
                        if old_origin is not None and info.get('origin')!=old_origin and info.get('ready')=='complete':break
                    except StudioError:
                        pass  # navigation can destroy the old execution context
                    time.sleep(.5)
                else:raise StudioError('Вкладка Studio не обновилась; файл не выбран')
                break
            if state.get('ready'):
                if ready_since is None:ready_since=time.monotonic()
                if time.monotonic()-ready_since>=2:return
            else:ready_since=None
            time.sleep(.5)
        if not restart:raise StudioError('Не найден выбор видео или его закрыло сообщение Studio; файл не выбран')


def selected(tab, selector):
    return tab.js('''(()=>{const es=[...document.querySelectorAll(%s)];return es.length===1&&es[0].getAttribute('aria-checked')==='true'})()''' % json.dumps(selector))


def find_thumbnail(video):
    """Newest '<clip> — ИИ-превью*.jpg/png' next to the clip; re-encoded if over YouTube's 2 MB limit."""
    video=Path(video)
    try:
        found=[p for p in video.parent.iterdir() if p.is_file() and p.name.startswith(video.stem+' — ИИ-превью') and p.suffix.lower() in ('.jpg','.jpeg','.png')]
    except OSError:
        return None
    if not found:return None
    best=max(found,key=lambda p:p.stat().st_mtime)
    if best.stat().st_size<=2_000_000:return best
    try:
        from PIL import Image
        out=Path(tempfile.gettempdir())/('narezko-thumb-'+best.stem[:40].replace(' ','_')+'.jpg')
        with Image.open(best) as im:
            im=im.convert('RGB');im.thumbnail((1280,720))
            for q in (90,82,74,66):
                im.save(out,'JPEG',quality=q,optimize=True)
                if out.stat().st_size<=2_000_000:return out
    except Exception:
        return None
    return None


_THUMB_INPUTS=("ytcp-uploads-dialog ytcp-thumbnail-uploader input#file-loader",
               "ytcp-uploads-dialog input[type=file][accept*=image]")
_THUMB_SET=r"""(()=>{const d=document.querySelector('ytcp-uploads-dialog');if(!d)return false;
  const imgs=[...d.querySelectorAll('ytcp-thumbnails-compact-editor-uploader img, ytcp-thumbnail-uploader img, #still-picker img, ytcp-video-custom-still-editor img, ytcp-thumbnail-editor img')];
  return imgs.some(i=>{const s=i.currentSrc||i.src||'';return (s.startsWith('blob:')||s.startsWith('data:image')||s.indexOf('ytimg.com')>=0)&&i.getClientRects().length>0;});})()"""


def attach_thumbnail(tab,path,progress=None):
    """Upload the custom thumbnail. Never fails the upload: returns False and reports instead."""
    try:
        selector=None
        for sel in _THUMB_INPUTS:
            state=tab.js('''(()=>{const es=[...document.querySelectorAll(%s)];return {n:es.length,dis:es.length===1&&es[0].disabled}})()''' % json.dumps(sel)) or {}
            if state.get('n')==1:
                if state.get('dis'):
                    if progress:progress('YouTube не даёт загрузить свой значок на этом канале; видео загружается без превью')
                    return False
                selector=sel;break
        if not selector:
            if progress:progress('Поле значка не найдено; видео загружается без превью')
            return False
        info=tab.call('DOM.getDocument',depth=0)
        node=tab.call('DOM.querySelector',nodeId=info['root']['nodeId'],selector=selector).get('nodeId')
        if not node:
            if progress:progress('Поле значка не найдено; видео загружается без превью')
            return False
        tab.call('DOM.setFileInputFiles',nodeId=node,files=[str(Path(path).resolve())])
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if tab.js(_THUMB_SET):
                if progress:progress('Превью добавлено как значок видео')
                return True
            time.sleep(.5)
        if progress:progress('Превью отправлено, но Studio не показала его за 25 с; проверьте значок в Studio')
        return True
    except Exception as exc:
        if progress:progress('Не удалось добавить превью ('+type(exc).__name__+': '+str(exc)[:120]+'); видео загружается без него')
        return False


def attach_file(tab,path):
    info=tab.call('DOM.getDocument',depth=0)
    result=tab.call('DOM.querySelector',nodeId=info['root']['nodeId'],selector='ytcp-uploads-dialog input[type="file"]')
    if not result.get('nodeId'): raise StudioError('Не найдено поле загрузки видео')
    tab.call('DOM.setFileInputFiles',nodeId=result['nodeId'],files=[str(Path(path).resolve())])


def local_schedule_timestamp(timestamp, tz_text, now=None):
    """Require an explicit Studio GMT/UTC offset matching the computer's local zone.

    The target's local UTC offset may differ across DST. We reject if the Studio
    displayed offset does not match it rather than silently scheduling wrongly.
    """
    now=time.time() if now is None else now
    if timestamp <= now + 120:
        raise StudioError('Запланированное время слишком близко или прошло; проверьте расписание вручную')
    matches=re.findall(r'\b(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?',str(tz_text),re.I)
    if len(set(matches)) != 1:
        raise StudioError('Не удалось прочитать часовой пояс YouTube Studio; откройте «Часовой пояс» и пришлите скриншот')
    sign,h,m=matches[0]; minutes=(int(h)*60+int(m or 0))*(1 if sign=='+' else -1)
    if abs(minutes)>14*60: raise StudioError('Некорректный часовой пояс YouTube')
    intended=datetime.fromtimestamp(timestamp).astimezone()
    if intended.utcoffset() != timedelta(minutes=minutes):
        raise StudioError('Часовой пояс YouTube не совпадает с часовым поясом компьютера')
    # Match the first publication's displayed minute (14:34 -> 15:34).
    # Studio controls have minute precision; never submit a time that is too close.
    intended=intended.replace(second=0,microsecond=0)
    if intended.timestamp() <= now + 120:
        raise StudioError('Запланированное время слишком близко или прошло; проверьте расписание вручную')
    return intended


_RU_MONTHS = ('янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек')


def parse_date(value):
    text=re.sub(r'\s+',' ',str(value).strip())
    russian=re.fullmatch(r'(\d{1,2})\s+([а-яё]+)\.?\s*,?\s*(\d{4})(?:\s*г\.?)?',text.lower())
    if russian:
        day,month,year=russian.groups()
        month=month[:3]
        if month=='май':month='мая'
        if month in _RU_MONTHS:
            try:return datetime(int(year),_RU_MONTHS.index(month)+1,int(day)).date()
            except ValueError:return None
    for fmt in ('%b %d, %Y','%B %d, %Y','%d %b %Y','%m/%d/%Y','%Y-%m-%d','%d.%m.%Y'):
        try:return datetime.strptime(text,fmt).date()
        except ValueError:pass
    return None


def parse_time(value):
    text=re.sub(r'\s+',' ',str(value).strip()).upper()
    for fmt in ('%H:%M','%I:%M %p','%I:%M%p'):
        try:return datetime.strptime(text,fmt).time()
        except ValueError:pass
    return None


# YouTube Studio may present Schedule as a collapsed card, not a visible radio.
# Restrict the action to one visible upload dialog and one unambiguous heading.
_OPEN_SCHEDULE = r"""(()=>{
  const visible=e=>!!e.getClientRects().length && !e.disabled && e.getAttribute('aria-disabled')!=='true';
  const dialogs=[...document.querySelectorAll('ytcp-uploads-dialog')].filter(visible);
  if(dialogs.length!==1)return dialogs.length>1?'ambiguous':null;
  const d=dialogs[0];
  const time=[...d.querySelectorAll('ytcp-visibility-scheduler #time-of-day-container input')].filter(visible);
  if(time.length===1)return 'ready';
  if(time.length>1)return 'ambiguous';
  const radios=[...d.querySelectorAll('#schedule-radio-button')].filter(visible);
  if(radios.length>1)return 'ambiguous';
  if(radios.length===1){radios[0].scrollIntoView({block:'center'});radios[0].click();return 'opened';}
  const exact=e=>/^(?:schedule|schedule publication|запланировать публикацию)$/i.test((e.innerText||'').trim());
  const headings=[...d.querySelectorAll('*')].filter(e=>visible(e)&&exact(e)&&
    ![...e.children].some(c=>visible(c)&&exact(c)));
  if(headings.length>1)return 'ambiguous';
  if(headings.length===1){headings[0].scrollIntoView({block:'center'});headings[0].click();return 'opened';}
  return null;
})()"""
_SCHEDULE_OPEN = """(()=>{const d=[...document.querySelectorAll('ytcp-uploads-dialog')].filter(e=>e.getClientRects().length);if(d.length!==1)return false;const es=[...d[0].querySelectorAll('ytcp-visibility-scheduler #time-of-day-container input')].filter(e=>e.getClientRects().length);return es.length===1})()"""
_SCHEDULE_ZONE = r"""(()=>{const e=document.querySelector('ytcp-uploads-dialog ytcp-visibility-scheduler');if(!e)return null;return [e.innerText,...[...e.querySelectorAll('[aria-label],[title]')].filter(x=>x.getClientRects().length).flatMap(x=>[x.getAttribute('aria-label'),x.getAttribute('title')])].filter(Boolean).join('\n')})()"""

def open_schedule(tab):
    state=wait(tab,_OPEN_SCHEDULE,30,'Не найдена карточка «Запланировать публикацию» в окне загрузки')
    if state=='ambiguous':raise StudioError('Карточка «Запланировать публикацию» найдена неоднозначно')
    wait(tab,_SCHEDULE_OPEN,15,'Карточка расписания не открылась или поле времени не найдено')


_DATE_STATE = r"""(()=>{
  const visible=e=>!!e.getClientRects().length;
  const triggers=[...document.querySelectorAll('ytcp-uploads-dialog #datepicker-trigger')].filter(visible);
  const pickers=[...document.querySelectorAll('ytcp-date-picker')].filter(p=>visible(p)||[...p.querySelectorAll('input')].some(visible));
  if(triggers.length!==1||pickers.length>1)return {ambiguous:true};
  const label=triggers[0].querySelector('.dropdown-trigger-text');
  const picker=pickers[0];
  const inputs=picker?[...picker.querySelectorAll('input')].filter(visible):[];
  if(inputs.length>1)return {ambiguous:true};
  const input=inputs[0];
  const messages=picker?[...picker.querySelectorAll('*')].filter(e=>visible(e)&&!e.children.length).map(e=>e.innerText||'').join(' '):'';
  const invalid=!!(input&&(input.getAttribute('aria-invalid')==='true'||input.matches(':invalid')||input.closest('[invalid]'))) || /(?:недопустимая дата|invalid date)/i.test(messages);
  return {date:label?label.innerText:triggers[0].innerText,open:!!input,invalid:invalid};
})()"""



def parse_calendar_month(value):
    text=re.sub(r'\s+',' ',str(value).lower()).strip()
    year=re.search(r'\b(\d{4})\b',text)
    words=re.findall(r'[a-zа-яё]+',text)
    if not year:return None
    en=('jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec')
    for word in words:
        prefix=word[:3]
        if prefix=='май':prefix='мая'
        if prefix in _RU_MONTHS:return int(year.group(1)),_RU_MONTHS.index(prefix)+1
        if prefix in en:return int(year.group(1)),en.index(prefix)+1
    return None


_CALENDAR_MONTHS = r"""(()=>{
 const visible=e=>!!e.getClientRects().length;
 const labels=[...document.querySelectorAll('ytcp-date-picker .calendar-month-label')].filter(visible);
 return labels.map(e=>e.innerText.trim());
})()"""
# The label's month container must contain one unique enabled day. Do not guess
# by number alone: the calendar can contain several months at once.
def calendar_day_click_script(label, day):
    return r"""(()=>{
      const visible=e=>!!e.getClientRects().length;
      const label=__LABEL__,day=__DAY__;
      const labels=[...document.querySelectorAll('ytcp-date-picker .calendar-month-label')]
        .filter(e=>visible(e)&&e.innerText.trim()===label);
      if(labels.length!==1)return 'ambiguous-month';
      let block=labels[0].closest('.calendar-month')||labels[0].parentElement;
      // Some builds wrap the month heading in an additional div.
      for(let i=0;i<3&&block&&!block.querySelector('.calendar-day');i++)block=block.parentElement;
      if(!block||block.querySelectorAll('.calendar-month-label').length!==1)return 'ambiguous-month';
      const days=[...block.querySelectorAll('.calendar-day')].filter(e=>
        visible(e)&&(e.innerText||e.textContent||'').trim()===String(day)&&
        !e.disabled&&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'&&
        !e.classList.contains('disabled')&&!e.classList.contains('invisible')&&
        !e.closest('[disabled],[aria-disabled="true"],.disabled,.invisible'));
      if(days.length!==1)return days.length?'ambiguous-day':'missing-day';
      days[0].scrollIntoView({block:'center'});days[0].click();return 'clicked';
    })()""".replace('__LABEL__',json.dumps(label,ensure_ascii=False)).replace('__DAY__',str(day))


_CALENDAR_SCROLL = r"""(()=>{
  const all=[...document.querySelectorAll('ytcp-date-picker #calendar-main')].filter(e=>e.getClientRects().length);
  if(all.length!==1)return false;
  all[0].scrollBy(0,__DELTA__);return true;
})()"""


def set_schedule_date(tab, when):
    state=tab.js(_DATE_STATE) or {}
    if state.get('ambiguous'):raise StudioError('Неоднозначный выбор даты в YouTube Studio')
    if parse_date(state.get('date'))==when.date() and not state.get('invalid'):return
    if not state.get('open'):
        click(tab,'#datepicker-trigger',message='Не удалось открыть календарь публикации')
        wait(tab,"[...document.querySelectorAll('ytcp-date-picker .calendar-month-label')].some(e=>e.getClientRects().length)",15,'Не открылся календарь публикации')
    target=(when.year,when.month)
    for _ in range(80):
        labels=tab.js(_CALENDAR_MONTHS) or []
        matched=[label for label in labels if parse_calendar_month(label)==target]
        if len(matched)>1:raise StudioError('Найдено несколько одинаковых месяцев в календаре')
        if matched:
            result=tab.js(calendar_day_click_script(matched[0],when.day))
            if result!='clicked':raise StudioError('Не удалось однозначно выбрать день в календаре ('+str(result)+')')
            time.sleep(1)
            state=tab.js(_DATE_STATE) or {}
            # If the picker remains open, close it after choosing a day; then
            # require Studio to show the committed date on the scheduler.
            if state.get('open'):
                tab.key('Escape','Escape');time.sleep(.5)
                state=tab.js(_DATE_STATE) or {}
            if not state.get('ambiguous') and not state.get('invalid') and parse_date(state.get('date'))==when.date():return
            raise StudioError('YouTube не подтвердил дату после выбора дня в календаре')
        months=[x for x in (parse_calendar_month(label) for label in labels) if x]
        if not months:raise StudioError('Не удалось прочитать месяц в календаре YouTube')
        if min(months)<=target<=max(months):raise StudioError('Нужный месяц не найден в календаре')
        direction=-350 if target<min(months) else 350
        if not tab.js(_CALENDAR_SCROLL.replace('__DELTA__',str(direction))):
            raise StudioError('Не удалось прокрутить календарь YouTube')
        time.sleep(.25)
    raise StudioError('Нужный месяц не найден в календаре YouTube')


def set_schedule(tab, timestamp):
    open_schedule(tab)
    tz=wait(tab,_SCHEDULE_ZONE,30,'Не удалось прочитать расписание YouTube')
    when=local_schedule_timestamp(timestamp,tz)
    # Set the future time first: today's date may be rejected while the default
    # time is midnight in the past. Then select and verify the date.
    time_sel='ytcp-visibility-scheduler #time-of-day-container input'
    wait(tab,"!!document.querySelector('ytcp-visibility-scheduler #time-of-day-container input')",15,'Не найден выбор времени')
    tab.fill(time_sel,when.strftime('%H:%M'));tab.key('Enter','Enter');time.sleep(0.5)
    # Studio commits the typed time only when the field loses focus; without blur the
    # input shows the new time but the video is scheduled at the default 00:00.
    tab.js("document.activeElement&&document.activeElement.blur()");time.sleep(1)
    set_schedule_date(tab,when)
    tab.js("document.activeElement&&document.activeElement.blur()");time.sleep(0.5)
    state=tab.js(_DATE_STATE) or {}
    value=tab.js("(()=>{const t=document.querySelector('ytcp-visibility-scheduler #time-of-day-container input');return t&&t.value})()")
    if state.get('ambiguous') or state.get('invalid') or parse_date(state.get('date')) != when.date() or parse_time(value) != when.time():
        raise StudioError('YouTube не подтвердил выбранные дату и время; проверьте черновик')
    tz2=tab.js(_SCHEDULE_ZONE)
    if local_schedule_timestamp(timestamp,tz2) != when:
        raise StudioError('Часовой пояс или время изменились; проверьте черновик')
    return when


# Recognise only the specific "checks still running" prompt the user approved.
# Never approve copyright claims, strikes, age restrictions, or other warnings.
_RECEIPT_STATE = r"""(()=>{
  const share=document.querySelector('ytcp-video-share-dialog');
  const links=share&&share.getClientRects().length ? [...share.querySelectorAll('a[href]')].map(a=>a.href) : null;
  const visible=e=>!!e.getClientRects().length;
  const dialogs=[...document.querySelectorAll('ytcp-dialog,ytcp-confirm-dialog,tp-yt-paper-dialog,[role="dialog"]')];
  const warning=dialogs.some(e=>visible(e)&&!e.matches('ytcp-uploads-dialog')&&
    /(?:проверка видео ещ[её] продолжается|проверки видео ещ[её] продолжаются|your video is still being checked|video.{0,30}still.{0,20}(?:being checked|checking)|checks?.{0,25}still.{0,20}(?:running|in progress))/i.test(e.innerText||''));
  // Studio's own "upload continues" dialog after Publish/Schedule. Scheduled videos use a
  // different text ("... будет загружено и обработано, мы установим ... доступ. Это произойдет <дата>"),
  // so recognise the dialog element itself as well as both texts.
  const still=[...document.querySelectorAll('ytcp-uploads-still-processing-dialog')].some(visible);
  const processing=still||dialogs.some(e=>visible(e)&&!e.matches('ytcp-uploads-dialog')&&(
    (/(?:обработка видео|video processing)/i.test(e.innerText||'')&&
     /(?:после того как завершится обработка видео|будет опубликовано|after.{0,100}processing|will be published)/i.test(e.innerText||''))||
    /(?:будет загружено и обработано|мы установим для него|after.{0,40}(?:uploaded|upload).{0,40}processed|will be (?:made )?(?:public|scheduled))/i.test(e.innerText||'')));
  return {links,warning,processing};
})()"""

_CONFIRM_CHECKS_WARNING = r"""(()=>{
  const visible=e=>!!e.getClientRects().length;
  const pattern=/(?:проверка видео ещ[её] продолжается|проверки видео ещ[её] продолжаются|your video is still being checked|video.{0,30}still.{0,20}(?:being checked|checking)|checks?.{0,25}still.{0,20}(?:running|in progress))/i;
  const dialogs=[...document.querySelectorAll('ytcp-dialog,ytcp-confirm-dialog,tp-yt-paper-dialog,[role="dialog"]')].filter(e=>visible(e)&&!e.matches('ytcp-uploads-dialog')&&pattern.test(e.innerText||''));
  if(!dialogs.length)return 'none';
  const candidates=[...new Set(dialogs.flatMap(d=>[...d.querySelectorAll('button,ytcp-button,tp-yt-paper-button,[role="button"]')]))]
    .filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&/^(?:publish|опубликовать|[oо][kк])$/i.test((e.innerText||'').trim()));
  // Prefer the actual innermost clickable element; never click if ambiguous.
  const leaves=candidates.filter(e=>!candidates.some(other=>other!==e&&e.contains(other)));
  if(leaves.length!==1)return 'ambiguous';
  leaves[0].click();return 'clicked';
})()"""

# Same button as _CONFIRM_CHECKS_WARNING, but return its centre for a trusted CDP mouse click.
_CHECKS_WARNING_TARGET = _CONFIRM_CHECKS_WARNING.replace(
    "leaves[0].click();return 'clicked';",
    "leaves[0].scrollIntoView({block:'center'});const r=leaves[0].getBoundingClientRect();"
    "if(!r.width||!r.height)return 'ambiguous';return {x:r.left+r.width/2,y:r.top+r.height/2};")
_CHECKS_WARNING_ATTEMPTS=5
_CHECKS_WARNING_RETRY=3


def confirm_checks_warning(tab, attempt=1):
    """Press «Опубликовать» in Studio's "checks are still running" prompt.

    Studio can ignore a synthetic DOM click while the prompt is still animating in, so odd attempts
    send a real mouse click through CDP and even attempts fall back to a DOM click. Returns
    'clicked', 'none' (prompt already gone) or 'ambiguous' (never clicks then).
    """
    if attempt%2==1 and hasattr(tab,'call'):
        target=tab.js(_CHECKS_WARNING_TARGET)
        if not isinstance(target,dict):return target
        x,y=float(target['x']),float(target['y'])
        tab.call('Input.dispatchMouseEvent',type='mouseMoved',x=x,y=y)
        tab.call('Input.dispatchMouseEvent',type='mousePressed',x=x,y=y,button='left',clickCount=1)
        tab.call('Input.dispatchMouseEvent',type='mouseReleased',x=x,y=y,button='left',clickCount=1)
        return 'clicked'
    return tab.js(_CONFIRM_CHECKS_WARNING)


def upload_video_id(tab):
    """Read only the video's link from the current upload dialog; never guess an ID."""
    links=tab.js("""(()=>{const d=document.querySelector('ytcp-uploads-dialog');
      return d?[...d.querySelectorAll('a[href]')].map(a=>a.href):[]})()""") or []
    ids={video_from_url(x) for x in links};ids.discard(None)
    if len(ids)>1:raise StudioError('В окне загрузки несколько ссылок на видео; проверьте Studio')
    return next(iter(ids),None)


def monitor_status(tab,channel_id,video_id,scheduled):
    """Require the exact video's Studio content row and a positive visibility state."""
    script=r"""(()=>{
      const id=__ID__;
      const anchors=[...document.querySelectorAll('a[href]')].filter(a=>{
        try {const u=new URL(a.href);return u.hostname==='studio.youtube.com'&&
             u.pathname.startsWith('/video/'+id+'/');} catch(_){return false;}
      });
      const rows=[...new Set(anchors.map(a=>a.closest('ytcp-video-row,[role="row"],tr')).filter(Boolean))];
      if(rows.length!==1)return {url:location.href,state:'unknown'};
      const row=rows[0];const text=(row.innerText||'').replace(/\s+/g,' ').trim();
      const pending=/(?:uploading|upload(?:ed)?\s*:\s*\d+\s*%|processing|process(?:ing)?\s+in\s+(?:sd|hd)|загружено\s*:\s*\d+\s*%|загрузка видео|обрабатывается|обработка\s+(?:в|видео|sd|hd))/i.test(text);
      if(pending)return {url:location.href,state:'processing'};
      const lines=(row.innerText||'').split(/\n/).map(x=>x.trim().toLowerCase());
      const scheduled=__SCHEDULED__;
      // Russian Studio shows scheduled videos as «Отложено» / «Отложенная публикация».
      const good=scheduled ? lines.some(x=>/^(?:scheduled|запланирован|отложен)/.test(x))
        : lines.some(x=>/^(?:public|для всех|открытый доступ)$/.test(x)) && lines.some(x=>/^(?:published|опубликовано)$/.test(x));
      return {url:location.href,state:good?'ready':'unknown'};
    })()""".replace('__ID__',json.dumps(video_id)).replace('__SCHEDULED__','true' if scheduled else 'false')
    result=(tab.read_js(script) if hasattr(tab,'read_js') else tab.js(script)) or {}
    if channel_from_url(result.get('url',''))!=channel_id:
        return 'unknown'
    return result.get('state','unknown')


_FIND_BY_TITLE = r"""(()=>{
  const want=__TITLE__.replace(/\s+/g,' ').trim().toLowerCase();
  const ids=[];
  for(const row of document.querySelectorAll('ytcp-video-row')){
    const t=row.querySelector('#video-title');
    const title=((t&&(t.textContent||''))||'').replace(/\s+/g,' ').trim().toLowerCase();
    if(title!==want)continue;
    for(const a of row.querySelectorAll('a[href]')){
      const m=/\/video\/([A-Za-z0-9_-]{11})\//.exec(a.href||'');
      if(m&&!ids.includes(m[1]))ids.push(m[1]);
    }
  }
  return {url:location.href,ids};
})()"""


def find_video_by_title(browser, channel_id, title, progress=None, attempts=6, section='upload'):
    """Studio sometimes publishes without showing the confirmation dialog: look the video up in the
    channel's content list by its exact title. Returns the ID only when exactly one video matches."""
    previous=getattr(browser,'monitor_tab',None)
    monitor=browser.open_monitor(channel_id) if section=='upload' else browser.open_monitor(channel_id,section)
    try:
        script=_FIND_BY_TITLE.replace('__TITLE__',json.dumps(str(title)))
        for attempt in range(attempts):
            time.sleep(4)
            result=monitor.read_js(script) or {}
            if channel_from_url(result.get('url',''))==channel_id:
                ids=result.get('ids') or []
                if len(ids)==1:
                    if progress:progress('Видео найдено в списке Studio по названию: '+ids[0])
                    return ids[0]
                if len(ids)>1:
                    if progress:progress('В Studio несколько видео с таким названием; не угадываю')
                    return None
            monitor.reload_monitor()
        return None
    finally:
        try:monitor.call('Page.close')
        except Exception:pass
        try:monitor.close()
        except Exception:pass
        browser.monitor_tab=previous


def wait_for_receipt(tab, progress=None, timeout=7200, *, browser=None, channel_id=None, candidate_id=None, scheduled=False, section='upload'):
    """Confirm publication prompt once, and wait for SD processing and visibility."""
    deadline=time.monotonic()+timeout; attempts=0; next_try=0.0
    next_report=time.monotonic()+60; refresh_at=time.monotonic()+60
    monitor=None; video_id=candidate_id; monitor_errors=0
    while time.monotonic()<deadline:
        state=(tab.read_js(_RECEIPT_STATE) if hasattr(tab,'read_js') else tab.js(_RECEIPT_STATE)) or {}
        if state.get('links') is not None:
            ids={video_from_url(x) for x in state['links']};ids.discard(None)
            if len(ids)>1:raise StudioError('В Studio несколько ссылок на видео; проверьте загрузку')
            if ids:
                got=ids.pop()
                if video_id and video_id!=got:raise StudioError('ID подтверждения не совпал с загружаемым видео')
                video_id=got
        # Re-press only while the prompt is still open: once it closes nothing is clicked again.
        if state.get('warning') and state.get('links') is None and attempts<_CHECKS_WARNING_ATTEMPTS and time.monotonic()>=next_try:
            attempts+=1;next_try=time.monotonic()+_CHECKS_WARNING_RETRY
            result=confirm_checks_warning(tab,attempts)
            if result=='ambiguous':raise StudioError('Не удалось однозначно подтвердить предупреждение о проверке; проверьте Studio')
            if result not in ('clicked','none'):raise StudioError('Не удалось подтвердить публикацию; проверьте Studio')
            if result=='clicked' and attempts==1 and progress:progress('Подтверждено предупреждение о продолжающейся проверке; ожидаю обработку')
        # Production requires a positive content-list status for the exact video.
        if video_id and browser is not None:
            if monitor is None:
                monitor=browser.open_monitor(channel_id) if section=='upload' else browser.open_monitor(channel_id,section)
                if progress:progress('Ожидаю завершения загрузки и обработки SD в YouTube Studio')
            try:
                current=monitor_status(monitor,channel_id,video_id,scheduled)
                monitor_errors=0
            except (StudioError,OSError):
                # Read-only polling may race a page reload; never replay publication.
                monitor_errors+=1
                if monitor_errors>=10:raise StudioError('Страница проверки Studio недоступна; проверьте видео вручную')
                current='unknown'
            if current=='ready':
                if progress:progress('YouTube показал обработанное видео с нужным доступом')
                return video_id
            if time.monotonic()>=refresh_at:
                if hasattr(monitor,'reload_monitor'):monitor.reload_monitor()
                else:monitor.call('Page.reload',ignoreCache=True)
                refresh_at=time.monotonic()+60
        elif video_id and browser is None:
            # Unit test / backwards-compatible utility; real uploads always pass browser.
            return video_id
        if progress and time.monotonic()>=next_report:
            progress('Ожидаю загрузку, обработку и подтверждение Studio; не закрывайте браузер')
            next_report=time.monotonic()+60
        time.sleep(2)
    raise StudioError('Не подтверждены загрузка, обработка и доступ; браузер оставлен открытым, проверьте Studio перед повтором')


def wait_for_submission(tab, candidate_id=None, progress=None, timeout=300):
    """Require Studio's post-publication dialog and exact video ID, not SD completion."""
    deadline=time.monotonic()+timeout;attempts=0;next_try=0.0;video_id=candidate_id
    while time.monotonic()<deadline:
        state=tab.js(_RECEIPT_STATE) or {}
        # The share dialog is Studio's receipt. Studio can leave the checks prompt rendered on top of it
        # after a successful publish, so read the receipt first and never press the prompt again then.
        if state.get('links') is not None:
            ids={video_from_url(x) for x in state['links']};ids.discard(None)
            if len(ids)>1:raise StudioError('В Studio несколько ссылок на видео')
            if ids:
                found=ids.pop()
                if video_id and video_id!=found:raise StudioError('ID видео в подтверждении не совпал')
                video_id=found
            if video_id:return video_id
        if state.get('warning'):
            # Re-press only while the prompt is still open: once it closes nothing is clicked again.
            if attempts<_CHECKS_WARNING_ATTEMPTS and time.monotonic()>=next_try:
                attempts+=1;next_try=time.monotonic()+_CHECKS_WARNING_RETRY
                result=confirm_checks_warning(tab,attempts)
                if result not in ('clicked','none'):raise StudioError('Не удалось однозначно подтвердить предупреждение; проверьте Studio')
                if result=='clicked' and attempts==1 and progress:progress('Подтверждено предупреждение о продолжающейся проверке')
            time.sleep(1);continue
        if state.get('processing') and video_id:return video_id
        time.sleep(1)
    raise StudioError('Studio не подтвердила отправку; проверьте видео перед повтором')


def validate_publication_plan(row):
    # Older test jobs may have stored public access for every clip. Never
    # silently publish an old second or later clip without a schedule.
    if row.get('clip_index',1)>1 and row.get('publish_at') is None:
        raise StudioError('У старого задания нет расписания для этой нарезки; создайте новое задание')


class YouTubeStudio:
    def __init__(self,root): self.root=Path(root)
    def connect(self,profile,ready_event,opened_event=None,timeout=600):
        with profile_lock(profile):
            with Browser(self.root,profile) as tab:
                if opened_event is not None: opened_event.set()
                if not ready_event.wait(timeout):
                    raise StudioError('Время входа истекло; повторите подключение')
                return channel_ready(tab,timeout=30)
    def submit(self,tab,browser,row,progress=None):
        validate_publication_plan(row)
        expected=row['channel_id']
        try:prepare_upload_dialog(tab,expected,progress=progress)
        except UnsafeUploadState:
            browser.keep_open=True;browser.attempt_may_upload=True
            raise
        channel_ready(tab,expected,timeout=5)
        # Selecting the file creates a draft; queue must already say uploading.
        browser.keep_open=True  # a draft/upload may be created even if the next action fails
        browser.attempt_may_upload=True
        attach_file(tab,row['file_path'])
        if progress:progress('Файл передан; ожидаю загрузку и настройки')
        title_selector=wait_for_title(tab,progress=progress)
        tab.fill(title_selector,row['title'])
        if row.get('description'):
            fill_description(tab,row['description'],progress=progress)
            ensure_title(tab,title_selector,row['title'],progress=progress)
        thumb=find_thumbnail(row['file_path'])
        if thumb:attach_thumbnail(tab,thumb,progress=progress)
        audience={'kids':'VIDEO_MADE_FOR_KIDS_MFK','not_kids':'VIDEO_MADE_FOR_KIDS_NOT_MFK'}.get(row['audience'])
        if not audience: raise StudioError('Аудитория не подтверждена')
        radio='tp-yt-paper-radio-button[name="'+audience+'"]'
        click(tab,radio)
        if not selected(tab,radio):raise StudioError('YouTube не подтвердил выбор аудитории')
        # The number of steps differs between channels (e.g. monetisation): press «Next» until
        # the visibility step is actually shown, and poll instead of sleeping between clicks.
        visibility="[...document.querySelectorAll('ytcp-uploads-dialog tp-yt-paper-radio-button[name=PUBLIC],ytcp-uploads-dialog #schedule-radio-button')].some(e=>e.getClientRects().length>0)"
        for _ in range(8):
            if tab.js(visibility):break
            if tab.js(_LIMIT_TEXT):
                raise UploadLimitReached('YouTube: исчерпан дневной лимит загрузок канала («Загрузка недоступна… через 24 часа»). '
                                         'Удалите черновик этой нарезки в Studio; остальные нарезки остаются в очереди. '
                                         'Лимит снимается через 24 часа или после расширенной проверки канала')
            deadline=time.monotonic()+600
            while not tab.js("[...document.querySelectorAll('ytcp-uploads-dialog #next-button button')].some(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true')"):
                if tab.js(_LIMIT_TEXT):
                    raise UploadLimitReached('YouTube: исчерпан дневной лимит загрузок канала («Загрузка недоступна… через 24 часа»). '
                                             'Удалите черновик этой нарезки в Studio; остальные нарезки остаются в очереди. '
                                             'Лимит снимается через 24 часа или после расширенной проверки канала')
                if time.monotonic()>deadline:raise StudioError('Кнопка «Далее» в Studio не стала доступной')
                time.sleep(.5)
            click(tab,'ytcp-uploads-dialog #next-button button',timeout=30)
            for _ in range(20):
                time.sleep(.3)
                if tab.js(visibility):break
        wait(tab,visibility,30,'Не открылись настройки доступа')
        if row['publish_at'] is None:
            radio='tp-yt-paper-radio-button[name="PUBLIC"]'
            click(tab,radio)
            if not selected(tab,radio):raise StudioError('YouTube не подтвердил публичный доступ')
        else:
            if progress:progress('Настраиваю отложенную публикацию: открываю расписание, проверяю дату, время и часовой пояс')
            set_schedule(tab,row['publish_at'])
            if progress:progress('Дата и время расписания заполнены; отправляю в Studio')
        channel_ready(tab,expected,timeout=5)
        candidate=upload_video_id(tab)
        # The enabled button can precede upload/check completion.
        click(tab,'ytcp-uploads-dialog #done-button button',timeout=1800)
        if progress:progress('Запрос публикации отправлен; ожидаю подтверждение Studio')
        try:
            return wait_for_submission(tab,candidate_id=candidate,progress=progress,timeout=90)
        except StudioError:
            # The video already exists in Studio (its link was in the upload dialog): treat it as
            # submitted and let the processing monitor confirm it, instead of asking the user.
            if candidate:
                if progress:progress('Studio не показала подтверждение, но видео создано ('+candidate+'); продолжаю')
                return candidate
            try:found=find_video_by_title(browser,expected,row['title'],progress=progress,section=row.get('section') or 'upload')
            except Exception:found=None
            if found:return found
            raise

    def upload_batch(self, queue, first, profile, channel_id, stop_event, progress, on_confirm):
        """Submit all ready clips in separate tabs, then wait for every exact ID."""
        # Пакеты разных каналов идут параллельно; замок канала берётся только на время работы окна загрузки.
        with batch_guard(profile):
            browser=Browser(self.root,profile)
            # Already submitted videos must never be interrupted by an error.
            browser.keep_open=bool(queue.submitted_uploads(profile))
            with browser as tab:
                current=first;used=False;monitor=None;last_report=0;last_refresh=time.monotonic()+30;monitor_errors=0;failure=None;failure_may_upload=False;attempts={};consecutive_failures=0;failure_reported=False
                while not stop_event.is_set():
                    if current is not None:
                        progress(current['id'],'загрузка на канал '+channel_id)
                        browser.attempt_may_upload=False
                        try:
                            with profile_lock(profile):
                                upload_tab=tab if not used else browser.open_upload_tab(channel_id)
                                used=True
                                video_id=self.submit(upload_tab,browser,current,progress=lambda msg:progress(current['id'],msg))
                            if not any(r['id']==current['id'] for r in queue.list_uploads()):
                                progress(current['id'],'запись удалена из очереди во время загрузки; видео '+video_id+' осталось в Studio')
                                current=None;continue
                            queue.mark_submitted(current['id'],channel_id,video_id)
                        except UploadLimitReached as exc:
                            # Studio kept a draft that it will not upload: return the clip, stop the batch.
                            queue.requeue_after_limit(current['id'],str(exc)[:300])
                            progress(current['id'],str(exc))
                            browser.keep_open=True
                            raise
                        except VerificationRequired as exc:
                            # Nothing was created in Studio: return the clip to the queue and stop.
                            queue.requeue(current['id'])
                            progress(current['id'],str(exc))
                            browser.keep_open=True
                            raise
                        except Exception as exc:
                            detail=str(exc)[:400] if isinstance(exc,StudioError) else 'Ошибка связи с браузером'
                            failure=type(exc).__name__+': '+detail
                            # Only failures known to precede file selection can be retried.
                            attempts[current['id']]=attempts.get(current['id'],0)+1
                            if not browser.attempt_may_upload and attempts[current['id']]<2:
                                progress(current['id'],'ошибка до выбора файла; повторяю автоматически один раз')
                                stop_event.wait(2)
                                continue
                            queue.mark_uncertain(current['id'],failure)
                            failure_may_upload=failure_may_upload or browser.attempt_may_upload
                            consecutive_failures+=1
                            progress(current['id'],'не подтверждено: '+failure+'; повтор этой нарезки не выполняется во избежание дубля')
                        else:
                            consecutive_failures=0
                            progress(current['id'],'отправлено в Studio; обработка продолжится параллельно')
                        current=None
                    # A single uncertain clip does not prevent later clips from starting.
                    # Multiple consecutive errors indicate the browser is unusable.
                    current=queue.claim(finished_only=True,profile=profile) if consecutive_failures<2 and not _video_limit(profile) else None
                    if current is not None:continue
                    pending=queue.submitted_uploads(profile)
                    if not pending:
                        if consecutive_failures>=2:
                            browser.keep_open=failure_may_upload
                            raise StudioError('Несколько ошибок подряд; загрузчик остановлен: '+failure)
                        if failure_may_upload:
                            # Unknown transfer must not be aborted or re-submitted.
                            # Keep the session available for any later clips.
                            if not failure_reported:
                                progress(0,'Статус одной загрузки неизвестен; Chrome остаётся открыт, новые нарезки продолжат загружаться')
                                failure_reported=True
                            browser.keep_open=True
                            stop_event.wait(5)
                            continue
                        browser.keep_open=False
                        return
                    if monitor is None:
                        channel_ready(tab,channel_id)
                        monitor=browser.open_monitor(channel_id)
                        progress(pending[0]['id'],f'Все доступные ролики отправлены; ожидаю обработку: {len(pending)}. Не закрывайте Chrome')
                    try:
                        for row in pending:
                            if monitor_status(monitor,channel_id,row['video_id'],row['publish_at'] is not None)=='ready':
                                queue.confirm(row['id'],channel_id,row['video_id'])
                                on_confirm(row['id'],row['video_id'])
                        if time.monotonic()>=last_refresh:
                            if hasattr(monitor,'reload_monitor'):monitor.reload_monitor()
                            else:monitor.call('Page.reload',ignoreCache=True)
                            last_refresh=time.monotonic()+30
                        monitor_errors=0
                    except Exception as exc:
                        monitor_errors+=1
                        # Failure to read one processing page must not prevent
                        # other newly rendered clips from being submitted.
                        if monitor_errors==1 or monitor_errors % 12==0:
                            progress(pending[0]['id'],'Не удалось прочитать статус обработки ('+type(exc).__name__+'); продолжу публикацию готовых нарезок и повторю проверку')
                    if time.monotonic()-last_report>=60:
                        left=len(queue.submitted_uploads(profile))
                        if left:progress(pending[0]['id'],f'Ожидаю загрузку и обработку {left} видео; не закрывайте Chrome')
                        last_report=time.monotonic()
                    stop_event.wait(5)
                # Leave the browser open; submitted video IDs survive a restart.
                browser.keep_open=True

    def upload(self,row,progress=None):
        validate_publication_plan(row)
        with profile_lock(row['profile']):
            browser=Browser(self.root,row['profile'])
            with browser as tab:
                expected=row['channel_id']; channel_ready(tab,expected)
                open_upload_menu(tab)
                wait(tab,"!!document.querySelector('ytcp-uploads-dialog input[type=file]')",30,'Не найден выбор видео')
                channel_ready(tab,expected,timeout=5)
                # Selecting the file creates a draft; queue must already say uploading.
                browser.keep_open=True  # a draft/upload may be created even if the next action fails
                attach_file(tab,row['file_path'])
                if progress:progress('Файл передан; ожидаю загрузку и настройки')
                title_selector=wait_for_title(tab,progress=progress)
                tab.fill(title_selector,row['title'])
                audience={'kids':'VIDEO_MADE_FOR_KIDS_MFK','not_kids':'VIDEO_MADE_FOR_KIDS_NOT_MFK'}.get(row['audience'])
                if not audience: raise StudioError('Аудитория не подтверждена')
                radio='tp-yt-paper-radio-button[name="'+audience+'"]'
                click(tab,radio)
                if not selected(tab,radio):raise StudioError('YouTube не подтвердил выбор аудитории')
                for _ in range(3):
                    click(tab,'ytcp-uploads-dialog #next-button button',timeout=600)
                    time.sleep(2)
                wait(tab,"!!(document.querySelector('tp-yt-paper-radio-button[name=PUBLIC]')||document.querySelector('#schedule-radio-button'))",30,'Не открылись настройки доступа')
                if row['publish_at'] is None:
                    radio='tp-yt-paper-radio-button[name="PUBLIC"]'
                    click(tab,radio)
                    if not selected(tab,radio):raise StudioError('YouTube не подтвердил публичный доступ')
                else:
                    set_schedule(tab,row['publish_at'])
                channel_ready(tab,expected,timeout=5)
                candidate=upload_video_id(tab)
                # The enabled button can precede upload/check completion.
                click(tab,'ytcp-uploads-dialog #done-button button',timeout=1800)
                if progress:progress('Запрос публикации отправлен; ожидаю подтверждение Studio')
                try:
                    video_id=wait_for_receipt(tab,progress=progress,browser=browser,channel_id=expected,
                                              candidate_id=candidate,scheduled=row['publish_at'] is not None)
                except Exception:
                    if progress:progress('Браузер оставлен открыт: проверьте загрузку и Studio перед повтором')
                    raise
                browser.keep_open=False
                return video_id
