"""Массовое редактирование профилей YouTube-каналов через YouTube Studio: общее описание и ссылки сразу
на всех привязанных каналах («Настройка канала → Профиль»).

Правила:
* каждый канал — свой профиль Chrome; каналы обрабатываются по очереди, через общий замок браузера YouTube;
* описание заменяется целиком (если включено), ссылки только добавляются — без дублей, существующие не трогаются;
* после «Опубликовать» страница перезагружается и проверяется, что изменения действительно сохранились.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import youtube_studio as ys

PROFILE_URL = 'https://studio.youtube.com/channel/{}/editing/profile?hl=en'
MAX_LINKS = 14  # больше YouTube не позволяет
DESC = "document.querySelector('#description-textbox #textbox')"
ITEM = "[...document.querySelectorAll('ytcp-channel-link-item')].filter(e=>e.getClientRects().length)[%d].querySelectorAll('input')[%d]"

_STATE_JS = r"""(()=>{const vis=e=>!!e.getClientRects().length;
 const d=document.querySelector('#description-textbox #textbox');
 const box=document.querySelector('ytcp-channel-links');
 const links=[...document.querySelectorAll('ytcp-channel-link-item')].filter(vis).map(it=>{const ins=[...it.querySelectorAll('input')];
   return {title:ins[0]?ins[0].value:'',url:ins[1]?ins[1].value:''}});
 const pub=document.querySelector('#publish-button'), dis=document.querySelector('#discard-changes-button');
 const on=b=>!!b&&vis(b)&&b.getAttribute('aria-disabled')!=='true'&&!b.hasAttribute('disabled');
 const errors=[...document.querySelectorAll('ytcp-form-input-container[invalid],ytcp-form-input-container[is-error]')].filter(vis)
   .map(e=>(e.innerText||'').replace(/\s+/g,' ').trim()).filter(Boolean).slice(0,3);
 return {url:location.href,ready:!!d&&!!box,desc:d?d.innerText:null,links,errors,
   canPublish:on(pub),canDiscard:on(dis)};})()"""

# Studio's own confirmation toast after «Опубликовать».
_TOAST_JS = r"""(()=>[...document.querySelectorAll('ytcp-toast,tp-yt-paper-toast,#toast,[role=alert],[role=status]')]
 .filter(e=>e.getClientRects().length).map(e=>(e.innerText||'').replace(/\s+/g,' ').trim()).filter(Boolean).join(' | '))()"""
_SAVED = re.compile(r'опубликован|сохранен|изменения применены|published|saved', re.I)

_ADD_LINK_JS = r"""(()=>{const box=document.querySelector('ytcp-channel-links')||document;
 const b=[...box.querySelectorAll('button')].filter(e=>e.getClientRects().length&&/^(добавить ссылку|add link)$/i.test((e.innerText||'').trim()));
 if(b.length!==1)return b.length;b[0].scrollIntoView({block:'center'});b[0].click();return 'clicked';})()"""

_CLICK_JS = r"""((id)=>{const h=document.querySelector('#'+id);if(!h||!h.getClientRects().length)return false;
 const b=h.querySelector('button')||h;b.click();return true;})(%s)"""


def norm_url(u):
    u = str(u or '').strip()
    u = re.sub(r'^(https?://)?(www\.)?', '', u, flags=re.I).rstrip('/')
    return u.lower()


def norm_text(t):
    return '\n'.join(re.sub(r'[ \t\u00a0]+', ' ', line).strip() for line in str(t or '').replace('\r', '').split('\n')).strip()


def _wait_state(tab, channel_id, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            st = tab.js(_STATE_JS) or {}
        except ys.StudioError:
            st = {}
        if st.get('ready') and ys.channel_from_url(st.get('url', '')) == channel_id:
            return st
        time.sleep(0.7)
    raise ys.StudioError('Не открылась страница «Настройка канала → Профиль» в Studio')


def _type(tab, locate, text, multiline=False):
    """Выделяет всё в поле и печатает текст как пользователь (Studio видит настоящие события ввода)."""
    ok = tab.js("(()=>{const e=%s;if(!e)return false;e.scrollIntoView({block:'center'});e.focus();"
                "if(e.select){e.select()}else{const r=document.createRange();r.selectNodeContents(e);"
                "const s=getSelection();s.removeAllRanges();s.addRange(r)}return true})()" % locate)
    if not ok:
        raise ys.StudioError('Не найдено поле на странице профиля канала')
    tab.call('Input.dispatchKeyEvent', type='keyDown', key='Delete', code='Delete', windowsVirtualKeyCode=46)
    tab.call('Input.dispatchKeyEvent', type='keyUp', key='Delete', code='Delete', windowsVirtualKeyCode=46)
    lines = text.split('\n') if multiline else [text.replace('\n', ' ')]
    for i, line in enumerate(lines):
        if line:
            tab.call('Input.insertText', text=line)
        if i < len(lines) - 1:
            tab.call('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13, text='\r')
            tab.call('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13)
    time.sleep(0.3)


_DEL_LINK_JS = r"""((i)=>{const vis=e=>!!e.getClientRects().length;
 const it=[...document.querySelectorAll('ytcp-channel-link-item')].filter(vis)[i];if(!it)return 'no item';
 const b=[...it.querySelectorAll('ytcp-icon-button')].filter(e=>vis(e)&&/^(удалить|delete|remove)/i.test((e.getAttribute('aria-label')||'').trim()));
 if(b.length!==1)return 'buttons '+b.length;b[0].click();return 'clicked'})(%d)"""


def _matches(item, remove):
    """Ссылка канала подходит под удаление: по адресу, а если адрес в списке не указан — по названию."""
    for x in remove or []:
        url, title = str(x.get('url') or '').strip(), str(x.get('title') or '').strip()
        if url and item.get('url') and norm_url(item['url']) == norm_url(url):
            return True
        if not url and title and norm_text(item.get('title')).lower() == norm_text(title).lower():
            return True
    return False


def fill_channel(tab, channel_id, description, links, progress=lambda m: None, remove=()):
    """Открывает профиль канала и вносит изменения, НЕ публикуя. Возвращает список изменений."""
    ys.channel_ready(tab, channel_id)
    try:
        tab.call('Page.bringToFront')
    except Exception:  # noqa: BLE001
        pass
    progress('открываю «Настройка канала → Профиль»')
    tab.call('Page.navigate', url=PROFILE_URL.format(channel_id))
    time.sleep(1.5)
    st = _wait_state(tab, channel_id)
    changed = []
    if description is not None and norm_text(st['desc']) != norm_text(description):
        progress('меняю описание')
        _type(tab, DESC, description, multiline=True)
        changed.append('описание')
    for i in reversed(range(len(st['links']))):
        item = st['links'][i]
        if not _matches(item, remove):
            continue
        name = item.get('title') or item.get('url')
        progress('удаляю ссылку «%s»' % name)
        r = tab.js(_DEL_LINK_JS % i)
        if r != 'clicked':
            raise ys.StudioError('Не найдена кнопка «Удалить» у ссылки «%s» (%s)' % (name, r))
        ys.wait(tab, "[...document.querySelectorAll('ytcp-channel-link-item')].filter(e=>e.getClientRects().length).length===%d" % (len(st['links']) - 1),
                10, 'Studio не удалила ссылку «%s»' % name)
        st['links'].pop(i)
        changed.append('удалена ссылка «%s»' % name)
    links = [x for x in links if not _matches(x, remove)]
    have = {norm_url(x['url']) for x in st['links'] if x['url']}
    todo, seen = [], set()
    for link in links:
        key = norm_url(link['url'])
        if key and key not in have and key not in seen:
            todo.append(link)
            seen.add(key)
    if len(st['links']) + len(todo) > MAX_LINKS:
        raise ys.StudioError(f"У канала уже {len(st['links'])} ссылок, а YouTube позволяет не больше {MAX_LINKS}")
    for link in todo:
        n = len((tab.js(_STATE_JS) or {}).get('links', []))
        progress('добавляю ссылку «%s»' % link['title'])
        r = tab.js(_ADD_LINK_JS)
        if r != 'clicked':
            raise ys.StudioError('Не найдена однозначная кнопка «Добавить ссылку»')
        ys.wait(tab, "[...document.querySelectorAll('ytcp-channel-link-item')].filter(e=>e.getClientRects().length).length>%d" % n,
                10, 'Studio не добавила поле для новой ссылки')
        _type(tab, ITEM % (n, 0), link['title'])
        _type(tab, ITEM % (n, 1), link['url'])
        changed.append('ссылка «%s»' % link['title'])
    return changed


_PUBLISH_POS = r"""(()=>{const h=document.querySelector('#publish-button');if(!h||!h.getClientRects().length)return null;
 const b=h.querySelector('button')||h;b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();
 return {x:r.left+r.width/2,y:r.top+r.height/2};})()"""


def _click_publish(tab):
    """Studio ignores a synthetic DOM click on «Опубликовать»: blur the field and send a real mouse click."""
    tab.js("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    time.sleep(0.6)
    p = tab.js(_PUBLISH_POS)
    if not p:
        raise ys.StudioError('Не найдена кнопка «Опубликовать»')
    tab.call('Input.dispatchMouseEvent', type='mouseMoved', x=p['x'], y=p['y'])
    tab.call('Input.dispatchMouseEvent', type='mousePressed', x=p['x'], y=p['y'], button='left', clickCount=1)
    tab.call('Input.dispatchMouseEvent', type='mouseReleased', x=p['x'], y=p['y'], button='left', clickCount=1)


def _read_saved(tab, browser, channel_id):
    """What is actually saved: the profile opened in a separate background tab (the edited page is left alone,
    so Chrome never asks «Leave site?»)."""
    if browser is None or not getattr(browser, 'port', None):
        tab.call('Page.navigate', url=PROFILE_URL.format(channel_id))
        time.sleep(2)
        return _wait_state(tab, channel_id)
    target = tab.call('Target.createTarget', url=PROFILE_URL.format(channel_id), background=True).get('targetId')
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end:
            pages = ys.local_json(f'http://127.0.0.1:{browser.port}/json/list')
            pg = next((x for x in pages if x.get('id') == target and x.get('webSocketDebuggerUrl')), None)
            if pg:
                t2 = ys.CDP(pg['webSocketDebuggerUrl'])
                try:
                    t2.call('Runtime.enable')
                    time.sleep(1.5)
                    return _wait_state(t2, channel_id)
                finally:
                    t2.close()
            time.sleep(0.5)
        raise ys.StudioError('Не открылась вкладка проверки профиля канала')
    finally:
        try:
            tab.call('Target.closeTarget', targetId=target)
        except Exception:  # noqa: BLE001
            pass


def _saved_ok(st, description, links, remove=()):
    if description is not None and norm_text(st['desc']) != norm_text(description):
        return 'описание на канале другое'
    left = [x.get('title') or x.get('url') for x in st['links'] if _matches(x, remove)]
    if left:
        return 'не удалились ссылки: ' + ', '.join(left)
    have = {norm_url(x['url']) for x in st['links']}
    missing = [x['title'] for x in links if norm_url(x['url']) not in have and not _matches(x, remove)]
    return ('нет ссылок: ' + ', '.join(missing)) if missing else ''


def apply_channel(tab, channel_id, description, links, progress=lambda m: None, browser=None, remove=()):
    changed = fill_channel(tab, channel_id, description, links, progress, remove=remove)
    if not changed:
        return 'уже всё так — без изменений'
    end = time.monotonic() + 15
    st = {}
    while time.monotonic() < end:
        st = tab.js(_STATE_JS) or {}
        if st.get('canPublish'):
            break
        time.sleep(0.5)
    else:
        tab.js(_CLICK_JS % json.dumps('discard-changes-button'))
        raise ys.StudioError('Studio не включила кнопку «Опубликовать»' + (': ' + '; '.join(st.get('errors') or []) if st.get('errors') else ''))
    progress('нажимаю «Опубликовать»')
    _click_publish(tab)
    progress('жду, пока Studio сохранит изменения')
    end, idle = time.monotonic() + 60, 0
    while time.monotonic() < end:
        time.sleep(0.5)
        st = tab.js(_STATE_JS) or {}
        if st.get('errors'):
            raise ys.StudioError('Studio не приняла изменения: ' + '; '.join(st['errors']))
        idle = idle + 1 if st.get('ready') and not st.get('canPublish') and not st.get('canDiscard') else 0
        if idle >= 4:
            break
    else:
        raise ys.StudioError('Studio не сохранила изменения за минуту — откройте профиль канала в Studio и проверьте')
    progress('проверяю, что изменения сохранились')
    problem = 'не удалось прочитать профиль'
    for _ in range(3):  # Studio может отдавать сохранённое с небольшой задержкой
        problem = _saved_ok(_read_saved(tab, browser, channel_id), description, links, remove)
        if not problem:
            return 'обновлено: ' + ', '.join(changed)
        time.sleep(5)
    raise ys.StudioError('После публикации ' + problem + ' — проверьте профиль канала в Studio')


def clean_links(links):
    out = []
    for i, x in enumerate(links or []):
        title, url = str(x.get('title') or '').strip(), str(x.get('url') or '').strip()
        if not title and not url:
            continue
        if not title or not url:
            raise ValueError(f'Ссылка {i + 1}: нужны и название, и адрес')
        if not re.match(r'^https?://\S+\.\S+', url):
            raise ValueError(f'Ссылка {i + 1}: адрес должен начинаться с https://')
        if len(title) > 100:
            raise ValueError(f'Ссылка {i + 1}: название длиннее 100 символов')
        out.append({'title': title, 'url': url})
    return out


class ChannelEditor:
    def __init__(self, root, service):
        self.root = Path(root)
        self.service = service
        self.path = self.root / 'state' / 'channel_profile.json'
        self.status = {}          # profile -> {'kind','text','at'}
        self.running = False
        self._stop = threading.Event()

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'description': '', 'update_description': True, 'links': [], 'unselected': []}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.path)

    def _set(self, profile, kind, text):
        self.status[profile] = {'kind': kind, 'text': text, 'at': time.time()}

    def _log(self, text):
        try:
            self.service.log('Каналы: ' + text)
        except Exception:  # noqa: BLE001
            pass

    def start(self, channels, description, links, on_change=lambda: None, remove=None):
        """channels: [{'profile','channel_id'}]; description: str или None (не менять)."""
        if self.running:
            raise ValueError('Каналы уже обновляются')
        if description is not None and len(description) > 1000:
            raise ValueError('Описание длиннее 1000 символов — YouTube такое не примет')
        self._remove = list(remove or [])
        if description is None and not links and not self._remove:
            raise ValueError('Нечего применять: включите описание, добавьте ссылку или укажите ссылку для удаления')
        if not channels:
            raise ValueError('Не выбрано ни одного канала')
        for c in channels:
            self._set(c['profile'], 'info', 'в очереди')
        self.running = True
        self._stop.clear()
        threading.Thread(target=self._run, args=(channels, description, links, on_change), name='channel-editor', daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self, channels, description, links, on_change):
        ok = 0
        try:
            for c in channels:
                if self._stop.is_set():
                    self._set(c['profile'], 'muted', 'отменено')
                    continue
                p = c['profile']
                self._set(p, 'warn', 'ждёт браузер YouTube')
                on_change()
                lock = ys.profile_lock(p)  # только этот канал: остальные каналы не ждут
                while not lock.acquire(timeout=5):
                    self._set(p, 'warn', 'ждёт: в Chrome канала открыто окно загрузки')
                    on_change()
                    if self._stop.is_set():
                        break
                else:
                    try:
                        browser = ys.Browser(self.root, p)
                        with browser as tab:
                            res = apply_channel(tab, c['channel_id'], description, links, remove=getattr(self, '_remove', []),
                                                progress=lambda m, p=p: (self._set(p, 'warn', m), on_change()), browser=browser)
                            browser.keep_open = False
                        self._set(p, 'ok', res)
                        self._log(f'{p}: {res}')
                        ok += 1
                    except Exception as exc:  # noqa: BLE001 — ошибка одного канала не останавливает остальные
                        self._set(p, 'error', str(exc)[:300] or type(exc).__name__)
                        self._log(f'{p}: ошибка — {exc}')
                    finally:
                        lock.release()
                    on_change()
                    continue
                self._set(p, 'muted', 'отменено')
        finally:
            self.running = False
            self._log(f'готово: {ok} из {len(channels)}')
            on_change()
