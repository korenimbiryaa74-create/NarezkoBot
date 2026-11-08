"""Окно добавления образца баннера: кадр из видео или скриншот, выделение мышью."""
from __future__ import annotations
import base64
import tkinter as tk
from tkinter import ttk, messagebox
import banner_blur

HINT = ('Выделите мышью неподвижную непрозрачную часть баннера (логотип, надпись, промокод) — без фона стрима. '
        'Если баннер анимирован или выезжает, добавьте 2–3 образца из разных моментов.')


class BannerPicker(tk.Toplevel):
    def __init__(self, master, image=None, video=None, ffmpeg=None, duration=0.0, on_saved=None):
        super().__init__(master)
        self.title('Образец баннера')
        self.video, self.ffmpeg, self.duration, self.on_saved = video, ffmpeg, float(duration or 0), on_saved
        self.frame = None; self.scale = 1.0; self.start = None; self.rect = None; self.box = None; self.photo = None
        self.time = tk.StringVar(value=f'{min(60.0, self.duration / 2):.1f}'); self.name = tk.StringVar()
        bar = ttk.Frame(self); bar.pack(fill='x', padx=6, pady=4)
        if video:
            ttk.Label(bar, text='Время, с').pack(side='left')
            entry = ttk.Entry(bar, textvariable=self.time, width=9); entry.pack(side='left', padx=3)
            entry.bind('<Return>', lambda _e: self.load_time())
            for d in (-60, -10, -1, 1, 10, 60):
                ttk.Button(bar, text=f'{d:+d}', width=4, command=lambda d=d: self.shift(d)).pack(side='left')
            ttk.Button(bar, text='Показать', command=self.load_time).pack(side='left', padx=3)
        ttk.Label(bar, text='Название').pack(side='left', padx=(10, 2))
        ttk.Entry(bar, textvariable=self.name, width=18).pack(side='left')
        ttk.Button(bar, text='Сохранить образец', command=self.save).pack(side='left', padx=6)
        self.hint = tk.StringVar(value=HINT)
        ttk.Label(self, textvariable=self.hint, wraplength=1000).pack(fill='x', padx=6)
        self.canvas = tk.Canvas(self, background='black', cursor='crosshair', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=6, pady=6)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        if image is not None:
            self.show(image)
        else:
            self.after(50, self.load_time)

    def shift(self, delta):
        try: t = float(self.time.get().replace(',', '.'))
        except ValueError: t = 0.0
        t = max(0.0, t + delta)
        if self.duration: t = min(t, max(0.0, self.duration - 0.5))
        self.time.set(f'{t:.1f}'); self.load_time()

    def load_time(self):
        try: t = max(0.0, float(self.time.get().replace(',', '.')))
        except ValueError:
            messagebox.showerror('Образец баннера', 'Время укажите в секундах, например 125.5', parent=self); return
        self.hint.set('Загрузка кадра…'); self.config(cursor='watch'); self.update_idletasks()
        try:
            self.show(banner_blur.full_frame_at(self.video, t, self.ffmpeg))
        except Exception as exc:
            messagebox.showerror('Образец баннера', str(exc), parent=self)
        finally:
            self.config(cursor=''); self.hint.set(HINT)

    def show(self, image):
        cv2, _np = banner_blur._cv()
        self.frame = image; h, w = image.shape[:2]
        max_w = max(400, self.winfo_screenwidth() - 120); max_h = max(300, self.winfo_screenheight() - 220)
        self.scale = min(1.0, max_w / w, max_h / h)
        view = image if self.scale == 1.0 else cv2.resize(image, (round(w * self.scale), round(h * self.scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode('.png', view)
        if not ok: raise banner_blur.BannerError('Не удалось показать кадр')
        self.photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode('ascii'))
        self.canvas.delete('all'); self.rect = None; self.box = None
        self.canvas.config(width=view.shape[1], height=view.shape[0])
        self.canvas.create_image(0, 0, image=self.photo, anchor='nw')

    def press(self, event):
        self.start = (event.x, event.y)
        if self.rect: self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='#ff2d2d', width=2)

    def drag(self, event):
        if self.start and self.rect: self.canvas.coords(self.rect, *self.start, event.x, event.y)

    def release(self, event):
        if not self.start or self.frame is None: return
        h, w = self.frame.shape[:2]
        x0, x1 = sorted((self.start[0], event.x)); y0, y1 = sorted((self.start[1], event.y))
        x0, y0 = max(0, round(x0 / self.scale)), max(0, round(y0 / self.scale))
        x1, y1 = min(w, round(x1 / self.scale)), min(h, round(y1 / self.scale))
        self.box = (x0, y0, x1 - x0, y1 - y0) if x1 - x0 >= 8 and y1 - y0 >= 8 else None
        self.start = None

    def save(self):
        if self.frame is None or not self.box:
            messagebox.showwarning('Образец баннера', 'Сначала выделите баннер мышью на кадре', parent=self); return
        try:
            t = float(self.time.get().replace(',', '.')) if self.video else None
            path = banner_blur.save_template(self.frame, self.box, self.name.get().strip(),
                                             source=str(self.video or ''), time_s=t)
        except Exception as exc:
            messagebox.showerror('Образец баннера', str(exc), parent=self); return
        if self.on_saved: self.on_saved()
        messagebox.showinfo('Образец баннера', f'Сохранено: {path.name}\nМожно выделить ещё один баннер или закрыть окно.', parent=self)
        if self.rect: self.canvas.delete(self.rect)
        self.rect = None; self.box = None
