"""Draw fixed per-streamer blur rectangles on a video frame (Flet dialog)."""
from __future__ import annotations
import asyncio
import flet as ft
import banner_blur
import manual_blur

MAX_W,MAX_H=960,540


class BlurPicker:
    def __init__(self,app,login,image,on_saved=None):
        self.app=app;self.login=login;self.on_saved=on_saved
        cv2,_=banner_blur._cv()
        h,w=image.shape[:2];scale=min(MAX_W/w,MAX_H/h,4.0)
        self.vw=max(1,round(w*scale));self.vh=max(1,round(h*scale))
        ok,buf=cv2.imencode('.png',cv2.resize(image,(self.vw,self.vh),interpolation=cv2.INTER_LINEAR))
        if not ok:raise ValueError('Не удалось показать кадр')
        self.boxes=[];self.start=None;self.last=None
        self.pending=ft.Container(border=ft.Border.all(2,'#ffd54f'),left=0,top=0,width=0,height=0,visible=False)
        self.layer=ft.Stack([ft.Image(src=buf.tobytes(),width=self.vw,height=self.vh,fit=ft.BoxFit.FILL),self.pending],width=self.vw,height=self.vh)
        self.counter=ft.Text('',size=13)
        existing=manual_blur.load_profile(app.root,login) if manual_blur.profile_path(app.root,login).exists() else {'boxes':[]}
        for box in existing['boxes']:self._add(box)
        self.detector=ft.GestureDetector(content=self.layer,mouse_cursor=ft.MouseCursor.PRECISE,drag_interval=16,
                                         on_pan_start=self._start,on_pan_update=self._update,on_pan_end=self._end)
        self.fut=None

    def _rect(self,box):
        x0,y0,x1,y1=box
        return ft.Container(border=ft.Border.all(3,'#ff5252'),bgcolor='#33ff5252',left=x0*self.vw/1000,top=y0*self.vh/1000,
                            width=(x1-x0)*self.vw/1000,height=(y1-y0)*self.vh/1000)

    def _add(self,box):
        rect=self._rect(box);self.boxes.append((box,rect));self.layer.controls.insert(len(self.layer.controls)-1,rect)
        self.counter.value=f'Областей: {len(self.boxes)} из 16'

    def _clamp(self,pos):
        return max(0,min(self.vw,pos.x)),max(0,min(self.vh,pos.y))

    def _start(self,e):
        self.start=self._clamp(e.local_position);self.last=self.start
        self.pending.left,self.pending.top=self.start;self.pending.width=self.pending.height=0;self.pending.visible=True;self.pending.update()

    def _update(self,e):
        if self.start is None:return
        self.last=self._clamp(e.local_position)
        x0,x1=sorted((self.start[0],self.last[0]));y0,y1=sorted((self.start[1],self.last[1]))
        self.pending.left,self.pending.top,self.pending.width,self.pending.height=x0,y0,x1-x0,y1-y0;self.pending.update()

    def _end(self,_e):
        if self.start is None:return
        (sx,sy),(ex,ey)=self.start,self.last or self.start;self.start=None
        self.pending.visible=False
        x0,x1=sorted((sx,ex));y0,y1=sorted((sy,ey))
        box=manual_blur.display_box(x0,y0,x1,y1,self.vw,self.vh)
        if box[2]-box[0]<4 or box[3]-box[1]<4:self.app.dialogs.toast('Слишком маленькая область','warn')
        elif len(self.boxes)>=16:self.app.dialogs.toast('Максимум 16 областей','warn')
        else:self._add(box)
        self.app.page.update()

    def _undo(self,_e=None):
        if self.boxes:
            _,rect=self.boxes.pop();self.layer.controls.remove(rect);self.counter.value=f'Областей: {len(self.boxes)} из 16';self.app.page.update()

    def _finish(self,value):
        async def handler(_e=None):
            if value=='save' and not self.boxes:
                self.app.dialogs.toast('Выделите области или нажмите «Без блюра»','warn');return
            if not self.fut.done():self.fut.set_result(value)
            self.app.page.pop_dialog()
        return handler

    async def run(self):
        self.fut=asyncio.get_running_loop().create_future()
        dlg=ft.AlertDialog(modal=True,title=ft.Text('Блюр: '+self.login),
            content=ft.Column([ft.Text('Выделите мышью все области (баннеры, вебка с лишним и т. п.). Они будут размыты на протяжении всей нарезки.',size=13),
                               ft.Row([ft.OutlinedButton('Убрать последнюю',icon=ft.Icons.UNDO,on_click=self._undo),self.counter],spacing=12),
                               ft.Container(self.detector,border=ft.Border.all(1,'#333'))],tight=True,spacing=10,scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton('Отмена',on_click=self._finish('cancel')),ft.TextButton('Без блюра',on_click=self._finish('none')),
                     ft.FilledButton('Сохранить области',icon=ft.Icons.SAVE,on_click=self._finish('save'))])
        self.app.page.show_dialog(dlg)
        result=await self.fut
        if result=='cancel':return False
        if result=='none' and not await self.app.dialogs.confirm('Блюр',f'Сохранить {self.login} без блюра?'):return False
        try:manual_blur.save_profile(self.app.root,self.login,[] if result=='none' else [b for b,_ in self.boxes])
        except Exception as exc:await self.app.dialogs.error('Блюр',str(exc));return False
        if self.on_saved:self.on_saved()
        self.app.dialogs.toast('Настройки блюра сохранены для '+self.login,'ok')
        return True
