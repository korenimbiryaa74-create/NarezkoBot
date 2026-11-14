"""Блок «Очистка диска» во вкладке «Настройки»."""
from __future__ import annotations

import asyncio

import flet as ft

import cleanup
from .common import card, muted, note


class CleanupCard:
    def __init__(self, app):
        self.app = app
        self.cats = []
        self.boxes = {}
        self.list = ft.Column(spacing=8)
        self.total = muted('')
        self.scan_btn = ft.OutlinedButton('Найти, что можно удалить', icon=ft.Icons.SEARCH, on_click=lambda e: app.spawn(self._scan))
        self.del_btn = ft.FilledButton('Удалить выбранное', icon=ft.Icons.DELETE_SWEEP, disabled=True, on_click=lambda e: app.spawn(self._delete))
        self.view = card(
            note('Удаляется только то, что боту больше не нужно. Стримы в работе, нарезки в очереди загрузки и на проверке '
                 'не трогаются никогда. Удаление безвозвратное, мимо корзины.', 'info'),
            self.list,
            ft.Row([self.scan_btn, self.del_btn, self.total], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            title='Очистка диска', icon=ft.Icons.CLEANING_SERVICES)

    def _sum(self):
        total = sum(c.size for c in self.cats if self.boxes.get(c.key) and self.boxes[c.key].value)
        self.total.value = f'Выбрано: {cleanup.fmt(total)}' if total else ''
        self.del_btn.disabled = not total
        self.app.page.update()

    async def _scan(self):
        self.scan_btn.disabled = True
        self.total.value = 'Считаю…'
        self.app.page.update()
        try:
            self.cats = await asyncio.to_thread(cleanup.scan, self.app.root)
        except Exception as exc:  # noqa: BLE001
            self.app.dialogs.toast(f'Не удалось посчитать: {exc}', 'error')
            self.cats = []
        self.boxes, rows = {}, []
        for c in self.cats:
            box = ft.Checkbox(label=f'{c.title} — {cleanup.fmt(c.size)} ({len(c.paths)} шт.)' if c.paths else f'{c.title} — нечего удалять',
                              value=c.default and bool(c.paths), disabled=not c.paths, on_change=lambda e: self._sum())
            self.boxes[c.key] = box
            rows.append(ft.Column([box, ft.Container(muted(c.hint, size=12), padding=ft.Padding.only(left=34))], spacing=0, tight=True))
        self.list.controls = rows
        self.scan_btn.disabled = False
        self._sum()

    async def _delete(self):
        keys = [k for k, b in self.boxes.items() if b.value]
        chosen = [c for c in self.cats if c.key in keys]
        if not chosen:
            return
        text = '\n'.join(f'• {c.title} — {cleanup.fmt(c.size)}' for c in chosen)
        if not await self.app.dialogs.confirm('Очистка диска', f'Удалить безвозвратно:\n{text}\n\nВсего {cleanup.fmt(sum(c.size for c in chosen))}.', yes='Удалить'):
            return
        self.del_btn.disabled = self.scan_btn.disabled = True
        self.total.value = 'Удаляю…'
        self.app.page.update()
        freed, count, errors = await asyncio.to_thread(cleanup.clean, self.app.root, keys)
        try:
            self.app.service.log(f'Очистка диска: освобождено {cleanup.fmt(freed)}, удалено объектов: {count}' + (f', не удалось: {len(errors)}' if errors else ''))
        except Exception:  # noqa: BLE001
            pass
        if errors:
            await self.app.dialogs.error('Очистка диска', f'Освобождено {cleanup.fmt(freed)}. Не удалось удалить (файл занят?):\n' + '\n'.join(errors[:15]))
        else:
            self.app.dialogs.toast(f'Освобождено {cleanup.fmt(freed)}', 'ok')
        await self._scan()
