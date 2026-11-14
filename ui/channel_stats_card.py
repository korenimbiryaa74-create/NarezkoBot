"""Карточка «Статистика» во вкладке «Каналы»: просмотры обычных нарезок, Shorts и сумма по каждому каналу.
Данные собирает channel_stats.py в фоне; эта карточка только показывает их и не трогает загрузки."""
from __future__ import annotations

import datetime

import flet as ft

from channel_stats import ChannelStats, growth, totals
from ui.common import MUTED, card, chip, empty, muted, ts

TILE_BG = '#1b1e24'
MAX_LIST = 60


def fmt(n):
    try:
        return f'{int(n):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return '—'


def _avg(views, count):
    return fmt(round(views / count)) if count else '—'


def _date(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso)).astimezone().strftime('%d.%m.%y')
    except (TypeError, ValueError):
        return ''


def _dur(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ''
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'


class ChannelStatsCard:
    def __init__(self, app, get_channels):
        self.app = app
        self.get_channels = get_channels
        self.stats = ChannelStats(app.root)
        self.expanded = set()
        self._shown = None
        self._auto_done = False
        self.body = ft.Column(spacing=10)
        self.state = muted('', size=12)
        self.btn = ft.OutlinedButton('Обновить статистику', icon=ft.Icons.REFRESH, on_click=lambda e: self.update_now())
        self.control = card(self.body, title='Статистика', icon=ft.Icons.INSIGHTS,
                            subtitle='Просмотры с YouTube: обычные нарезки, Shorts и всего. Учитываются только опубликованные видео; '
                                     'данные берутся с открытых страниц каналов и не мешают загрузкам',
                            actions=[self.state, self.btn])

    def _targets(self):
        return [c for c in (self.get_channels() or [])
                if c.get('channel_id') and not str(c.get('profile', '')).startswith('__')]

    def update_now(self, auto=False):
        try:
            chans = self._targets()
            if not chans:
                if not auto:
                    self.app.dialogs.toast('Нет привязанных каналов', 'error')
                return
            if self.stats.start(chans, on_done=self.app.refresh_soon) and not auto:
                self.app.dialogs.toast(f'Обновляю статистику каналов: {len(chans)}', 'ok')
        except Exception as exc:  # noqa: BLE001
            self.app.dialogs.toast(f'Статистика: {exc}', 'error')
        self.app.refresh_soon()

    def _toggle(self, profile):
        self.expanded ^= {profile}
        self._shown = None
        self.app.refresh_soon()

    def refresh(self):
        if not self._auto_done and self._targets():
            self._auto_done = True
            if self.stats.stale():
                self.update_now(auto=True)
        s = self.stats
        self.btn.disabled = s.running
        if s.running:
            self.state.value, self.state.color = (s.progress or 'обновляю…'), MUTED
        elif s.error:
            self.state.value, self.state.color = s.error, '#ff8a8a'
        else:
            self.state.value = ('обновлено ' + ts(s.data['updated'])) if s.data.get('updated') else ''
            self.state.color = MUTED
        key = (s.version, s.running and not s.data.get('channels'), tuple(sorted(self.expanded)))
        if key != self._shown:
            self._shown = key
            self.body.controls = self._build()

    # ---------- layout ----------
    def _build(self):
        data = self.stats.data
        chans = [c for c in data.get('channels') or [] if isinstance(c, dict)]
        if not chans:
            return [empty('Собираю статистику…' if self.stats.running else 'Статистики пока нет — нажмите «Обновить статистику»')]
        tot = [totals(c) for c in chans]
        def total(k):
            return sum(t[k] for t in tot)
        subs = sum(int(c.get('subs') or 0) for c in chans)
        rows = [ft.Row([
            self._tile('Всего просмотров', fmt(total('views')), f"{fmt(total('clips') + total('shorts'))} видео"),
            self._tile('Обычные нарезки', fmt(total('clip_views')), f"{fmt(total('clips'))} видео · в среднем {_avg(total('clip_views'), total('clips'))}"),
            self._tile('Shorts', fmt(total('short_views')), f"{fmt(total('shorts'))} видео · в среднем {_avg(total('short_views'), total('shorts'))}"),
            self._tile('Подписчики', fmt(subs), f'каналов: {len(chans)}'),
        ], spacing=10, wrap=True)]
        for c, t in sorted(zip(chans, tot), key=lambda x: -x[1]['views']):
            rows.append(self._channel(c, t, data))
        return rows

    def _tile(self, label, value, sub):
        return ft.Container(ft.Column([muted(label, size=12), ft.Text(value, size=22, weight=ft.FontWeight.BOLD), muted(sub, size=11)],
                                      spacing=2, tight=True),
                            bgcolor=TILE_BG, border_radius=12, padding=12, width=240)

    def _block(self, label, count, views):
        return ft.Column([muted(label, size=11), ft.Text(fmt(views), size=16, weight=ft.FontWeight.W_600),
                          muted(f'{fmt(count)} видео · ср. {_avg(views, count)}', size=11)], spacing=0, tight=True, width=170)

    def _channel(self, c, t, data):
        p = str(c.get('profile', ''))
        if c.get('subs_text'):
            subs = c['subs_text']
        elif c.get('subs') is not None:
            subs = f"{fmt(c['subs'])} подписчиков"
        else:
            subs = 'подписчики скрыты'
        name = [ft.Text(p, size=14, weight=ft.FontWeight.W_600)]
        if c.get('title'):
            name.append(muted(c['title'], size=11, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
        name.append(muted(subs, size=11))
        head = [ft.Column(name, spacing=0, tight=True, width=190),
                self._block('Обычные нарезки', t['clips'], t['clip_views']),
                self._block('Shorts', t['shorts'], t['short_views']),
                self._block('Всего', t['clips'] + t['shorts'], t['views'])]
        g = growth(data, p)
        if g:
            head.append(chip(f'{"+" if g[0] >= 0 else ""}{fmt(g[0])} с {ts(g[1])}', 'ok' if g[0] > 0 else 'muted'))
        if c.get('error'):
            head.append(chip('не обновилось', 'error'))
        is_open = p in self.expanded
        head.append(ft.TextButton('Скрыть видео' if is_open else 'Видео', icon=ft.Icons.EXPAND_LESS if is_open else ft.Icons.EXPAND_MORE,
                                  on_click=lambda e, p=p: self._toggle(p)))
        parts = [ft.Row(head, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True)]
        if c.get('error'):
            parts.append(muted('Показаны прошлые данные: ' + str(c['error']), size=11))
        if is_open:
            parts.append(self._videos(c))
        return ft.Container(ft.Column(parts, spacing=8, tight=True), bgcolor=TILE_BG, border_radius=10, padding=10)

    def _videos(self, c):
        vids = sorted((v for v in c.get('videos') or [] if isinstance(v, dict)), key=lambda v: -(v.get('views') or 0))
        if not vids:
            return muted('Опубликованных видео пока нет', size=12)
        lines = []
        for v in vids[:MAX_LIST]:
            short = v.get('kind') == 'short'
            lines.append(ft.Row([
                chip('Shorts' if short else 'Нарезка', 'info' if short else 'muted'),
                ft.Text(v.get('title') or v.get('id') or '', size=12, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                muted(_dur(v.get('length')), size=11),
                muted(_date(v.get('published')), size=11),
                ft.Text(fmt(v.get('views')) + ('' if v.get('exact', True) else ' ≈'), size=12, weight=ft.FontWeight.W_600,
                        width=90, text_align=ft.TextAlign.RIGHT),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        if len(vids) > MAX_LIST:
            lines.append(muted(f'… и ещё {len(vids) - MAX_LIST}', size=11))
        return ft.Column(lines, spacing=4, tight=True)
