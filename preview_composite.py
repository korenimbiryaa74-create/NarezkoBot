"""Preserve real source and portrait while using locally generated headline styling.

Experimental: the model output must still be reviewed for spelling and layout.
"""
from pathlib import Path
import numpy as np
import cv2
from PIL import Image, ImageOps, ImageDraw, ImageFilter


def compose(model, source, portrait):
    model=ImageOps.fit(model.convert('RGB'),(1280,720),Image.Resampling.LANCZOS).convert('RGBA')
    if source is None: src=None
    elif isinstance(source,(str,Path)):
        with Image.open(source) as image: src=ImageOps.exif_transpose(image).convert('RGB')
    else: src=source.convert('RGB')
    with Image.open(portrait) as image:
        if image.width*image.height>40_000_000: raise ValueError('Слишком большой вырез стримера')
        person=ImageOps.exif_transpose(image).convert('RGBA')
    alpha=person.getchannel('A').getextrema()
    if alpha==(255,255) or alpha==(0,0): raise ValueError('Вырез должен быть непустым прозрачным PNG')
    # The style model is asked to place large yellow/white lettering in the lower-left.
    # Recover its strokes + shadow, rather than asking it to repaint the real assets.
    rgb=np.asarray(model.convert('RGB'),dtype=np.int16)
    r,g,b=rgb[:,:,0],rgb[:,:,1],rgb[:,:,2]
    yellow=(r>150)&(g>110)&(b<145)&(r>g*.85)
    white=(r>145)&(g>145)&(b>145)&((np.maximum.reduce([r,g,b])-np.minimum.reduce([r,g,b]))<65)
    yellow[:450,:]=False;white[:475,:]=False
    # Suppress a long horizontal line left by the model's original frame.
    upper=white[:490,:].astype(np.uint8)
    rule=cv2.morphologyEx(upper,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(90,1)))
    white[:490,:] &= ~rule.astype(bool)
    strokes=(yellow|white).astype(np.uint8);strokes[:,790:]=0
    # Remove small bright fragments of the old generated frame/background.
    connected=cv2.dilate(strokes,cv2.getStructuringElement(cv2.MORPH_RECT,(7,7)))
    n,labels,stats,_=cv2.connectedComponentsWithStats(connected,8)
    keep=np.zeros(n,dtype=np.uint8)
    for i,(x,y,w,h,area) in enumerate(stats[1:],1):
        if area>=160 and h>=22 and w>=9: keep[i]=1
    selected=keep[labels]*255
    if np.count_nonzero(selected)<3500: raise ValueError('Не удалось выделить надпись модели в ожидаемой области')
    mask=Image.fromarray(selected.astype('uint8')).filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(1))
    base=model.copy()
    if src is not None:
        ImageDraw.Draw(base).rectangle((0,0,850,485),fill=(54,2,6,255))
        base.paste(ImageOps.fit(src,(790,432),Image.Resampling.LANCZOS),(12,12))
        ImageDraw.Draw(base).rectangle((10,10,803,445),outline=(245,245,245,255),width=4)
    h=720;w=max(1,round(person.width*h/person.height))
    person=person.resize((w,h),Image.Resampling.LANCZOS)
    if w>1280: person=person.crop((w-1280,0,w,720));w=1280
    base.alpha_composite(person,(1280-w,0))
    base.paste(model,(0,0),mask)
    return base.convert('RGB')
