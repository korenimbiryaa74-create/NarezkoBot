"""Вкладка «Каналы»: статистика просмотров, общее описание и ссылки сразу для всех привязанных YouTube-каналов."""
from __future__ import annotations

import flet as ft

from channel_editor import ChannelEditor, clean_links
from ui.common import card, chip, empty, muted, note, page_header, ts

try:  # статистика — необязательная часть: при любой ошибке вкладка работает как раньше
    from ui.channel_stats_card import ChannelStatsCard
except Exception:  # noqa: BLE001
    ChannelStatsCard = None


class ChannelsView:
    def __init__(self, app):
        self.app = app
        self.editor = ChannelEditor(app.root, app.service)
        data = self.editor.load()
        self.unselected = set(data.get('unselected') or [])
        self.desc_on = ft.Checkbox(label='Заменить описание на выбранных каналах', value=bool(data.get('update_description', True)))
        self.desc = ft.TextField(label='Описание канала', value=data.get('description', ''), multiline=True,
                                 min_lines=4, max_lines=12, max_length=1000)
        self.links_col = ft.Column(spacing=8)
        self.link_rows = []
        for link in (data.get('links') or [{}]):
            self._add_link(link, update=False)
        self.remove_col = ft.Column(spacing=8)
        self.remove_rows = []
        for link in (data.get('remove') or [{}]):
            self._add_remove(link, update=False)
        self.channels_col = ft.Column(spacing=6)
        self.rows = {}
        self.run_btn = ft.FilledButton('Применить к выбранным каналам', icon=ft.Icons.PUBLISH, on_click=lambda e: self._apply())
        self.stop_btn = ft.TextButton('Остановить', icon=ft.Icons.STOP_CIRCLE_OUTLINED, on_click=lambda e: self.editor.stop(), visible=False)
        self.state = muted('')
        self.stats_card = None
        if ChannelStatsCard is not None:
            try:
                self.stats_card = ChannelStatsCard(app, lambda: self._channels(self._last))
            except Exception:  # noqa: BLE001
                self.stats_card = None
        self._init_en()
        self.view = ft.Column([
            page_header('Каналы', 'Статистика просмотров, общее описание и ссылки сразу для всех привязанных YouTube-каналов'),
            *([self.stats_card.control] if self.stats_card else []),
            self.en_card,
            card(self.desc_on, self.desc, title='Описание', icon=ft.Icons.DESCRIPTION_OUTLINED,
                 subtitle='Текст целиком заменит текущее описание на каждом выбранном канале'),
            card(self.links_col,
                 ft.Row([ft.OutlinedButton('Добавить ссылку', icon=ft.Icons.ADD_LINK, on_click=lambda e: self._add_link({}))]),
                 title='Ссылки', icon=ft.Icons.LINK,
                 subtitle='Добавляются на каждый канал, если такой ссылки там ещё нет. Существующие ссылки не трогаются'),
            card(self.remove_col,
                 ft.Row([ft.OutlinedButton('Добавить ссылку для удаления', icon=ft.Icons.LINK_OFF, on_click=lambda e: self._add_remove({}))]),
                 title='Удалить ссылки с каналов', icon=ft.Icons.LINK_OFF,
                 subtitle='Ссылка с таким адресом удаляется на каждом выбранном канале. Если адрес не указан — удаляется ссылка с таким названием'),
            card(self.channels_col,
                 note('Бот по очереди открывает Chrome каждого канала → «Настройка канала → Профиль», вносит изменения, нажимает '
                      '«Опубликовать» и проверяет, что всё сохранилось. Если браузер YouTube занят загрузкой роликов — ждёт.', 'info'),
                 ft.Row([self.run_btn, self.stop_btn, self.state], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 title='Куда применить', icon=ft.Icons.SMART_DISPLAY_OUTLINED),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------- links ----------
    def _add_link(self, link, update=True):
        title = ft.TextField(label='Название', value=link.get('title', ''), dense=True, width=240)
        url = ft.TextField(label='Адрес (https://…)', value=link.get('url', ''), dense=True, expand=True)
        row = ft.Row([title, url], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        row.controls.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip='Убрать из списка', on_click=lambda e, r=row: self._remove_link(r)))
        row.data = (title, url)
        self.link_rows.append(row)
        self.links_col.controls.append(row)
        if update:
            self.app.page.update()

    def _remove_link(self, row):
        if row in self.link_rows:
            self.link_rows.remove(row)
            self.links_col.controls.remove(row)
        if not self.link_rows:
            self._add_link({}, update=False)
        self.app.page.update()

    def _add_remove(self, link, update=True):
        title = ft.TextField(label='Название (если адрес не знаете)', value=link.get('title', ''), dense=True, width=260)
        url = ft.TextField(label='Адрес ссылки, которую удалить (https://…)', value=link.get('url', ''), dense=True, expand=True)
        row = ft.Row([title, url], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        row.controls.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip='Убрать из списка', on_click=lambda e, r=row: self._drop_remove(r)))
        row.data = (title, url)
        self.remove_rows.append(row)
        self.remove_col.controls.append(row)
        if update:
            self.app.page.update()

    def _drop_remove(self, row):
        if row in self.remove_rows:
            self.remove_rows.remove(row)
            self.remove_col.controls.remove(row)
        if not self.remove_rows:
            self._add_remove({}, update=False)
        self.app.page.update()

    def _removes(self):
        out = []
        for i, r in enumerate(self.remove_rows, 1):
            title, url = (r.data[0].value or '').strip(), (r.data[1].value or '').strip()
            if not title and not url:
                continue
            if url and not url.lower().startswith(('http://', 'https://')):
                raise ValueError(f'Удаление, строка {i}: адрес должен начинаться с https://')
            out.append({'title': title, 'url': url})
        return out

    def _links(self):
        return [{'title': r.data[0].value or '', 'url': r.data[1].value or ''} for r in self.link_rows]

    # ---------- apply ----------
    def _channels(self, snap=None):
        s = (snap or {}).get('shorts') or {}
        chans = s.get('channels')
        if chans is None:
            chans = self.editor.service.youtube_queue.list_profiles()
        return [{'profile': c['profile'], 'channel_id': c['channel_id']} for c in chans if c.get('channel_id')]

    def _apply(self):
        try:
            links = clean_links(self._links())
            remove = self._removes()
            description = (self.desc.value or '').strip() if self.desc_on.value else None
            chosen = [c for c in self._channels(self._last) if self.rows.get(c['profile']) and self.rows[c['profile']][0].value]
            self.unselected = {p for p, r in self.rows.items() if not r[0].value}
            self.editor.save({'description': self.desc.value or '', 'update_description': bool(self.desc_on.value),
                              'links': links, 'remove': remove, 'unselected': sorted(self.unselected)})
            self.editor.start(chosen, description, links, on_change=self.app.refresh_soon, remove=remove)
            self.app.dialogs.toast(f'Обновляю каналов: {len(chosen)}. Ход — ниже, у каждого канала.', 'ok')
        except Exception as exc:  # noqa: BLE001
            self.app.dialogs.toast(str(exc), 'error')
        self.app.refresh_soon()

    # ---------- refresh ----------
    _last = None

    def refresh(self, snap):
        self._last = snap
        chans = self._channels(snap)
        profiles = [c['profile'] for c in chans]
        if profiles != list(self.rows):
            self.rows = {}
            ctrls = []
            for c in chans:
                box = ft.Checkbox(value=c['profile'] not in self.unselected)
                st_chip, st_text = ft.Container(), muted('', size=12)
                ctrls.append(ft.Container(ft.Row([box, ft.Column([ft.Text(c['profile'], size=14, weight=ft.FontWeight.W_600),
                                                                   muted(c['channel_id'], size=11)], spacing=0, tight=True, width=260),
                                                  st_chip, ft.Container(st_text, expand=True)],
                                                 spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                          bgcolor='#1b1e24', border_radius=10, padding=10))
                self.rows[c['profile']] = (box, st_chip, st_text)
            self.channels_col.controls = ctrls or [empty('Нет привязанных каналов. Привяжите канал во вкладке YouTube.')]
        for p, (box, st_chip, st_text) in self.rows.items():
            st = self.editor.status.get(p)
            if st:
                label = {'ok': 'готово', 'error': 'ошибка', 'warn': 'в работе', 'info': 'в очереди', 'muted': 'отменено'}.get(st['kind'], st['kind'])
                st_chip.content = chip(label, st['kind'])
                st_text.value = f"{ts(st['at'], '%H:%M')} — {st['text']}"
            else:
                st_chip.content, st_text.value = None, ''
        running = self.editor.running
        self.run_btn.disabled = running
        self.stop_btn.visible = running
        self.state.value = 'идёт обновление каналов…' if running else ''
        if self.stats_card:
            try:
                self.stats_card.refresh()
            except Exception:  # noqa: BLE001 — ошибка статистики не должна мешать вкладке
                pass
        try:
            self._refresh_en(chans)
        except Exception:  # noqa: BLE001
            pass

    # ---------- английские (США) каналы ----------
    def _cfg(self):
        return getattr(self.app, 'config', None) or {}

    def _init_en(self):
        cfg = self._cfg()
        langs = cfg.get('languages') or {}
        self.en_rows = {}
        self.en_box = ft.Row(spacing=16, wrap=True)
        extra = sorted(k for k, v in langs.items() if v == 'en')
        self.en_extra = ft.TextField(label='Стримеры на английском без привязанного канала (логины Twitch через запятую)',
                                     value=', '.join(extra), dense=True)
        footer = (cfg.get('youtube') or {}).get('description_footer_en')
        self.en_footer = ft.TextField(label='Подпись в описании всех английских видео', multiline=True, min_lines=2, max_lines=8,
                                      value=footer if isinstance(footer, str) else '💬 My Telegram - https://t.me/korenmbr')
        self.en_state = muted('', size=12)
        self.en_card = card(
            note('Для отмеченных каналов всё на английском: распознавание речи, запросы ChatGPT, названия («NAME REACTS: …»), '
                 'описание («Name\'s links»), таймкоды, текст на превью и Shorts.', 'info'),
            self.en_box, self.en_extra, self.en_footer,
            ft.Row([ft.FilledButton('Сохранить', icon=ft.Icons.SAVE, on_click=lambda e: self._save_en()), self.en_state], spacing=12,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            title='Английские каналы (США)', icon=ft.Icons.LANGUAGE,
            subtitle='Язык задаётся по стримеру (логин Twitch = профиль канала)')

    def _refresh_en(self, chans):
        profiles = [c['profile'] for c in chans if not str(c['profile']).startswith('__')]
        if profiles == list(self.en_rows):
            return
        langs = self._cfg().get('languages') or {}
        self.en_rows = {p: ft.Checkbox(label=p, value=langs.get(str(p).lower()) == 'en') for p in profiles}
        self.en_box.controls = list(self.en_rows.values()) or [muted('Нет привязанных каналов', size=12)]
        bound = {str(p).lower() for p in profiles}
        extra = [x.strip().lower() for x in (self.en_extra.value or '').split(',') if x.strip()]
        self.en_extra.value = ', '.join(x for x in extra if x not in bound)

    def _save_en(self):
        try:
            langs = {str(p).lower(): 'en' for p, box in self.en_rows.items() if box.value}
            for x in (self.en_extra.value or '').split(','):
                x = x.strip().lower().lstrip('@')
                if x:
                    if not x.replace('_', '').isalnum():
                        raise ValueError(f'Некорректный логин Twitch: {x}')
                    langs[x] = 'en'
            footer = (self.en_footer.value or '').strip()

            def mutate(c):
                c['languages'] = langs
                c.setdefault('youtube', {})['description_footer_en'] = footer

            if self.app.save_config(mutate) is False:
                return
            self.en_state.value = 'сохранено: английских — ' + str(len(langs))
            self.app.dialogs.toast('Английские каналы сохранены', 'ok')
        except Exception as exc:  # noqa: BLE001
            self.app.dialogs.toast(str(exc), 'error')
        self.app.refresh_soon()
