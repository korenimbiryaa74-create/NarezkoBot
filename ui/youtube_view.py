"""YouTube: everything in one place — auto-publish switch, linked channels,
linking new channels and the publication queue."""
from __future__ import annotations
import asyncio
import json
import os
import re
import threading
from pathlib import Path
import flet as ft
from youtube_queue import profile_key
from .common import card,chip,muted,note,page_header,steps,empty,ts,MUTED

FAST=(1,15);SLOW=(15,60)
AUDIENCE=[('not_kids','Не для детей'),('kids','Для детей')]
STATE={'queued':('В очереди','warn'),'uploading':('Загрузка','info'),'confirmed':('Опубликовано','ok'),
       'needs_review':('Нужна проверка','error')}
FILTERS=[('all','Все'),('review','Нужна проверка'),('queued','В очереди'),('done','Опубликованные')]


LIMIT_CHOICES=[0,2,3,4,5,6,8,10,12,15,20]


def limit_dropdown(value=0,on_select=None,keep=False):
    """Clips per stream: 0 = no limit. keep=True adds «как в настройках стримера» (value 'keep')."""
    opts=([ft.DropdownOption(key='keep',text='Как в настройках стримера')] if keep else [])+\
         [ft.DropdownOption(key=str(n),text='Без ограничения' if n==0 else f'Не больше {n}') for n in LIMIT_CHOICES]
    value='keep' if keep else str(value if value in LIMIT_CHOICES else 0)
    dd=ft.Dropdown(label='Нарезок за стрим',width=240,options=opts,value=value)
    if on_select:dd.on_select=on_select
    return dd


def mode_label(r,d,g=False):
    names=[n for n,on in (('реакции',r),('разговоры',d),('игры',g)) if on]
    if r and d and not g:return 'гибрид'
    return ('только ' if len(names)==1 else '')+' + '.join(names)


def _hours_text(h):
    h=int(h);word='час' if h%10==1 and h%100!=11 else ('часа' if h%10 in (2,3,4) and h%100 not in (12,13,14) else 'часов')
    return f'раз в {h} {word}'


class SpeedPicker:
    """Fast: one video every 1-15 hours. Slow: every 15-60 hours."""
    def __init__(self,hours=1,on_change=None):
        self.on_change=on_change
        self.mode=ft.SegmentedButton(segments=[ft.Segment(value='fast',label=ft.Text('Быстро'),icon=ft.Icon(ft.Icons.BOLT)),
                                               ft.Segment(value='slow',label=ft.Text('Медленно'),icon=ft.Icon(ft.Icons.HOURGLASS_BOTTOM))],
                                     selected=['fast'],on_change=self._mode)
        self.slider=ft.Slider(min=1,max=15,divisions=14,value=1,round=0,label='{value} ч',width=300,on_change=self._slide)
        self.text=ft.Text('',size=14,weight=ft.FontWeight.W_600)
        self.control=ft.Column([muted('Скорость публикации',size=12),ft.Row([self.mode,self.text],spacing=14,wrap=True),self.slider],spacing=4,tight=True)
        self.set(hours)

    @property
    def hours(self):
        return int(round(self.slider.value or 1))

    def set(self,hours):
        hours=max(FAST[0],min(SLOW[1],int(hours or 1)))
        self.mode.selected=['fast' if hours<=FAST[1] else 'slow'];self._range(hours)

    def _range(self,hours):
        lo,hi=FAST if (self.mode.selected or ['fast'])[0]=='fast' else SLOW
        self.slider.min,self.slider.max,self.slider.divisions=lo,hi,hi-lo
        self.slider.value=max(lo,min(hi,hours));self.text.value=_hours_text(self.slider.value)

    def _mode(self,_e=None):
        fast=(self.mode.selected or ['fast'])[0]=='fast'
        self._range(min(self.hours,FAST[1]) if fast else max(self.hours,16))
        if self.on_change:self.on_change()

    def _slide(self,_e=None):
        self.text.value=_hours_text(self.hours)
        if self.on_change:self.on_change()


def _audience(value=None):
    return ft.Dropdown(label='Аудитория',value=value,width=230,options=[ft.DropdownOption(key=k,text=t) for k,t in AUDIENCE])


class ProfileCard:
    def __init__(self,view,row):
        self.view=view;self.profile=row['profile']
        self.speed=SpeedPicker(row['interval_hours'],on_change=self._changed)
        self.audience=_audience(row['audience'] or None)
        self.save=ft.FilledButton('Сохранить',icon=ft.Icons.SAVE,visible=False,on_click=lambda e:view.app.spawn(self._save))
        self.audience.on_select=self._changed
        self.channel=ft.Text(row['channel_id'],size=13,color=MUTED,selectable=True)
        title=self.profile+(' · тестовый канал' if self.profile=='__test__' else '')
        # What to look for in this streamer's streams: reactions, discussions or both (hybrid).
        self.cb_r=ft.Checkbox(label='Реакции',on_change=lambda e:view.app.spawn(self._save_mode,'reaction'))
        self.cb_d=ft.Checkbox(label='Разговоры',on_change=lambda e:view.app.spawn(self._save_mode,'discussion'))
        # Games: only when ticked does the ChatGPT request mention games at all (chat + full matches, max 4).
        self.cb_g=ft.Checkbox(label='Игры',tooltip='Целые матчи от начала до конца, не больше 4 за стрим; бот дополнительно скачивает чат VOD',
                              on_change=lambda e:view.app.spawn(self._save_mode,'game'))
        self.kind=ft.Text('',size=12,color=MUTED)
        # Clips per stream for this streamer (0 = no limit, e.g. for short mandatory clips).
        self.limit=limit_dropdown(0,on_select=lambda e:view.app.spawn(self._save_limit))
        self._load_mode()
        if self.profile=='__test__':
            mode_row=muted('Для тестовых видео типы моментов берутся из карточки стримера, указанного для теста.',size=12)
        else:
            mode_row=ft.Row([muted('Искать:',size=12),self.cb_r,self.cb_d,self.cb_g,self.limit,
                             muted('лишнее отбрасывается до рендера',size=12)],
                            spacing=10,wrap=True,vertical_alignment=ft.CrossAxisAlignment.CENTER)
        # Compact header: name + one-line summary; settings are hidden until expanded.
        self.summary=ft.Text('',size=12,color=MUTED,max_lines=1,overflow=ft.TextOverflow.ELLIPSIS)
        self.toggle=ft.IconButton(ft.Icons.EXPAND_MORE,icon_size=20,tooltip='Настройки канала',on_click=self._toggle)
        self.details=ft.Column([
            ft.Row([self.speed.control,self.audience],spacing=24,wrap=True,vertical_alignment=ft.CrossAxisAlignment.START),
            mode_row,
            ft.Row([ft.Icon(ft.Icons.TAG,size=16,color=MUTED),self.channel,
                    ft.IconButton(ft.Icons.CONTENT_COPY,icon_size=16,tooltip='Скопировать ID канала',on_click=lambda e:view.app.spawn(self._copy))],spacing=2),
        ],spacing=8,tight=True,visible=False)
        head=ft.Container(ft.Row([ft.Icon(ft.Icons.SMART_DISPLAY,size=18,color=ft.Colors.PRIMARY),
                                  ft.Text(title,size=14,weight=ft.FontWeight.W_600,no_wrap=True),
                                  ft.Container(self.summary,expand=True)],spacing=10,vertical_alignment=ft.CrossAxisAlignment.CENTER),
                          expand=True,on_click=self._toggle,padding=ft.Padding.symmetric(vertical=6))
        self.control=ft.Container(ft.Column([
            ft.Row([head,self.save,
                    ft.IconButton(ft.Icons.OPEN_IN_NEW,icon_size=18,tooltip='Открыть канал в YouTube Studio',on_click=self._open),
                    ft.IconButton(ft.Icons.LINK,icon_size=18,tooltip='Перепривязать канал',on_click=lambda e:view.open_connect(self.profile,self.current())),
                    self.toggle],spacing=2,vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.details],spacing=4,tight=True),
            bgcolor='#1d2026',border_radius=12,padding=ft.Padding.only(left=12,right=4,top=2,bottom=2))
        self._summary()

    def _toggle(self,_e=None):
        self.details.visible=not self.details.visible
        self.toggle.icon=ft.Icons.EXPAND_LESS if self.details.visible else ft.Icons.EXPAND_MORE
        self.view.app.page.update()

    def _summary(self):
        if not hasattr(self,'summary'):return
        parts=[_hours_text(self.speed.hours)]
        if self.profile!='__test__':
            parts.append(self.kind.value or '')
            try:n=int(self.limit.value or 0)
            except ValueError:n=0
            parts.append(f'до {n} за стрим' if n else 'без лимита')
        aud=dict(AUDIENCE).get(self.audience.value or '')
        if aud:parts.append(aud.lower())
        lang=(self.view.app.config.get('languages') or {}).get(self.profile)
        if isinstance(lang,str) and lang.lower().startswith('en'):parts.append('EN')
        self.summary.value=' · '.join(p for p in parts if p)

    def current(self):
        return {'speed':self.speed.hours,'audience':self.audience.value}

    def _load_mode(self):
        m=(self.view.app.config.get('streamer_modes') or {}).get(self.profile) or {}
        r=bool(m.get('reaction',True));d=bool(m.get('discussion',True));g=bool(m.get('game',False))
        self.cb_r.value=r;self.cb_d.value=d;self.cb_g.value=g
        n=int(m.get('max_clips') or 0)
        if n and not any(o.key==str(n) for o in self.limit.options):self.limit.options.append(ft.DropdownOption(key=str(n),text=f'Не больше {n}'))
        self.limit.value=str(n)
        self.kind.value=mode_label(r,d,g)
        self._summary()

    async def _save_mode(self,changed):
        app=self.view.app;r=bool(self.cb_r.value);d=bool(self.cb_d.value);g=bool(self.cb_g.value)
        if not (r or d or g):
            # at least one kind must stay on
            if changed=='reaction':d=True
            else:r=True
        # Merge: keep the clip limit and other per-streamer fields.
        if app.save_config(lambda c:c.setdefault('streamer_modes',{}).setdefault(self.profile,{}).update({'reaction':r,'discussion':d,'game':g})):
            self._load_mode();app.page.update()
            app.dialogs.toast(f"{self.profile}: {self.kind.value}",'ok')

    async def _save_limit(self):
        app=self.view.app
        try:n=int(self.limit.value or 0)
        except ValueError:n=0
        if app.save_config(lambda c:c.setdefault('streamer_modes',{}).setdefault(self.profile,{}).update({'max_clips':n})):
            self._summary();app.page.update()
            app.dialogs.toast(f"{self.profile}: "+('без ограничения нарезок' if not n else f'не больше {n} нарезок за стрим'),'ok')

    def _changed(self,_e=None):
        self.save.visible=True;self.view.app.page.update()

    def update_row(self,row):
        if self.channel.value!=row['channel_id']:self.channel.value=row['channel_id']
        self._load_mode()
        if not self.save.visible:
            self.speed.set(row['interval_hours']);self.audience.value=row['audience'] or None
            self._summary()

    def _open(self,_e=None):
        if hasattr(os,'startfile'):os.startfile('https://studio.youtube.com/channel/'+self.channel.value)

    async def _copy(self):
        await self.view.app.clipboard.set(self.channel.value);self.view.app.dialogs.toast('ID канала скопирован')

    async def _save(self):
        app=self.view.app;cur=self.current()
        if cur['audience'] not in ('kids','not_kids'):
            await app.dialogs.error('YouTube','Укажите аудиторию видео для этого канала.');return
        old=app.service.youtube_queue.get_profile(self.profile)
        if not old:await app.dialogs.error('YouTube','Канал не найден. Привяжите его заново.');return
        try:app.service.youtube_queue.bind(self.profile,old['channel_id'],int(cur['speed']),audience=cur['audience'])
        except ValueError as exc:await app.dialogs.error('YouTube',str(exc));return
        self.save.visible=False;self._summary();app.dialogs.toast('Сохранено для новых нарезок. Запланированные публикации не меняются.','ok')
        app.refresh_soon()


class YouTubeView:
    def __init__(self,app):
        self.app=app;self.cards={};self.connect=None;self.filter='all';self._uploads_key=None
        self.enabled=ft.Switch(label='Публиковать нарезки на YouTube автоматически',on_change=lambda e:app.spawn(self._toggle))
        self.waiting=ft.Text('',size=13,color='#f3c95b',visible=False)
        # Footer of every video description (the streamer's Twitch links are added above it automatically).
        self.footer=ft.TextField(label='Подпись в описании видео',multiline=True,min_lines=3,max_lines=8,text_size=13,
                                 on_change=lambda e:self._footer_changed())
        self.footer_save=ft.FilledButton('Сохранить подпись',icon=ft.Icons.SAVE,visible=False,on_click=lambda e:app.spawn(self._save_footer))
        self._footer_dirty=False
        self.auto=ft.Switch(label='Загружать реакции сразу, без ручной проверки источника и прав',on_change=lambda e:app.spawn(self._toggle_auto))
        self.profiles=ft.Column(spacing=6)
        self.shown=30
        self.footer_box=ft.Column([self.footer,
            muted('Описание: «🌐 Ссылки на …» — Twitch и соцсети стримера, затем эта подпись.',size=12),
            ft.Row([self.footer_save])],spacing=8,tight=True,visible=False)
        self.footer_btn=ft.TextButton('Подпись в описании',icon=ft.Icons.EDIT_NOTE,on_click=self._toggle_footer)
        # Connect wizard
        self.c_profile=ft.Dropdown(label='Профиль (чей канал)',width=260,on_select=self._profile_changed)
        self.c_custom=ft.TextField(label='Twitch-ник стримера',width=260,visible=False)
        self.c_speed=SpeedPicker(1);self.c_audience=_audience()
        self.c_status=ft.Text('',size=14,weight=ft.FontWeight.W_500)
        self.c_ring=ft.ProgressRing(width=18,height=18,stroke_width=2,visible=False)
        self.c_start=ft.FilledButton('Открыть браузер и войти',icon=ft.Icons.LOGIN,on_click=lambda e:app.spawn(self._start_connect))
        self.c_ready=ft.FilledButton('Канал выбран — подтвердить',icon=ft.Icons.CHECK_CIRCLE,disabled=True,on_click=self._ready)
        self.c_close=ft.TextButton('Скрыть',on_click=self._hide_connect)
        self.connect_card=card(
            muted('1) профиль, скорость, аудитория → 2) «Открыть браузер и войти» → 3) войдите в Google и откройте нужный канал в Studio → 4) «Канал выбран — подтвердить».',size=12),
            ft.Row([self.c_profile,self.c_custom,self.c_audience],spacing=16,wrap=True),
            self.c_speed.control,
            ft.Row([self.c_ring,self.c_status],spacing=10),
            ft.Row([self.c_start,self.c_ready,self.c_close],spacing=10,wrap=True),
            title='Привязка канала',icon=ft.Icons.ADD_LINK)
        self.connect_card.key='yt-connect'
        self.connect_card.visible=False
        self.filters=ft.SegmentedButton(segments=[ft.Segment(value=k,label=ft.Text(t)) for k,t in FILTERS],selected=['all'],
                                        on_change=self._filter_changed)
        self.uploads=ft.Column(spacing=6)
        self.view=ft.Column([
            page_header('YouTube','Каналы, настройки публикации и очередь загрузок'),
            card(ft.Row([self.enabled,self.footer_btn],wrap=True,alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),self.waiting,
                 muted('Первая нарезка — сразу, остальные по расписанию канала. Публикуйте только контент, на который у вас есть права.',size=12),
                 self.footer_box,padding=14,spacing=8),
            card(self.profiles,title='Привязанные каналы',icon=ft.Icons.VIDEO_LIBRARY,subtitle='Нажмите на канал, чтобы открыть настройки. Если вход истёк — перепривяжите.',
                 actions=[ft.FilledButton('Привязать канал',icon=ft.Icons.ADD,on_click=lambda e:self.open_connect())],padding=14,spacing=10),
            self.connect_card,
            card(self.filters,self.uploads,title='Очередь публикаций',icon=ft.Icons.QUEUE_PLAY_NEXT,padding=14,spacing=10,
                 actions=[ft.FilledButton('Выложить нарезки…',icon=ft.Icons.UPLOAD,on_click=lambda e:app.spawn(self._manual_upload)),
                          ft.OutlinedButton('Очистить очередь',icon=ft.Icons.DELETE_SWEEP,on_click=lambda e:app.spawn(self._clear_queue)),
                          ft.TextButton('Удалить всё',icon=ft.Icons.DELETE_FOREVER,on_click=lambda e:app.spawn(self._clear_all))]),
        ],spacing=12,scroll=ft.ScrollMode.AUTO,expand=True)

    # ---------- refresh ----------
    def refresh(self,snap):
        cfg=self.app.config
        self.enabled.value=bool(cfg.get('youtube',{}).get('enabled',False))
        self.auto.value=bool(cfg.get('youtube',{}).get('auto_approve_reactions',False))
        if not self._footer_dirty:self.footer.value=str(cfg.get('youtube',{}).get('description_footer') or '')
        self.waiting.value=snap.get('yt_waiting') or '';self.waiting.visible=bool(self.waiting.value)
        rows=snap['profiles'];seen=set()
        for row in rows:
            seen.add(row['profile'])
            if row['profile'] in self.cards:self.cards[row['profile']].update_row(row)
            else:self.cards[row['profile']]=ProfileCard(self,row)
        for name in list(self.cards):
            if name not in seen:del self.cards[name]
        controls=[self.cards[r['profile']].control for r in rows] or [empty('Каналы ещё не привязаны. Нажмите «Привязать канал».',ft.Icons.LINK_OFF)]
        if [id(c) for c in self.profiles.controls]!=[id(c) for c in controls]:self.profiles.controls=controls
        self._refresh_connect()
        self._refresh_uploads(snap['uploads'])

    def _visible_uploads(self,uploads):
        rows=[r for r in uploads if r['state']!='cancelled']
        recent=rows[-200:];ids={r['id'] for r in recent}
        rows=[r for r in rows if r['state']=='needs_review' and r['id'] not in ids]+recent
        f=self.filter
        if f=='review':rows=[r for r in rows if r['state']=='needs_review']
        elif f=='queued':rows=[r for r in rows if r['state'] in ('queued','uploading')]
        elif f=='done':rows=[r for r in rows if r['state']=='confirmed']
        rows.sort(key=lambda r:(r['state']!='needs_review',-r['id']))
        return rows

    def _refresh_uploads(self,uploads):
        rows=self._visible_uploads(uploads);total=len(rows);rows=rows[:self.shown]
        key=(self.filter,self.shown,total,tuple((r['id'],r['state'],r.get('video_id'),r.get('detail'),r.get('publish_at'),r.get('tracking',1)) for r in rows))
        if key==self._uploads_key:return
        self._uploads_key=key
        self.uploads.controls=[self._upload_row(r) for r in rows] or [empty('Публикаций нет')]
        if total>len(rows):
            self.uploads.controls.append(ft.TextButton(f'Показать ещё ({total-len(rows)})',icon=ft.Icons.EXPAND_MORE,on_click=self._more))

    def _more(self,_e=None):
        self.shown+=30;self._uploads_key=None;self.app.refresh_soon()

    def _toggle_footer(self,_e=None):
        self.footer_box.visible=not self.footer_box.visible or self._footer_dirty
        self.app.page.update()

    def _upload_row(self,r):
        label,kind=STATE.get(r['state'],(r['state'],'muted'))
        if r['state']=='uploading' and not r.get('tracking',1):label,kind='Статус не подтверждён','warn'
        when='Сразу' if not r['publish_at'] else ts(r['publish_at'],'%d.%m.%Y %H:%M')
        link=('https://youtu.be/'+r['video_id']) if r.get('video_id') else ''
        meta=f"№{r['id']} · {r['profile']} · публикация: {when}"
        actions=[]
        if r['state']=='needs_review':
            actions+=[ft.TextButton('Повторить',icon=ft.Icons.REPLAY,on_click=lambda e,r=r:self.app.spawn(self._retry,r)),
                      ft.TextButton('Уже загружено',icon=ft.Icons.TASK_ALT,on_click=lambda e,r=r:self.app.spawn(self._confirm_existing,r))]
        if r['state'] in ('queued','needs_review'):
            actions.append(ft.TextButton('Отменить',icon=ft.Icons.CLOSE,on_click=lambda e,r=r:self.app.spawn(self._cancel,r)))
        if link:
            actions+=[ft.IconButton(ft.Icons.OPEN_IN_NEW,tooltip='Открыть видео',on_click=lambda e,l=link:os.startfile(l) if hasattr(os,'startfile') else None),
                      ft.IconButton(ft.Icons.CONTENT_COPY,tooltip='Скопировать ссылку',on_click=lambda e,l=link:self.app.spawn(self._copy,l))]
        detail=link or (r.get('detail') or '')
        texts=[ft.Text(r['title'] or 'Без названия',size=13,weight=ft.FontWeight.W_600,max_lines=1,overflow=ft.TextOverflow.ELLIPSIS),
               muted(meta+((' · '+link) if link else ''),size=12,max_lines=1,overflow=ft.TextOverflow.ELLIPSIS)]
        if detail and not link:texts.append(ft.Text(detail,size=12,color='#f3c95b',selectable=True,max_lines=2,overflow=ft.TextOverflow.ELLIPSIS))
        return ft.Container(ft.Row([ft.Column(texts,spacing=3,expand=True,tight=True),chip(label,kind),ft.Row(actions,spacing=0,tight=True)],
                                   spacing=12,vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            bgcolor='#1d2026',border_radius=10,padding=ft.Padding.symmetric(horizontal=12,vertical=4))

    def _filter_changed(self,e):
        self.filter=(self.filters.selected or ['all'])[0];self.shown=30;self._uploads_key=None;self.app.refresh_soon()

    # ---------- description footer ----------
    def _footer_changed(self):
        self._footer_dirty=True;self.footer_save.visible=True;self.app.page.update()

    async def _save_footer(self):
        value=(self.footer.value or '').replace('\r\n','\n')
        if self.app.save_config(lambda c:c.setdefault('youtube',{}).update(description_footer=value)):
            self._footer_dirty=False;self.footer_save.visible=False;self.app.page.update()
            self.app.dialogs.toast('Подпись сохранена для новых публикаций','ok')

    def description_for(self,job,profile):
        import youtube_descriptions as yd
        cfg=self.app.config
        return yd.build(self.app.root,yd.login_for(job,profile,cfg),cfg.get('youtube',{}).get('description_footer'))

    # ---------- publish switch ----------
    async def _toggle(self):
        value=bool(self.enabled.value)
        if value and not await self.app.dialogs.confirm('YouTube','Включить автоматическую публичную загрузку нарезок на привязанные каналы?','Включить'):
            self.enabled.value=False;self.app.page.update();return
        if self.app.save_config(lambda c:c.setdefault('youtube',{}).update(enabled=value)):
            self.app.dialogs.toast('Публикация на YouTube '+('включена' if value else 'выключена'),'ok' if value else 'info')

    async def _toggle_auto(self):
        from publication_guard import REVIEW
        app=self.app;value=bool(self.auto.value)
        if value and not await app.dialogs.confirm('YouTube: реакции без проверки',
                'Реакции будут загружаться сразу, без вашей проверки источника и прав. Ответственность за публикацию чужих видео — на вас: '
                'возможны жалобы правообладателей и страйки. Передачи из списка «не публиковать» по-прежнему блокируются. '
                'Ожидающие проверки реакции будут сразу поставлены в очередь. Включить?','Включить'):
            self.auto.value=False;app.page.update();return
        if not app.save_config(lambda c:c.setdefault('youtube',{}).update(auto_approve_reactions=value)):return
        moved=0
        if value:
            for r in app.service.youtube_queue.list_uploads():
                if r['state']=='needs_review' and (r.get('detail') or '').startswith(REVIEW):
                    try:app.service.youtube_queue.retry_reviewed(int(r['id']));moved+=1
                    except ValueError:pass
            app.service.resume_youtube_after_review()
            app.service.log(f'YouTube: реакции публикуются без ручной проверки; возвращено в очередь: {moved}')
        app.dialogs.toast(('Включено. Возвращено в очередь: '+str(moved)) if value else 'Выключено: реакции снова ждут вашей проверки','ok' if value else 'info')
        app.refresh_soon()

    # ---------- connect wizard ----------
    def _profiles_options(self,selected=None):
        names=[n.lower() for n in self.app.config.get('streamers',[])]
        for p in self.cards:
            if p!='__test__' and p not in names:names.append(p)
        if selected and selected not in names and selected!='__test__':names.append(selected)
        opts=[ft.DropdownOption(key=n,text=n) for n in names]
        opts+= [ft.DropdownOption(key='__test__',text='Тестовый канал (__test__)'),ft.DropdownOption(key='__other__',text='Другой стример…')]
        return opts

    def open_connect(self,profile=None,current=None):
        if self.connect and self.connect.get('busy'):self.connect_card.visible=True;self.app.page.update();return
        self.c_profile.options=self._profiles_options(profile)
        self.c_profile.value=profile or (self.c_profile.options[0].key if self.c_profile.options else None)
        self.c_custom.visible=self.c_profile.value=='__other__'
        if current:self.c_speed.set(current['speed']);self.c_audience.value=current.get('audience')
        self.connect={'busy':False};self.c_status.value='';self.c_status.color=None
        self.connect_card.visible=True;self.app.page.update()
        # Окно привязки стоит под списком каналов — без прокрутки оно открывается за пределами экрана.
        self.app.spawn(self._scroll_to_connect)

    async def _scroll_to_connect(self):
        try:
            import asyncio
            await asyncio.sleep(0.15)
            await self.view.scroll_to(scroll_key='yt-connect',duration=400)
        except Exception as exc:  # noqa: BLE001 — прокрутка не должна ломать привязку
            print('scroll to connect failed:',exc)

    def _profile_changed(self,_e=None):
        self.c_custom.visible=self.c_profile.value=='__other__';self.app.page.update()

    def _hide_connect(self,_e=None):
        if self.connect and self.connect.get('busy'):
            self.app.dialogs.toast('Подключение ещё идёт — закончите его в браузере.','warn');return
        self.connect_card.visible=False;self.connect=None;self.app.page.update()

    def _ready(self,_e=None):
        c=self.connect
        if not c or not c.get('busy'):return
        if not c['opened'].is_set():self.app.dialogs.toast('Дождитесь открытия окна браузера.','warn');return
        c['ready'].set();self._refresh_connect();self.app.page.update()

    async def _start_connect(self):
        app=self.app
        if self.connect and self.connect.get('busy'):return
        raw=self.c_custom.value if self.c_profile.value=='__other__' else self.c_profile.value
        try:profile=profile_key(raw or '')
        except Exception as exc:await app.dialogs.error('YouTube',str(exc) or 'Укажите профиль');return
        audience=self.c_audience.value
        if audience not in ('kids','not_kids'):await app.dialogs.error('YouTube','Укажите аудиторию видео для этого канала.');return
        interval=self.c_speed.hours
        if not await app.dialogs.confirm('YouTube',f'Откроется отдельное окно браузера для профиля «{profile}». Войдите сами, выберите нужный канал '
                                         'в YouTube Studio и вернитесь сюда, чтобы нажать «Канал выбран — подтвердить». Продолжить?','Открыть браузер'):return
        ready=threading.Event();opened=threading.Event()
        self.connect={'busy':True,'ready':ready,'opened':opened,'profile':profile}
        def work():
            try:
                channel=app.service.youtube_studio.connect(profile,ready,opened)
                app.service.youtube_queue.bind(profile,channel,interval,audience=audience)
                msg=f'Профиль {profile} привязан к каналу {channel}. Проверьте, что это нужный канал.';ok=True
                app.service.log('YouTube: '+msg)
            except Exception as exc:
                msg=str(exc)[:400] or type(exc).__name__;ok=False
                app.service.log('YouTube: не удалось привязать канал ('+type(exc).__name__+')')
            app.post('yt_connect_done',(ok,msg))
        threading.Thread(target=work,daemon=True).start()
        self._refresh_connect()

    def _refresh_connect(self):
        c=self.connect
        busy=bool(c and c.get('busy'))
        self.c_start.disabled=busy;self.c_ring.visible=busy
        for ctl in (self.c_profile,self.c_custom,self.c_speed.control,self.c_audience):ctl.disabled=busy
        if not busy:self.c_ready.disabled=True;return
        if not c['opened'].is_set():self.c_status.value='Открываю окно браузера…';self.c_ready.disabled=True
        elif not c['ready'].is_set():self.c_status.value='Войдите в Google, откройте нужный канал в YouTube Studio и нажмите «Канал выбран — подтвердить».';self.c_ready.disabled=False
        else:self.c_status.value='Проверяю выбранный канал…';self.c_ready.disabled=True
        self.c_status.color=None

    async def on_event(self,kind,payload):
        if kind!='yt_connect_done':return
        ok,msg=payload
        profile=str((self.connect or {}).get('profile') or '').strip()
        self.connect={'busy':False};self._refresh_connect()
        if ok and profile and profile!='__test__' and re.fullmatch(r'[A-Za-z0-9_]{2,25}',profile):
            try:self.app.add_streamer(profile)
            except Exception:pass
        self.c_status.value=msg;self.c_status.color='#6fdc9b' if ok else '#ff8a8a'
        self.app.refresh_soon()
        await (self.app.dialogs.info if ok else self.app.dialogs.error)('YouTube',msg)

    # ---------- queue actions ----------
    async def _copy(self,text):
        await self.app.clipboard.set(text);self.app.dialogs.toast('Ссылка скопирована')

    async def _retry(self,r):
        from publication_guard import BLOCK,REVIEW
        app=self.app
        if (r.get('detail') or '').startswith(BLOCK):await app.dialogs.error('YouTube',r['detail']);return
        if (r.get('detail') or '').startswith(REVIEW):
            text=('Вы проверили исходный ролик и права, убедились, что эта нарезка не относится к запрещённым передачам, '
                  'и разрешаете её загрузить на YouTube? Решение остаётся за вами. Если источник неизвестен — нажмите «Отмена».')
        else:
            text=('Проверьте YouTube Studio на выбранном канале. Если видео или черновик уже создан, удалите его или не повторяйте загрузку. '
                  'Вы подтверждаете, что видео и черновика на канале нет и повтор не создаст дубль?')
        if not await app.dialogs.confirm('YouTube: проверка перед повтором',text,'Повторить'):return
        try:
            app.service.youtube_queue.retry_reviewed(int(r['id']));app.service.resume_youtube_after_review()
            app.service.log('YouTube: пользователь разрешил повтор после ручной проверки');app.refresh_soon()
        except ValueError as exc:await app.dialogs.error('YouTube',str(exc))

    async def _cancel(self,r):
        app=self.app
        text=('Отменить загрузку №'+str(r['id'])+'? Это не удалит видео или черновик на YouTube. '
              'Проверьте Studio и убедитесь, что загрузка этого видео не продолжается в Chrome. Другие записи очереди не отменятся.')
        if not await app.dialogs.confirm('YouTube: отмена загрузки',text,'Отменить загрузку'):return
        try:
            app.service.youtube_queue.cancel_reviewed_or_queued(r['id']);app.service.resume_youtube_after_review(r['profile'])
            app.service.log('YouTube: отменена запись №'+str(r['id'])+' после проверки пользователем');app.refresh_soon()
        except ValueError as exc:await app.dialogs.error('YouTube',str(exc))

    async def _confirm_existing(self,r):
        from youtube_studio import video_from_url
        app=self.app
        text=('В YouTube Studio проверьте именно канал '+r['channel_id']+', видео «'+(r['title'] or '')+'», '
              'завершение загрузки и обработки, публичный доступ или точную отложенную дату и время. '
              'После проверки закройте оставшееся окно браузера этого профиля. Видео сохранено и настройки верны?')
        if not await app.dialogs.confirm('YouTube: подтверждение',text,'Да, всё верно'):return
        value=await app.dialogs.prompt('YouTube','Ссылка на подтверждённое видео',hint='https://youtu.be/…')
        if value is None:return
        video_id=video_from_url(value.strip())
        if not video_id:await app.dialogs.error('YouTube','Не удалось прочитать ссылку на видео.');return
        try:
            app.service.youtube_queue.confirm_reviewed(r['id'],video_id);app.service.resume_youtube_after_review(r['profile'])
            app.service.log('YouTube: пользователь подтвердил видео '+video_id);app.refresh_soon()
        except ValueError as exc:await app.dialogs.error('YouTube',str(exc))

    # ---------- manual publish of a finished job ----------
    def _job_folders(self):
        out=[]
        # Hidden jobs too: clearing the history must not stop re-publishing finished clips.
        for j in self.app.service.store.list_jobs(300,include_hidden=True):
            if j['state']!='done':continue
            cands=[j.get('output_dir') or '']
            m=re.search(r';\s*([A-Za-z]:\\.+)$',(j.get('detail') or '').strip())
            if m:cands.append(m.group(1).strip())
            for c in cands:
                if not c:continue
                p=Path(c)
                if not p.is_dir():continue
                for sub in (p/'Готовые без тишины',p/'Готовые нарезки',p):
                    files=sorted(x for x in sub.glob('*.mp4') if x.is_file()) if sub.is_dir() else []
                    if files:out.append((j,sub,files));break
                break
        return out

    @staticmethod
    def _titles(folder,files):
        job=folder.parent if folder.name in ('Готовые без тишины','Готовые нарезки') else folder
        titles=[]
        for f in files:
            t=None
            try:t=(json.loads((job/(f.stem+'.pipeline.json')).read_text(encoding='utf-8-sig')).get('reaction') or {}).get('video_title')
            except (OSError,ValueError,AttributeError):pass
            try:t=json.loads(f.with_name(f.stem+' — источник.json').read_text(encoding='utf-8-sig')).get('video_title') or t
            except (OSError,ValueError,AttributeError):pass
            t=re.sub(r'[<>]','',str(t or re.sub(r'^\d+\s+','',f.stem))).strip()[:100].strip()
            titles.append(t or f.stem[:100])
        return titles

    async def _manual_upload(self):
        app=self.app;q=app.service.youtube_queue
        items=await asyncio.to_thread(self._job_folders)
        if not items:await app.dialogs.info('Выложить нарезки','Нет завершённых заданий с готовыми MP4.');return
        profiles=q.list_profiles()
        if not profiles:await app.dialogs.error('Выложить нарезки','Сначала привяжите канал YouTube.');return
        name=lambda j:('Тест: '+j['local_name']) if j.get('local_name') else j['streamer']
        jobs=ft.Dropdown(label='Задание',width=600,value=str(items[0][0]['id']),
                         options=[ft.DropdownOption(key=str(j['id']),text=f"№{j['id']} · {name(j)[:60]} · нарезок: {len(fs)}"+(' · из истории' if j.get('hidden') else '')) for j,_,fs in items])
        keys=[p['profile'] for p in profiles]
        def guess(j):
            s=(j.get('streamer') or '').lower()
            return s if s in keys else ('__test__' if '__test__' in keys else keys[0])
        channel=ft.Dropdown(label='Канал',width=300,value=guess(items[0][0]),
                            options=[ft.DropdownOption(key=k,text=k+(' (тестовый)' if k=='__test__' else '')) for k in keys])
        def pick(_e=None):
            j=next((x[0] for x in items if str(x[0]['id'])==jobs.value),None)
            if j:channel.value=guess(j);app.page.update()
        jobs.on_select=pick
        body=ft.Column([jobs,channel,muted('Первая нарезка публикуется сразу, остальные — отложенно с интервалом канала. '
                                           'Если рядом с нарезкой есть «— ИИ-превью», оно станет значком видео.',size=12)],tight=True,spacing=14)
        if await app.dialogs._show('Выложить нарезки',body,[('Отмена',None,False),('Выложить','ok',True)],icon=ft.Icons.UPLOAD,width=640)!='ok':return
        j,folder,files=next(x for x in items if str(x[0]['id'])==jobs.value)
        existing=[r for r in q.job_rows(j['id']) if r['state']!='cancelled']
        if existing and not await app.dialogs.confirm('Выложить нарезки',f"У задания №{j['id']} уже есть записи публикаций: {len(existing)}. "
                                                      'Заменить их новыми? Уже загруженные в Studio видео останутся там — возможны дубли.','Заменить'):return
        titles=self._titles(folder,files)
        description=await asyncio.to_thread(self.description_for,j,channel.value)
        try:count=q.enqueue(j['id'],j['streamer'] or 'test',files,titles=titles,profile=channel.value,replace=True,descriptions=description)
        except (ValueError,OSError) as exc:await app.dialogs.error('Выложить нарезки',str(exc));return
        if not app.config.get('youtube',{}).get('enabled'):
            if await app.dialogs.confirm('YouTube','Публикация на YouTube выключена. Включить, чтобы бот начал загрузку?','Включить'):
                app.save_config(lambda c:c.setdefault('youtube',{}).update(enabled=True))
        app.service.resume_youtube_after_review()
        app.service.log(f"YouTube: задание №{j['id']} поставлено на публикацию вручную ({count}) на канал {channel.value}")
        app.dialogs.toast(f'В очереди: {count}. Загрузка начнётся автоматически.','ok');self._uploads_key=None;app.refresh_soon()

    async def _clear_all(self):
        app=self.app
        if not await app.dialogs.confirm('Удалить всё','Удалить ВСЕ записи публикаций — очередь и историю, для всех каналов? '
                                         'Видео, уже загруженные в YouTube Studio, останутся там. Загрузки, которые сейчас идут в Chrome, '
                                         'не остановятся, но бот перестанет их отслеживать.','Удалить всё'):return
        try:
            count=app.service.youtube_queue.clear_all();app.service.resume_youtube_after_review()
            app.service.log(f'YouTube: удалены все записи публикаций ({count})')
            app.dialogs.toast(f'Удалено записей: {count}','ok');self._uploads_key=None;app.refresh_soon()
        except Exception as exc:await app.dialogs.error('YouTube','Не удалось очистить: '+type(exc).__name__)

    async def _clear_queue(self):
        app=self.app
        text=('Отменить все ожидающие и требующие проверки загрузки для ВСЕХ профилей, включая тестовый? '
              'Загрузка, которая уже идёт, продолжится; новые нарезки могут снова попасть в очередь. '
              'Видео и черновики в YouTube Studio не удаляются.')
        if not await app.dialogs.confirm('YouTube: очистить очередь',text,'Очистить'):return
        try:
            count=app.service.youtube_queue.clear_pending_uploads();app.service.resume_youtube_after_review()
            app.service.log(f'YouTube: очередь очищена, отменено загрузок {count}')
            app.dialogs.toast(f'Отменено загрузок: {count}. Текущая загрузка продолжится.','ok');app.refresh_soon()
        except Exception as exc:await app.dialogs.error('YouTube','Не удалось очистить очередь: '+type(exc).__name__)
