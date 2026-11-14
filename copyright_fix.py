"""Автоисправление заявок об авторских правах.

Если видео заблокировано в части стран (обычно Россия и Беларусь) из-за заявленной музыки, бот делает
в Studio то же, что вручную: «Принять меры» → «Удалить трек» → «Далее» → «Удалить трек» (речь остаётся)
→ «Сохранить» → галочка «Я понимаю…» → «Подтвердить изменения».

Поиск заблокированных видео публичный (список стран в данных видео) и не открывает Chrome; Chrome канала
нужен только когда есть что исправлять, и берётся замок только этого канала."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import channel_stats as cs
import youtube_studio as ys

CHECK_EVERY = 20 * 60      # как часто проверять опубликованные видео
RETRY_AFTER = 3 * 3600     # повтор, если после правки видео всё ещё заблокировано (YouTube обрабатывает правку)
MAX_ATTEMPTS = 3           # сколько раз пробовать одно видео, потом — уведомление для ручной проверки
RECENT_DAYS = 45           # какие видео проверять (выложенные ботом за последние дни)


class ClaimFixError(RuntimeError):
    pass


def blocked_status(client, video_id):
    """'blocked' — видео недоступно хотя бы в части стран; 'ok'; 'unknown' — не удалось понять."""
    try:
        j = client.api('player', {'videoId': video_id})
    except Exception:  # noqa: BLE001
        return 'unknown'
    mf = (j.get('microformat') or {}).get('playerMicroformatRenderer') or {}
    countries = mf.get('availableCountries')
    if isinstance(countries, list) and countries:
        return 'blocked' if ('RU' not in countries or 'BY' not in countries or len(countries) < 240) else 'ok'
    text = json.dumps(j.get('playabilityStatus') or {}, ensure_ascii=False).lower()
    if re.search(r'заблокирован|blocked|авторск|copyright', text):
        return 'blocked'
    return 'unknown'


# ---------- Studio ----------
_VISIBLE = "const vis=e=>!!(e&&e.getClientRects().length);"


def _poll(tab, expression, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = tab.js(expression)
        if value:
            return value
        time.sleep(0.6)
    raise ClaimFixError(message)


def _click(tab, selector, timeout, message):
    """Нажать единственную видимую и активную кнопку."""
    expr = "(()=>{" + _VISIBLE + "const es=[...document.querySelectorAll(%s)].filter(vis);" \
           "if(es.length!==1)return false;const b=es[0];" \
           "if(b.getAttribute('aria-disabled')==='true'||b.hasAttribute('disabled')&&b.getAttribute('disabled')!=='false')return false;" \
           "b.click();return true})()" % json.dumps(selector)
    _poll(tab, expr, timeout, message)


def fix_claims(tab, video_id, progress=None):
    """Одна заявка за проход (после правки YouTube блокирует видео до конца обработки).
    Возвращает 'fixed', 'processing' (правка уже обрабатывается), 'no_claims' или 'no_erase'."""
    say = progress or (lambda m: None)
    tab.call('Page.navigate', url=f'https://studio.youtube.com/video/{video_id}/claims?hl=ru')
    _poll(tab, "document.readyState==='complete'&&!!document.querySelector('ytcr-video-home-page')", 60,
          'Не открылась страница заявок в Studio')
    time.sleep(4)
    text = tab.js("document.body.innerText") or ''
    if re.search(r'изменения обрабатываются|changes are (?:being )?process|processing (?:your )?changes', text, re.I):
        return 'processing'
    count = tab.js("(()=>{" + _VISIBLE + "return [...document.querySelectorAll('ytcp-button#actions-button')].filter(vis).length})()") or 0
    if not count:
        return 'no_claims'
    say('заявка найдена — «Принять меры»')
    tab.js("(()=>{" + _VISIBLE + "const b=[...document.querySelectorAll('ytcp-button#actions-button')].filter(vis)[0];b.click()})()")
    _poll(tab, "(()=>{" + _VISIBLE + "return vis(document.querySelector('ytcr-video-actions-dialog #confirm-button'))})()", 25,
          'Не открылось окно «Выберите действие»')
    card = 'ytcr-video-actions-dialog button#action-card-NON_TAKEDOWN_CLAIM_OPTION_ERASE_SONG'
    if not tab.js("(()=>{" + _VISIBLE + "return vis(document.querySelector(%s))})()" % json.dumps(card)):
        tab.js("(()=>{const b=document.querySelector('ytcr-video-actions-dialog #cancel-button');b&&b.click()})()")
        return 'no_erase'
    say('«Удалить трек» → «Далее»')
    tab.js("document.querySelector(%s).click()" % json.dumps(card))
    _click(tab, 'ytcr-video-actions-dialog ytcp-button#confirm-button', 15, 'Кнопка «Далее» не стала активной')
    radio = 'ytcr-remove-song tp-yt-paper-radio-button#WAVE_FORM_ERASE'
    _poll(tab, "(()=>{" + _VISIBLE + "return vis(document.querySelector(%s))})()" % json.dumps(radio), 40,
          'Не открылось окно «Удаление трека»')
    if tab.js("document.querySelector(%s).getAttribute('aria-checked')" % json.dumps(radio)) != 'true':
        tab.js("document.querySelector(%s).click()" % json.dumps(radio))
    _poll(tab, "document.querySelector(%s).getAttribute('aria-checked')==='true'" % json.dumps(radio), 10,
          'Не удалось выбрать «Удалить трек» (сохранить речь)')
    say('«Сохранить»')
    _click(tab, 'ytcr-editing-tool-dialog ytcp-button#continue-button', 20, 'Кнопка «Сохранить» не стала активной')
    box = 'ytve-save-dialog ytcp-checkbox-lit#confirm-checkbox [role=checkbox]'
    _poll(tab, "(()=>{" + _VISIBLE + "return vis(document.querySelector(%s))})()" % json.dumps(box), 30,
          'Не открылось окно «Подтвердите изменения»')
    if tab.js("document.querySelector(%s).getAttribute('aria-checked')" % json.dumps(box)) != 'true':
        tab.js("document.querySelector(%s).click()" % json.dumps(box))
    say('«Подтвердить изменения»')
    _click(tab, 'ytve-save-dialog ytcp-button#apply-button', 15, 'Кнопка «Подтвердить изменения» не стала активной')
    _poll(tab, "/изменения обрабатываются|changes are (?:being )?process|processing (?:your )?changes/i.test(document.body.innerText)"
               "||![...document.querySelectorAll('ytve-save-dialog tp-yt-paper-dialog')].some(e=>e.getClientRects().length)",
          60, 'Studio не подтвердила применение изменений')
    return 'fixed'


# ---------- фоновая проверка ----------
class CopyrightDoctor:
    def __init__(self, root, service):
        self.root = Path(root)
        self.service = service
        self.path = self.root / 'state' / 'copyright_fixes.json'
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.state = {}
        self._last = 0.0

    def _save(self):
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

    def _log(self, text):
        self.service.log('Авторские права: ' + text)

    def _notify(self, text):
        try:
            self.service.store.notify(text)
        except Exception:  # noqa: BLE001
            pass

    def candidates(self):
        """{video_id: (profile, title)} — видео, выложенные ботом за последние RECENT_DAYS дней."""
        since = time.time() - RECENT_DAYS * 86400
        out = {}
        db = sqlite3.connect('file:' + str(self.root / 'state' / 'jobs.sqlite3') + '?mode=ro', uri=True, timeout=30)
        try:
            for profile, vid, title in db.execute("SELECT profile, video_id, title FROM youtube_uploads WHERE state='confirmed' "
                                                  "AND video_id IS NOT NULL AND video_id!='' AND updated_at>=?", (since,)):
                out[vid] = (profile, title)
        finally:
            db.close()
        shorts = self.root / 'state' / 'shorts.sqlite3'
        if shorts.exists():
            db = sqlite3.connect('file:' + str(shorts) + '?mode=ro', uri=True, timeout=30)
            try:
                for profile, vid, title in db.execute("SELECT profile, video_id, title FROM items WHERE status='done' "
                                                      "AND video_id IS NOT NULL AND video_id!='' AND updated_at>=?", (since,)):
                    out.setdefault(vid, (profile, title))
            finally:
                db.close()
        return {v: p for v, p in out.items() if p[0] and not str(p[0]).startswith('__')}

    def tick(self, stop_event):
        if time.time() - self._last < CHECK_EVERY:
            return
        self._last = time.time()
        client = cs._Client()
        todo = {}
        for vid, (profile, title) in self.candidates().items():
            st = self.state.get(vid) or {}
            if st.get('gave_up') or time.time() - float(st.get('last') or 0) < RETRY_AFTER:
                continue
            if blocked_status(client, vid) == 'blocked':
                todo.setdefault(profile, []).append((vid, title))
        for profile, items in todo.items():
            if stop_event.is_set():
                return
            lock = ys.profile_lock(profile)
            if not lock.acquire(timeout=60):
                self._log(f'{profile}: в Chrome канала идёт загрузка — исправлю позже')
                self._last = 0.0  # повторить на следующем проходе
                continue
            try:
                self._log(f'{profile}: заблокированных видео {len(items)} — исправляю заявки в Studio')
                browser = ys.Browser(self.root, profile)
                with browser as tab:
                    for vid, title in items:
                        self._fix_one(tab, profile, vid, title)
                    browser.keep_open = False
            except Exception as exc:  # noqa: BLE001
                self._log(f'{profile}: не удалось открыть Studio ({type(exc).__name__}: {str(exc)[:150]})')
            finally:
                lock.release()

    def _fix_one(self, tab, profile, vid, title):
        st = self.state.setdefault(vid, {'attempts': 0})
        try:
            res = fix_claims(tab, vid, progress=lambda m: self._log(f'{profile} {vid}: {m}'))
        except Exception as exc:  # noqa: BLE001
            res = f'ошибка: {type(exc).__name__}: {str(exc)[:150]}'
        st['last'] = time.time()
        st['result'] = res
        st['profile'] = profile
        url = f'https://youtu.be/{vid}'
        if res == 'fixed':
            st['attempts'] = int(st.get('attempts') or 0) + 1
            self._log(f'{profile}: «{title}» — музыка удалена (речь сохранена), YouTube обрабатывает изменения {url}')
            self._notify(f'Авторские права: «{title}» был заблокирован в некоторых странах — бот удалил заявленную музыку {url}')
        elif res == 'processing':
            self._log(f'{profile}: «{title}» — правка ещё обрабатывается YouTube {url}')
        elif res == 'no_erase':
            st['gave_up'] = True
            self._log(f'{profile}: «{title}» — у заявки нет варианта «Удалить трек», нужна ручная проверка {url}')
            self._notify(f'Авторские права: «{title}» заблокирован, но «Удалить трек» недоступно — проверьте вручную {url}')
        else:
            st['attempts'] = int(st.get('attempts') or 0) + 1
            self._log(f'{profile}: «{title}» — {res} {url}')
        if not st.get('gave_up') and int(st.get('attempts') or 0) >= MAX_ATTEMPTS and res != 'processing':
            st['gave_up'] = True
            self._notify(f'Авторские права: «{title}» всё ещё заблокирован после {MAX_ATTEMPTS} попыток — проверьте вручную {url}')
        self._save()
