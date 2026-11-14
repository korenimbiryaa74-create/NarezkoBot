"""Deterministic reaction thumbnail: framed source image, streamer cutout, exact headline.

No model draws the person or the text, so the streamer can never appear twice and
every letter of the headline is exactly the text that was requested.
Look follows the channel style: deep red background, white-framed source on the
left, streamer on the right, heavy italic yellow/white headline with 3D shadow.
"""
from __future__ import annotations
import colorsys
import itertools
import os
import re
import tempfile
from pathlib import Path
from PIL import Image,ImageChops,ImageDraw,ImageFilter,ImageFont,ImageOps

ROOT=Path(__file__).resolve().parent
W,H=1280,720
FRAME=(14,14,800,456)          # source frame box (786x442, 16:9)
TEXT_TOP,TEXT_BOTTOM,TEXT_LEFT=466,710,20
PERSON_MAX_W=640
DEFAULT_BG=(120,10,18)         # Stint-like deep red
YELLOW=((255,248,120),(255,214,20),(255,158,0))
WHITE=((255,255,255),(240,241,245),(178,184,198))


class RenderError(ValueError):
    pass


def _font(size):
    """(font, is_italic). Montserrat Black Italic ships with the bot (OFL)."""
    for p,italic in ((ROOT/'fonts'/'Montserrat-BlackItalic.ttf',True),(ROOT/'fonts'/'Montserrat-ExtraBoldItalic.ttf',True)):
        if p.is_file():return ImageFont.truetype(str(p),size),italic
    win=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for name,italic in (('seguibli.ttf',True),('ariblk.ttf',False),('arialbd.ttf',False)):
        p=win/name
        if p.is_file():return ImageFont.truetype(str(p),size),italic
    raise RenderError('Не найден жирный шрифт с кириллицей (fonts/Montserrat-BlackItalic.ttf)')


# ---------------- background ----------------
def _base_colour(style):
    """Saturated colour from the style example; grey/neutral styles fall back to deep red."""
    if not style:return DEFAULT_BG
    try:
        with Image.open(style) as im:small=im.convert('RGB').resize((64,36))
    except OSError:return DEFAULT_BG
    best=None
    for x in range(64):
        for y in range(36):
            if not (x<12 or y>26):continue          # background usually shows left/bottom
            r,g,b=small.getpixel((x,y));h,s,v=colorsys.rgb_to_hsv(r/255,g/255,b/255)
            if s>0.45 and 0.12<v<0.75:
                score=s*v
                if best is None or score>best[0]:best=(score,(h,s,v))
    if not best:return DEFAULT_BG
    h,s,v=best[1];r,g,b=colorsys.hsv_to_rgb(h,min(1,s*1.05),0.47)
    return (int(r*255),int(g*255),int(b*255))


def _mix(a,b,t):
    return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))


def _background(style):
    # Channel look: always the Stint-like deep red (style examples only had grey/brown tints).
    base=DEFAULT_BG
    centre=tuple(min(255,int(c*1.25)) for c in base);edge=tuple(int(c*0.16) for c in base)
    # big soft radial light behind the frame / headline
    g=Image.radial_gradient('L').resize((2200,1500))
    g=g.crop((1100-470,750-330,1100-470+W,750-330+H))
    bg=Image.composite(Image.new('RGB',(W,H),edge),Image.new('RGB',(W,H),centre),g)
    # light rays from the upper-left
    rays=Image.new('L',(W,H),0);d=ImageDraw.Draw(rays)
    for i,ang in enumerate(range(-10,80,9)):
        import math
        a1=math.radians(ang);a2=math.radians(ang+3.2)
        d.polygon([(-80,-60),(-80+2400*math.cos(a1),-60+2400*math.sin(a1)),(-80+2400*math.cos(a2),-60+2400*math.sin(a2))],fill=34 if i%2 else 20)
    rays=rays.filter(ImageFilter.GaussianBlur(14))
    bg=Image.composite(Image.new('RGB',(W,H),_mix(centre,(255,90,90),0.35)),bg,rays)
    # dark vignette, strongest at the bottom-right behind the streamer
    vig=Image.radial_gradient('L').resize((1900,1300)).crop((950-560,650-300,950-560+W,650-300+H)).point(lambda v:max(0,v-40)*255//215)
    bg=Image.composite(Image.new('RGB',(W,H),(8,0,1)),bg,vig.point(lambda v:v*170//255))
    # film grain against banding
    noise=Image.effect_noise((W,H),28).convert('RGB')
    bg=Image.blend(bg,ImageChops.multiply(bg,noise.point(lambda v:200+v//5)),0.35)
    return bg.convert('RGBA')


def _soft(alpha,offset,blur,opacity,pos=(0,0),colour=(0,0,0)):
    layer=Image.new('L',(W,H),0);layer.paste(alpha,(pos[0]+offset[0],pos[1]+offset[1]))
    layer=layer.filter(ImageFilter.GaussianBlur(blur)).point(lambda v:v*opacity//255)
    out=Image.new('RGBA',(W,H),colour+(0,));out.putalpha(layer);return out


# ---------------- streamer ----------------
def _person(cutout):
    with Image.open(cutout) as src:
        if src.width*src.height>40_000_000:raise RenderError('Вырез слишком большой')
        im=ImageOps.exif_transpose(src).convert('RGBA')
    a=im.getchannel('A')
    if a.getextrema()==(255,255):raise RenderError('Вырез должен быть PNG с прозрачным фоном')
    box=a.point(lambda v:255 if v>20 else 0).getbbox()
    if not box:raise RenderError('Вырез пустой')
    im=im.crop(box)
    scale=min(H/im.height,PERSON_MAX_W/im.width)
    im=im.resize((max(1,round(im.width*scale)),max(1,round(im.height*scale))),Image.Resampling.LANCZOS)
    # small upscales look soft: a light unsharp mask restores edges
    return im.filter(ImageFilter.UnsharpMask(radius=2,percent=60,threshold=2)) if scale>1.2 else im


# ---------------- headline ----------------
def _splits(words,max_lines=3):
    n=len(words)
    for lines in range(1,min(max_lines,n)+1):
        for cuts in itertools.combinations(range(1,n),lines-1):
            idx=(0,)+cuts+(n,)
            yield [' '.join(words[idx[i]:idx[i+1]]) for i in range(lines)]


def _metrics(size):
    sw=max(3,round(size*0.06));depth=max(3,round(size*0.075))
    return sw,depth


def _measure(text,size):
    f,italic=_font(size);sw,depth=_metrics(size)
    l,t,r,b=ImageDraw.Draw(Image.new('L',(1,1))).textbbox((0,0),text,font=f,stroke_width=sw)
    h=b-t;extra=(0 if italic else int(0.16*h))+round(depth*0.5)
    return f,italic,(r-l)+extra,h,(l,t)


def _gap(size):
    return -round(size*0.03)       # lines sit tightly, like on the channel's covers


def fit_text(label,box_w,box_h):
    words=re.sub(r'\s+',' ',str(label)).strip().upper().split(' ')
    if not words or not words[0]:raise RenderError('Пустая надпись')
    best=None
    for lines in _splits(words):
        lo,hi=28,200
        while lo<hi:
            mid=(lo+hi+1)//2
            ms=[_measure(x,mid) for x in lines]
            if max(m[2] for m in ms)<=box_w and sum(m[3] for m in ms)+_gap(mid)*(len(lines)-1)<=box_h:lo=mid
            else:hi=mid-1
        ms=[_measure(x,lo) for x in lines]
        if max(m[2] for m in ms)>box_w:continue
        widths=[m[2] for m in ms]
        balance=min(widths)/max(widths) if len(widths)>1 else 1.0
        score=lo*(1.0 if len(lines)<=2 else 0.86)*(0.85+0.15*balance)
        if len(lines)>1 and min(len(x) for x in lines)<=3:score*=0.72   # lone «Я», «НЕ»
        if best is None or score>best[0]:best=(score,lo,lines)
    if best is None:raise RenderError('Надпись не помещается: сократите текст')
    return best[1],best[2]


def _gradient(size,stops):
    w,h=size;top,mid,bottom=stops
    col=Image.new('RGB',(1,256))
    for y in range(256):
        t=y/255
        col.putpixel((0,y),_mix(top,mid,t/0.45) if t<0.45 else _mix(mid,bottom,(t-0.45)/0.55))
    return col.resize((w,h),Image.Resampling.BILINEAR).convert('RGBA')


def _line_layer(text,size,stops):
    f,italic,w,h,(l,t)=_measure(text,size);sw,depth=_metrics(size)
    pad=sw+depth+8;lw,lh=w+pad*2,h+pad*2;org=(pad-l,pad-t)
    fill=Image.new('L',(lw,lh),0);ImageDraw.Draw(fill).text(org,text,font=f,fill=255)
    outline=Image.new('L',(lw,lh),0);ImageDraw.Draw(outline).text(org,text,font=f,fill=255,stroke_width=sw,stroke_fill=255)
    extrude=Image.new('L',(lw,lh),0)
    for i in range(1,depth+1):extrude=ImageChops.lighter(extrude,ImageChops.offset(outline,round(i*0.55),i))
    layer=Image.new('RGBA',(lw,lh),(0,0,0,0))
    layer.paste((40,3,6,255),(0,0),extrude)              # 3D side
    layer.paste((14,1,2,255),(0,0),outline)              # thick outline
    # text fill with gradient; the top edge is tied to the glyph height
    grad=Image.new('RGBA',(lw,lh),(0,0,0,0));grad.paste(_gradient((lw,h),stops),(0,pad))
    layer.paste(grad,(0,0),fill)
    # thin bright inner rim on the upper edge for a glossy look
    rim=ImageChops.subtract(fill,ImageChops.offset(fill,0,max(1,size//40)))
    layer.paste((255,255,255,140),(0,0),rim.point(lambda v:v*110//255))
    if not italic:
        k=0.16;layer=layer.transform((lw+int(k*lh),lh),Image.Transform.AFFINE,(1,k,-k*lh,0,1,0),resample=Image.Resampling.BICUBIC)
    return layer,pad


def render(frame,cutout,label,destination,style=None):
    """frame: PIL image of the source (or illustration). Returns layout info."""
    if frame.width<160 or frame.height<90:raise RenderError('Изображение источника слишком маленькое')
    canvas=_background(style)
    # 1) framed source with a red glow, like the channel covers
    x0,y0,x1,y1=FRAME;fw,fh=x1-x0,y1-y0
    src=ImageOps.fit(frame.convert('RGB'),(fw,fh),Image.Resampling.LANCZOS)
    plate=Image.new('L',(fw+8,fh+8),255)
    canvas.alpha_composite(_soft(plate,(0,0),26,150,pos=(x0-4,y0-4),colour=(255,40,50)))
    canvas.alpha_composite(_soft(plate,(10,14),16,170,pos=(x0-4,y0-4)))
    ImageDraw.Draw(canvas).rectangle((x0-4,y0-4,x1+3,y1+3),fill=(238,238,242,255))
    canvas.paste(src,(x0,y0))
    # 2) streamer, bottom-right, in front of the frame
    person=_person(cutout);px,py=W-person.width,H-person.height
    a=person.getchannel('A')
    canvas.alpha_composite(_soft(a,(-6,0),22,170,pos=(px,py)))
    canvas.alpha_composite(person,(px,py))
    # 3) headline in the lower-left, never far under the streamer's body
    full=Image.new('L',(W,H),0);full.paste(a,(px,py))
    band=full.crop((0,TEXT_TOP,W,TEXT_BOTTOM)).point(lambda v:255 if v>40 else 0)
    cols=band.getbbox()
    # text may lie over the streamer's torso a little, as on the channel covers
    right=min(830,max(760,(cols[0] if cols else W)+110))
    size,lines=fit_text(label,right-TEXT_LEFT,TEXT_BOTTOM-TEXT_TOP)
    layers=[_line_layer(x,size,YELLOW if i%2==0 else WHITE) for i,x in enumerate(lines)]
    heights=[_measure(x,size)[3] for x in lines]
    total=sum(heights)+_gap(size)*(len(lines)-1)
    y=TEXT_TOP+max(0,(TEXT_BOTTOM-TEXT_TOP-total)//2);placed=[]
    for (layer,pad),hgt in zip(layers,heights):
        lx,ly=TEXT_LEFT-pad,y-pad
        canvas.alpha_composite(_soft(layer.getchannel('A'),(8,12),12,190,pos=(lx,ly)))
        canvas.alpha_composite(layer,(lx,ly) if lx>=0 else (0,ly))
        placed.append((TEXT_LEFT,y,lx+layer.width-pad,y+hgt));y+=hgt+_gap(size)
    dest=Path(destination);dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.preview-',suffix='.jpg',dir=dest.parent);os.close(fd)
    try:
        canvas.convert('RGB').save(tmp,'JPEG',quality=94,optimize=True,subsampling=0)
        if dest.exists():raise RenderError('Превью уже существует; не перезаписываю')
        if os.name=='nt':os.rename(tmp,dest)
        else:os.link(tmp,dest)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'font_size':size,'lines':lines,'text_boxes':placed,'text_right_limit':right,'person_box':(px,py,W,H)}
