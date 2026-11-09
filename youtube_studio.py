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
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from youtube_queue import profile_key

CHANNEL = re.compile(r'^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)')
VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')
_LOCK = threading.Lock()

class StudioError(RuntimeError): pass


def channel_from_url(url):
    parsed = urlparse(str(url))
    match = CHANNEL.match(parsed.path)
    return match.group(1) if parsed.scheme == 'https' and parsed.hostname == 'studio.youtube.com' and match else None


def video_from_url(url):
    from urllib.parse import parse_qs
    p = urlparse(str(url))
    if p.scheme != 'https' or p.hostname not in ('youtu.be','youtube.com','www.youtube.com'):
        return None
    value = p.path.strip('/') if p.hostname == 'youtu.be' else parse_qs(p.query).get('v',[''])[0]
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
        self.ws=websocket.create_connection(websocket_url,timeout=30,suppress_origin=True)
        self.seq=0
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
        self.proc=None; self.tab=None
    def __enter__(self):
        self.profile.mkdir(parents=True,exist_ok=True)
        # Reserve a random local port; Chrome refuses an already used port.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        args=[chrome_path(),f'--user-data-dir={self.profile}',f'--remote-debugging-port={port}',
              '--remote-debugging-address=127.0.0.1','--no-first-run','--no-default-browser-check',
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
    def __exit__(self,*args):
        if self.tab:self.tab.close();self.tab=None
        if self.proc:
            self.proc.terminate()
            try:self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=6)
            self.proc=None


def wait(tab, expression, timeout, message):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=tab.js(expression)
        if value: return value
        time.sleep(.6)
    raise StudioError(message)


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


def click(tab, selector, timeout=30):
    script='''(()=>{const es=[...document.querySelectorAll(%s)].filter(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');if(es.length!==1)return false;es[0].scrollIntoView({block:'center'});es[0].click();return true})()''' % json.dumps(selector)
    # A false return means no click was issued. A CDP error must never be replayed.
    return wait(tab,script,timeout,'Не найден однозначный элемент YouTube Studio')


def selected(tab, selector):
    return tab.js('''(()=>{const es=[...document.querySelectorAll(%s)];return es.length===1&&es[0].getAttribute('aria-checked')==='true'})()''' % json.dumps(selector))


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
        raise StudioError('Не удалось однозначно проверить часовой пояс YouTube Studio')
    sign,h,m=matches[0]; minutes=(int(h)*60+int(m or 0))*(1 if sign=='+' else -1)
    if abs(minutes)>14*60: raise StudioError('Некорректный часовой пояс YouTube')
    intended=datetime.fromtimestamp(timestamp).astimezone()
    if intended.utcoffset() != timedelta(minutes=minutes):
        raise StudioError('Часовой пояс YouTube не совпадает с часовым поясом компьютера')
    # Studio date/time controls have minute precision.
    if intended.second or intended.microsecond:
        intended=(intended+timedelta(minutes=1)).replace(second=0,microsecond=0)
    return intended


def parse_date(value):
    text=re.sub(r'\s+',' ',str(value).strip())
    for fmt in ('%b %d, %Y','%B %d, %Y','%d %b %Y','%m/%d/%Y','%Y-%m-%d'):
        try:return datetime.strptime(text,fmt).date()
        except ValueError:pass
    return None


def parse_time(value):
    text=re.sub(r'\s+',' ',str(value).strip()).upper()
    for fmt in ('%H:%M','%I:%M %p','%I:%M%p'):
        try:return datetime.strptime(text,fmt).time()
        except ValueError:pass
    return None


def set_schedule(tab, timestamp):
    click(tab,'#schedule-radio-button')
    tz=wait(tab,"(()=>{const e=document.querySelector('ytcp-visibility-scheduler');return e&&e.innerText})()",30,'Не удалось прочитать расписание YouTube')
    when=local_schedule_timestamp(timestamp,tz)
    click(tab,'#datepicker-trigger')
    # Picker structure can change; ambiguity is an error, never guess.
    date_sel='ytcp-date-picker input'
    wait(tab,"!!document.querySelector('ytcp-date-picker input')",15,'Не найден выбор даты')
    tab.fill(date_sel,when.strftime('%b %d, %Y'));tab.key('Enter','Enter');time.sleep(1)
    time_sel='ytcp-visibility-scheduler #time-of-day-container input'
    wait(tab,"!!document.querySelector('ytcp-visibility-scheduler #time-of-day-container input')",15,'Не найден выбор времени')
    tab.fill(time_sel,when.strftime('%H:%M'));tab.key('Enter','Enter');time.sleep(1)
    actual=tab.js('''(()=>{const d=document.querySelector('#datepicker-trigger .dropdown-trigger-text');const t=document.querySelector('ytcp-visibility-scheduler #time-of-day-container input');return {date:d&&d.innerText,time:t&&t.value}})()''') or {}
    if parse_date(actual.get('date')) != when.date() or parse_time(actual.get('time')) != when.time():
        raise StudioError('YouTube не подтвердил выбранные дату и время; проверьте черновик')
    tz2=tab.js("document.querySelector('ytcp-visibility-scheduler').innerText")
    if local_schedule_timestamp(timestamp,tz2) != when:
        raise StudioError('Часовой пояс или время изменились; проверьте черновик')
    return when


class YouTubeStudio:
    def __init__(self,root): self.root=Path(root)
    def connect(self,profile,ready_event,opened_event=None,timeout=600):
        with _LOCK:
            with Browser(self.root,profile) as tab:
                if opened_event is not None: opened_event.set()
                if not ready_event.wait(timeout):
                    raise StudioError('Время входа истекло; повторите подключение')
                return channel_ready(tab,timeout=30)
    def upload(self,row):
        with _LOCK:
            with Browser(self.root,row['profile']) as tab:
                expected=row['channel_id']; channel_ready(tab,expected)
                click(tab,'ytcp-header .ytcpAppHeaderCreateIcon')
                click(tab,'tp-yt-paper-item[test-id="upload"]')
                wait(tab,"!!document.querySelector('ytcp-uploads-dialog input[type=file]')",30,'Не найден выбор видео')
                channel_ready(tab,expected,timeout=5)
                # Selecting the file creates a draft; queue must already say uploading.
                attach_file(tab,row['file_path'])
                wait(tab,"!!document.querySelector('ytcp-uploads-dialog #title-textarea #textbox')",90,'Не найдено название видео')
                tab.fill('ytcp-uploads-dialog #title-textarea #textbox',row['title'])
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
                # Require the button to be enabled; wait for upload/checks.
                click(tab,'ytcp-uploads-dialog #done-button button',timeout=1800)
                result=wait(tab,'''(()=>{const d=document.querySelector('ytcp-video-share-dialog');if(!d||!d.getClientRects().length)return null;return [...d.querySelectorAll('a[href]')].map(a=>a.href)})()''',600,'Подтверждение YouTube не получено; проверьте Studio перед повтором')
                ids={video_from_url(x) for x in result};ids.discard(None)
                if len(ids)!=1:raise StudioError('Не удалось проверить идентификатор видео; проверьте Studio')
                return ids.pop()
