import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
import reaction_previews as rp
import preview_composite as pc


class SearchAndCompositeTests(unittest.TestCase):
    def test_late_spoken_title_is_searchable_and_outweighs_generic_matches(self):
        row={'start':0,'end':400,'title':'Полицейские разбирают семейные конфликты и необычные вызовы'}
        segs=[{'start':1,'text':'Какие то разные видео можно посмотреть'},
              {'start':240,'text':'Код 112'}, {'start':242,'text':'ППС тёплый приём'}]
        calls=[]
        def runner(cmd,**kw):
            calls.append(cmd[-1])
            entries=[{'id':'AAAAAAAAAAA','title':'Полицейские семейные конфликты и необычные вызовы'},
                     {'id':'BBBBBBBBBBB','title':'КОД 112 ППС ТЁПЛЫЙ ПРИЁМ'},
                     {'id':'CCCCCCCCCCC','title':'КОД 112 ППС ТЁПЛЫЙ ПРИЁМ трейлер'}]
            return SimpleNamespace(returncode=0,stdout=json.dumps({'entries':entries}).encode())
        found, queries=rp.find_candidates(row,segs,runner=runner)
        self.assertEqual(queries[0], '112 ппс теплый прием')
        self.assertEqual(found[0]['id'],'BBBBBBBBBBB')
        self.assertNotIn('CCCCCCCCCCC',[item['id'] for item in found])
        self.assertTrue(calls[0].endswith(queries[0]))

    def test_address_numbers_are_not_sent_as_spoken_title(self):
        row={'start':0,'end':40,'title':'Происшествие в большом городе'}
        segs=[{'start':1,'text':'улица 112'}, {'start':2,'text':'квартира десять'}]
        self.assertFalse(any(q.startswith('112 ') for q in rp.queries(row,segs)))

    def test_topic_overlap_is_not_source_identification(self):
        row={'start':0,'end':120,'title':'Стримеры осуждают казино и рекламируют лутбоксы'}
        segs=[{'start':1,'text':'Вопросы в России. На самом деле всё по-другому.'}]
        def runner(cmd,**kw):
            data={'entries':[{'id':'Ogw5PhqMu3I','title':'СЕКРЕТЫ БОГАТЫХ Как на самом деле решаются вопросы в России Александр Добровинский'}]}
            return SimpleNamespace(returncode=0,stdout=json.dumps(data).encode())
        with self.assertRaises(rp.SourceUncertain):rp.find_candidates(row,segs,runner=runner)

    def test_youtube_thumbnail_prefers_current_hq720_and_falls_back(self):
        import io
        image=io.BytesIO();Image.new('RGB',(1280,720),'red').save(image,'JPEG');payload=image.getvalue()
        class Response:
            def __init__(self,status):self.status_code=status
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,*args):yield payload
        class Session:
            def __init__(self,missing=False):self.urls=[];self.missing=missing
            def get(self,url,**kw):
                self.urls.append(url)
                return Response(404 if self.missing and 'hq720' in url else 200)
        s=Session(); im,url=rp.download_thumbnail('LTrigE81ed8',s)
        self.assertTrue(url.endswith('/hq720.jpg'));self.assertEqual(im.size,(1280,720));self.assertEqual(len(s.urls),1)
        s=Session(missing=True);im,url=rp.download_thumbnail('LTrigE81ed8',s)
        self.assertTrue(url.endswith('/maxresdefault.jpg'));self.assertEqual(len(s.urls),2)

    def test_synthetic_composite_preserves_generated_illustration_and_real_portrait(self):
        model=Image.new('RGB',(1280,720),(20,70,40));draw=ImageDraw.Draw(model)
        draw.rectangle((30,500,610,550),fill=(255,220,0));draw.rectangle((30,590,580,680),fill='white')
        with tempfile.TemporaryDirectory() as d:
            portrait=Path(d)/'portrait.png';p=Image.new('RGBA',(200,400),(20,40,80,255));p.putpixel((0,0),(0,0,0,0));p.save(portrait)
            result=pc.compose(model,None,portrait)
            self.assertEqual(result.getpixel((100,100)),(20,70,40))
            self.assertEqual(result.getpixel((1200,200)),(20,40,80))

    def test_composite_keeps_source_and_original_portrait(self):
        model=Image.new('RGB',(1280,720),(30,0,0)); draw=ImageDraw.Draw(model)
        draw.rectangle((30,480,610,550),fill=(255,220,0)); draw.rectangle((30,590,580,680),fill='white')
        source=Image.new('RGB',(1280,720),(0,120,40))
        with tempfile.TemporaryDirectory() as d:
            portrait=Path(d)/'portrait.png';Image.new('RGBA',(300,720),(200,20,30,255)).save(portrait)
            # A real cutout has at least some transparent pixels.
            p=Image.open(portrait);p.putpixel((0,0),(0,0,0,0));p.save(portrait)
            result=pc.compose(model,source,portrait)
            self.assertEqual(result.getpixel((100,100)),(0,120,40))
            self.assertEqual(result.getpixel((1200,200)),(200,20,30))
            self.assertEqual(result.getpixel((100,510)),(255,220,0))
            self.assertEqual(result.size,(1280,720))

    def test_old_frame_line_is_removed_without_removing_new_caption(self):
        model=Image.new('RGB',(1280,720),(30,0,0));draw=ImageDraw.Draw(model)
        draw.rectangle((10,475,750,477),fill='white')
        draw.rectangle((30,500,610,550),fill=(255,220,0));draw.rectangle((30,590,580,680),fill='white')
        with tempfile.TemporaryDirectory() as d:
            portrait=Path(d)/'portrait.png';p=Image.new('RGBA',(200,400),(20,40,80,255));p.putpixel((0,0),(0,0,0,0));p.save(portrait)
            result=pc.compose(model,Image.new('RGB',(1280,720),'blue'),portrait)
            self.assertNotEqual(result.getpixel((100,476)),(255,255,255))
            self.assertEqual(result.getpixel((100,510)),(255,220,0))

    def test_missing_model_lettering_fails_instead_of_claiming_success(self):
        with tempfile.TemporaryDirectory() as d:
            portrait=Path(d)/'portrait.png';p=Image.new('RGBA',(200,400),(1,2,3,255));p.putpixel((0,0),(0,0,0,0));p.save(portrait)
            with self.assertRaisesRegex(ValueError,'выделить надпись'):
                pc.compose(Image.new('RGB',(1280,720),'black'),Image.new('RGB',(1280,720),'blue'),portrait)

if __name__=='__main__':unittest.main()
