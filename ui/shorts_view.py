"""YouTube Shorts tab: per-channel autopilot (queue, interval, mode) on top of ShortsFactory."""
from __future__ import annotations
import time
import flet as ft
from ui.common import card, chip, muted, note, page_header, empty, ts, MUTED
from shorts_autopilot import MODES

STATUS = {'queued': ('в очереди', 'info'), 'working': ('в работе', 'warn'), 'done': ('опубликован', 'ok'), 'failed': ('ошибка', 'error')}
KIND = {'post': 'Пост', 'fact': 'Факт', 'telegram': 'Telegram', 'auto': 'Сам', 'moment': 'Момент'}


def _when(t):
    if not t:
        return 'сейчас'
    if t - time.time() < 60:
        return 'сейчас'
    return ts(t, '%d.%m %H:%M')


class ChannelRow:
    def __init__(self, view, ch):
        self.view, self.profile = view, ch['profile']
        pilot = view.pilot
        self.enabled = ft.Switch(value=bool(ch['enabled']), on_change=lambda e: self._save(enabled=int(bool(e.control.value))))
        self.interval = ft.Dropdown(label='Интервал', width=170, dense=True, value=str(ch['interval_hours']),
                                    options=[ft.DropdownOption(key=str(h), text=f'каждые {h} ч' if h > 1 else 'каждый час') for h in range(1, 25)])
        self.interval.on_select = lambda e: self._save(interval_hours=int(self.interval.value))
        self.mode = ft.Dropdown(label='Откуда ролики', width=300, dense=True, value=ch['mode'],
                                options=[ft.DropdownOption(key=k, text=t) for k, t in MODES])
        self.mode.on_select = lambda e: self._save(mode=self.mode.value)
        self.info = muted('', size=12)
        self.note = ft.Text('', size=12, color='#f3c95b')
        self.view_ctrl = ft.Container(ft.Column([
            ft.Row([self.enabled,
                    ft.Column([ft.Text(self.profile, size=15, weight=ft.FontWeight.W_600), muted(ch['channel_id'], size=11)], spacing=0, tight=True, expand=True),
                    ft.OutlinedButton('Сделать сейчас', icon=ft.Icons.BOLT, on_click=lambda e: self._now())],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.interval, self.mode], wrap=True, spacing=10),
            self.info, self.note,
        ], spacing=8, tight=True), bgcolor='#1b1e24', border_radius=12, padding=14)
        self.update(ch)

    def update(self, ch):
        if ch['enabled']:
            nxt = 'сейчас' if ch['next_due'] <= time.time() else _when(ch['next_due'])
            sched = f'Следующий ролик: {nxt}'
        else:
            sched = 'Выключен'
        last = f" · последний: {ts(ch['last_upload'])}" if ch['last_upload'] else ''
        self.info.value = f"{sched} · в очереди: {ch['queued']} · опубликовано: {ch['done']}{last}"
        self.note.value = ch.get('note') or ''
        self.note.visible = bool(self.note.value)
        if not ch.get('audience'):
            self.note.value = 'У канала не выбрана аудитория — укажите её во вкладке YouTube, иначе загрузка не начнётся'
            self.note.visible = True

    def _save(self, **fields):
        try:
            self.view.pilot.store.save_channel(self.profile, **fields)
            self.view.pilot.wake.set()
        except Exception as exc:
            self.view.app.dialogs.toast(str(exc), 'error')
        self.view.app.refresh_soon()

    def _now(self):
        pilot = self.view.pilot
        problem = pilot.factory_problem()
        if problem:
            self.view.app.dialogs.toast(problem, 'error')
            return
        pilot.run_now(self.profile)
        queued = pilot.store.next_item(self.profile, ('post', 'fact', 'telegram'))
        if self.mode.value == 'stream':
            what = 'из следующего момента стрима' if pilot.store.next_item(self.profile, ('moment',)) else '— но моментов пока нет (появятся после следующего нарезанного стрима)'
        else:
            what = 'из очереди' if queued and self.mode.value != 'auto' else 'на научную тему (очередь пуста)' if self.mode.value != 'auto' else 'на научную тему'
        busy = ' Сейчас идёт нарезка стрима — начну сразу после неё.' if pilot.stream_busy() else ''
        self.view.app.dialogs.toast(f'{self.profile}: делаю ролик {what} и выкладываю — 2–4 минуты, ход виден в «Состоянии».{busy}', 'ok')
        self.view.app.refresh_soon()

class ShortsView:
    def __init__(self, app):
        self.app = app
        self.pilot = app.shorts
        self.rows = {}
        self._items_sig = None
        self.switch = ft.Switch(label='Автопубликация Shorts', value=self.pilot.enabled, on_change=self._toggle)
        self.status_chip = ft.Container()
        self.status_text = muted('')
        self.factory = ft.TextField(label='Папка ShortsFactory', value=str(self.pilot.factory), dense=True, expand=True,
                                    on_blur=self._save_factory)
        self.factory_note = ft.Container()
        self.channels_col = ft.Column(spacing=10)
        self.target = ft.Dropdown(label='Канал', width=260, dense=True, options=[])
        self.text = ft.TextField(label='Текст поста или факт', multiline=True, min_lines=3, max_lines=8, expand=True)
        self.items_col = ft.Column(spacing=6)
        self.view = ft.Column([
            page_header('YouTube Shorts', 'Ролики из постов, фактов и научных тем — по расписанию на каждый канал',
                        actions=[self.switch]),
            card(ft.Row([self.status_chip, self.status_text], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 note('Пока нарезается стрим, Shorts не делаются и не выкладываются: генерация останавливается и ролик ждёт окончания нарезки.', 'info'),
                 ft.Row([self.factory]), self.factory_note,
                 title='Состояние', icon=ft.Icons.MONITOR_HEART_OUTLINED),
            card(self.channels_col, title='Каналы', icon=ft.Icons.SMART_DISPLAY_OUTLINED,
                 subtitle='Каналы привязываются во вкладке YouTube. Здесь — включение, интервал и источник роликов'),
            card(ft.Row([self.target]), self.text,
                 ft.Row([ft.FilledButton('Добавить пост', icon=ft.Icons.ARTICLE_OUTLINED, on_click=lambda e: self._add('post')),
                         ft.OutlinedButton('Добавить факт', icon=ft.Icons.LIGHTBULB_OUTLINE, on_click=lambda e: self._add('fact')),
                         ft.TextButton('Вставить из буфера', icon=ft.Icons.CONTENT_PASTE, on_click=lambda e: app.spawn(self._paste))], spacing=8),
                 ft.Divider(color='#262a31'), self.items_col,
                 title='Очередь роликов', icon=ft.Icons.QUEUE_PLAY_NEXT),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------- refresh ----------
    def refresh(self, snap):
        s = snap.get('shorts')
        if not s:
            return
        self.switch.value = s['enabled']
        st = s['status']
        kind = 'ok' if s['current'] else 'muted' if not s['enabled'] else 'warn' if st.startswith('Пауза') or st.startswith('Ждёт:') else 'error' if st.startswith('Ошибка') or st.startswith('Не найден') else 'ok'
        self.status_chip.content = chip('работает' if kind == 'ok' else 'пауза' if kind == 'warn' else 'ошибка' if kind == 'error' else 'выключено', kind)
        cur = s['current']
        self.status_text.value = st if not cur else f"{cur['profile']}: {cur['stage']}" + (f" — {cur['detail']}" if cur['detail'] else '')
        self.factory_note.content = note(s['factory_problem'], 'error') if s['factory_problem'] else None
        # channels: rebuild only when the set of linked channels changes (keeps fields being edited)
        profiles = [c['profile'] for c in s['channels']]
        if profiles != list(self.rows):
            self.rows = {c['profile']: ChannelRow(self, c) for c in s['channels']}
            self.channels_col.controls = [r.view_ctrl for r in self.rows.values()] or \
                [empty('Нет привязанных каналов. Привяжите канал во вкладке YouTube → «Войти и привязать канал».')]
            self.target.options = [ft.DropdownOption(key=p, text=p) for p in profiles]
            if self.target.value not in profiles:
                self.target.value = profiles[0] if profiles else None
        else:
            for c in s['channels']:
                self.rows[c['profile']].update(c)
        sig = tuple((i['id'], i['status'], i['detail'], i['video_id']) for i in s['items'])
        if sig != self._items_sig:
            self._items_sig = sig
            self.items_col.controls = [self._item_row(i) for i in s['items']] or [empty('Очередь пуста. В режиме «Моменты со стримов» моменты появятся сами после нарезки стрима; иначе — добавьте пост или факт.')]

    def _item_row(self, i):
        label, kind = STATUS.get(i['status'], (i['status'], 'muted'))
        title = i['title'] or (i['text'][:110] + ('…' if len(i['text']) > 110 else '')) or 'Научная тема — будет выбрана автоматически'
        actions = []
        if i['status'] == 'done' and i['video_id']:
            actions.append(ft.IconButton(ft.Icons.OPEN_IN_NEW, tooltip='Открыть на YouTube', url=f"https://youtube.com/shorts/{i['video_id']}"))
        if i['video_path']:
            actions.append(ft.IconButton(ft.Icons.PLAY_CIRCLE_OUTLINE, tooltip='Смотреть видео', on_click=lambda e, p=i['video_path']: self.app.open_path(p)))
        if i['status'] == 'failed':
            actions.append(ft.IconButton(ft.Icons.REPLAY, tooltip='Повторить (проверьте Studio, чтобы не было дубля)', on_click=lambda e, n=i['id']: self._retry(n)))
        if i['status'] != 'working':
            actions.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip='Удалить', icon_color=MUTED, on_click=lambda e, n=i['id']: self._delete(n)))
        lines = [ft.Row([chip(label, kind), chip(KIND.get(i['kind'], i['kind'])), muted(i['profile'], size=12), muted(ts(i['updated_at']), size=12)], spacing=6, wrap=True),
                 ft.Text(title, size=13, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)]
        if i['detail'] and i['status'] != 'done':
            lines.append(muted(i['detail'], size=12))
        return ft.Container(ft.Row([ft.Column(lines, spacing=4, tight=True, expand=True), ft.Row(actions, spacing=0, tight=True)],
                                   vertical_alignment=ft.CrossAxisAlignment.START),
                            bgcolor='#1b1e24', border_radius=10, padding=ft.Padding.symmetric(horizontal=12, vertical=10))

    # ---------- actions ----------
    def _toggle(self, e):
        on = bool(self.switch.value)
        if on and self.pilot.factory_problem():
            self.app.dialogs.toast(self.pilot.factory_problem(), 'error')
        self.pilot.set_enabled(on)
        self.app.refresh_soon()

    def _save_factory(self, e):
        self.pilot.store.set('factory_dir', (self.factory.value or '').strip())
        self.pilot.wake.set()
        self.app.refresh_soon()

    def _add(self, kind):
        text = (self.text.value or '').strip()
        if not self.target.value:
            self.app.dialogs.toast('Сначала привяжите канал во вкладке YouTube', 'warn')
            return
        if len(text) < (20 if kind == 'fact' else 80):
            self.app.dialogs.toast('Слишком короткий текст' + (' для поста — вставьте его целиком' if kind == 'post' else ''), 'warn')
            return
        self.pilot.store.add_item(self.target.value, kind, text)
        self.text.value = ''
        self._items_sig = None
        self.app.dialogs.toast(('Пост' if kind == 'post' else 'Факт') + f' добавлен в очередь {self.target.value}', 'ok')
        self.app.refresh_soon()

    async def _paste(self):
        try:
            self.text.value = await self.app.clipboard.get() or ''
        except Exception:
            self.app.dialogs.toast('Не удалось прочитать буфер обмена', 'warn')
        self.app.page.update()

    def _retry(self, item_id):
        self.pilot.store.retry_item(item_id)
        self._items_sig = None
        self.pilot.wake.set()
        self.app.refresh_soon()

    def _delete(self, item_id):
        self.pilot.store.delete_item(item_id)
        self._items_sig = None
        self.app.refresh_soon()
