"""Draw fixed per-streamer blur rectangles on a video frame."""
import base64
import tkinter as tk
from tkinter import ttk, messagebox
import banner_blur
import manual_blur

class ManualBlurPicker(tk.Toplevel):
    def __init__(self,master,root,login,image,on_saved=None):
        super().__init__(master);self.title('Блюр: '+login)
        self.root,self.login,self.on_saved=root,login,on_saved
        self.boxes=[];self.pending=None;self.start=None;self.image=image
        h,w=image.shape[:2];sw=max(400,self.winfo_screenwidth()-100);sh=max(240,self.winfo_screenheight()-250)
        self.scale=min(sw/w,sh/h,4.0);self.vw=max(1,round(w*self.scale));self.vh=max(1,round(h*self.scale))
        cv2,_=banner_blur._cv();view=cv2.resize(image,(self.vw,self.vh),interpolation=cv2.INTER_LINEAR)
        ok,buf=cv2.imencode('.png',view)
        if not ok:raise ValueError('Не удалось показать кадр')
        self.photo=tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode('ascii'))
        ttk.Label(self,text='Выделите мышью все области. Они будут размыты на протяжении всей нарезки, даже когда баннер скрыт.',wraplength=1000).pack(padx=8,pady=6)
        bar=ttk.Frame(self);bar.pack(fill='x',padx=8)
        ttk.Button(bar,text='Убрать последнее',command=self.undo).pack(side='left')
        ttk.Button(bar,text='Сохранить области',command=self.save).pack(side='left',padx=6)
        ttk.Button(bar,text='Без блюра',command=self.no_blur).pack(side='left')
        ttk.Button(bar,text='Отмена',command=self.destroy).pack(side='left',padx=6)
        self.canvas=tk.Canvas(self,width=self.vw,height=self.vh,background='black',highlightthickness=0,cursor='crosshair');self.canvas.pack(padx=8,pady=8)
        self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self.canvas.bind('<ButtonPress-1>',self.press);self.canvas.bind('<B1-Motion>',self.drag);self.canvas.bind('<ButtonRelease-1>',self.release)
        existing=manual_blur.load_profile(root,login) if manual_blur.profile_path(root,login).exists() else {'boxes':[]}
        for box in existing['boxes']:self.add(box)
    def xy(self,event):return max(0,min(self.vw,event.x)),max(0,min(self.vh,event.y))
    def press(self,event):
        self.start=self.xy(event);self.pending=self.canvas.create_rectangle(*self.start,*self.start,outline='red',width=3)
    def drag(self,event):
        if self.start is not None:self.canvas.coords(self.pending,*self.start,*self.xy(event))
    def release(self,event):
        if self.start is None:return
        x,y=self.xy(event);x0,x1=sorted((self.start[0],x));y0,y1=sorted((self.start[1],y))
        self.canvas.delete(self.pending);self.start=None;self.pending=None
        box=manual_blur.display_box(x0,y0,x1,y1,self.vw,self.vh)
        if box[2]-box[0]<4 or box[3]-box[1]<4:
            messagebox.showwarning('Блюр','Слишком маленькая область',parent=self);return
        if len(self.boxes)>=16:
            messagebox.showwarning('Блюр','Максимум 16 областей',parent=self);return
        self.add(box)
    def add(self,box):
        self.boxes.append((box,self.canvas.create_rectangle(box[0]*self.vw/1000,box[1]*self.vh/1000,box[2]*self.vw/1000,box[3]*self.vh/1000,outline='red',width=3)))
    def undo(self):
        if self.boxes:self.canvas.delete(self.boxes.pop()[1])
    def save(self):
        if not self.boxes:messagebox.showwarning('Блюр','Выделите области или нажмите «Без блюра»',parent=self);return
        self.commit([b for b,_ in self.boxes])
    def no_blur(self):
        if messagebox.askyesno('Блюр',f'Сохранить {self.login} без блюра?',parent=self):self.commit([])
    def commit(self,boxes):
        try:manual_blur.save_profile(self.root,self.login,boxes)
        except Exception as exc:messagebox.showerror('Блюр',str(exc),parent=self);return
        if self.on_saved:self.on_saved()
        messagebox.showinfo('Блюр','Настройки сохранены для '+self.login,parent=self);self.destroy()
