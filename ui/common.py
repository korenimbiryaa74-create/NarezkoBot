"""Shared look, small widgets and async dialogs for the Flet control panel."""
from __future__ import annotations
import asyncio
import datetime
import flet as ft

# Dark palette: (background, foreground) per status kind.
KINDS={
    'ok':('#16352a','#6fdc9b'),
    'warn':('#3a2f12','#f3c95b'),
    'error':('#3d1a1d','#ff8a8a'),
    'info':('#17304a','#7cc0ff'),
    'muted':('#2a2d33','#b8bcc6'),
}
CARD_BG='#17191e'
CARD_BORDER='#262a31'
MUTED='#9aa0ab'


def ts(t,fmt='%d.%m %H:%M'):
    try:return datetime.datetime.fromtimestamp(float(t)).strftime(fmt)
    except (TypeError,ValueError,OSError):return ''


def chip(text,kind='muted',icon=None):
    bg,fg=KINDS.get(kind,KINDS['muted'])
    items=[]
    if icon:items.append(ft.Icon(icon,size=14,color=fg))
    items.append(ft.Text(text,size=12,weight=ft.FontWeight.W_600,color=fg,no_wrap=True))
    return ft.Container(ft.Row(items,spacing=4,tight=True),bgcolor=bg,border_radius=20,
                        padding=ft.Padding.symmetric(horizontal=10,vertical=4))


def muted(text,size=13,**kw):
    return ft.Text(text,size=size,color=MUTED,**kw)


def card(*controls,title=None,icon=None,subtitle=None,actions=None,expand=False,padding=18,spacing=12):
    body=[]
    if title:
        head=[]
        if icon:head.append(ft.Icon(icon,size=20,color=ft.Colors.PRIMARY))
        titles=[ft.Text(title,size=16,weight=ft.FontWeight.W_600)]
        if subtitle:titles.append(muted(subtitle,size=12))
        head.append(ft.Column(titles,spacing=2,tight=True,expand=True))
        if actions:head.append(ft.Row(actions,spacing=6,tight=True))
        body.append(ft.Row(head,spacing=10,vertical_alignment=ft.CrossAxisAlignment.CENTER))
    body.extend(controls)
    return ft.Container(ft.Column(body,spacing=spacing,tight=not expand,expand=expand),bgcolor=CARD_BG,border_radius=16,
                        border=ft.Border.all(1,CARD_BORDER),padding=padding,expand=expand)


def page_header(title,subtitle=None,actions=None):
    left=[ft.Text(title,size=26,weight=ft.FontWeight.BOLD)]
    if subtitle:left.append(muted(subtitle,size=14))
    row=[ft.Column(left,spacing=4,tight=True,expand=True)]
    if actions:row.append(ft.Row(actions,spacing=8,tight=True,wrap=True))
    return ft.Row(row,vertical_alignment=ft.CrossAxisAlignment.CENTER)


def note(text,kind='info',icon=ft.Icons.INFO_OUTLINE):
    bg,fg=KINDS.get(kind,KINDS['info'])
    return ft.Container(ft.Row([ft.Icon(icon,size=18,color=fg),ft.Text(text,size=13,color=fg,expand=True)],spacing=10,
                               vertical_alignment=ft.CrossAxisAlignment.START),
                        bgcolor=bg,border_radius=12,padding=ft.Padding.symmetric(horizontal=14,vertical=10))


def steps(items):
    rows=[]
    for n,text in enumerate(items,1):
        rows.append(ft.Row([ft.Container(ft.Text(str(n),size=12,weight=ft.FontWeight.BOLD,color=ft.Colors.ON_PRIMARY),
                                         bgcolor=ft.Colors.PRIMARY,width=22,height=22,border_radius=11,alignment=ft.Alignment.CENTER),
                            ft.Text(text,size=13,expand=True)],spacing=10,vertical_alignment=ft.CrossAxisAlignment.START))
    return ft.Column(rows,spacing=8,tight=True)


def empty(text,icon=ft.Icons.INBOX_OUTLINED):
    return ft.Container(ft.Row([ft.Icon(icon,color=MUTED,size=18),muted(text)],spacing=8,tight=True),
                        padding=ft.Padding.symmetric(vertical=8))


class Dialogs:
    """Await-able confirm/info/prompt dialogs. Call them only from tasks (app.spawn)."""
    def __init__(self,page):
        self.page=page

    async def _show(self,title,body,buttons,icon=None,width=540):
        loop=asyncio.get_running_loop();fut=loop.create_future()
        def answer(value):
            async def handler(_e=None):
                if not fut.done():fut.set_result(value)
                self.page.pop_dialog()
            return handler
        actions=[]
        for label,value,primary in buttons:
            btn=ft.FilledButton(label,on_click=answer(value)) if primary else ft.TextButton(label,on_click=answer(value))
            actions.append(btn)
        dlg=ft.AlertDialog(modal=True,title=ft.Text(title),content=ft.Container(body,width=width),actions=actions,
                           actions_alignment=ft.MainAxisAlignment.END,icon=ft.Icon(icon) if icon else None)
        self.page.show_dialog(dlg)
        return await fut

    async def confirm(self,title,text,yes='Да',no='Отмена'):
        return bool(await self._show(title,ft.Text(text,selectable=True),[(no,False,False),(yes,True,True)],icon=ft.Icons.HELP_OUTLINE))

    async def info(self,title,text):
        await self._show(title,ft.Text(text,selectable=True),[('OK',True,True)],icon=ft.Icons.INFO_OUTLINE)

    async def error(self,title,text):
        await self._show(title,ft.Text(text,selectable=True),[('Закрыть',True,True)],icon=ft.Icons.ERROR_OUTLINE)

    async def prompt(self,title,label,value='',hint=None,ok='Готово'):
        field=ft.TextField(label=label,value=value,autofocus=True,hint_text=hint)
        result=await self._show(title,field,[('Отмена',None,False),(ok,'ok',True)])
        if result!='ok':return None
        return field.value or ''

    def toast(self,text,kind='info'):
        bg,fg=KINDS.get(kind,KINDS['info'])
        self.page.show_dialog(ft.SnackBar(ft.Text(text,color=fg),bgcolor=bg,duration=4000))
