import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import banner_blur
import pipeline
import stream_cutter

class BannerTests(unittest.TestCase):
    def test_parse_and_padding(self):
        self.assertEqual(banner_blur.parse_boxes('{"banners":[]}'),[])
        self.assertEqual(banner_blur.parse_boxes('{"banners":[[0,0,100,100]]}'),[[0,0,112,112]])
        for raw in ('no','{}','{"banners":[[1,2,0,3]]}','{"banners":[[0,0,1000,1000]]}','{"banners":[[0,0,1001,5]]}','{"banners":[[0,0,3,true]]}'):
            with self.subTest(raw=raw),self.assertRaises(banner_blur.BannerError):banner_blur.parse_boxes(raw)
    def test_windows_and_offsets(self):
        sample=[{'start':10.,'end':15.,'box':[0,0,200,200]},{'start':15.,'end':20.,'box':[0,0,200,200]}]
        self.assertEqual(banner_blur.timed_regions(sample,12,18,1000,500),[(0,0,200,100,[(0,6)])])
        self.assertEqual(banner_blur.timed_regions(sample,21,25,1000,500),[])
    def test_model_error_is_not_no_banners(self):
        from urllib.error import URLError
        with patch('banner_blur.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=URLError('down')
            with self.assertRaises(banner_blur.BannerError):banner_blur.request_boxes(b'a')
    def test_ollama_error_diagnostics_do_not_leak_server_content(self):
        from urllib.error import HTTPError,URLError
        import io
        cases=[
            (HTTPError('http://localhost',404,'',{},io.BytesIO(b'{"error":"model qwen3-vl:4b not found: SECRET"}')),banner_blur.BannerSetupError,'не найдена'),
            (HTTPError('http://localhost',404,'',{},io.BytesIO(b'{"error":"unknown route SECRET"}')),banner_blur.BannerSetupError,'HTTP 404'),
            (HTTPError('http://localhost',500,'',{},io.BytesIO(b'{"error":"out of memory SECRET"}')),banner_blur.BannerSetupError,'недостаточно памяти'),
            (HTTPError('http://localhost',500,'',{},io.BytesIO(b'{"error":"SECRET"}')),banner_blur.BannerError,'HTTP 500'),
            (URLError(ConnectionRefusedError('SECRET')),banner_blur.BannerSetupError,'Нет соединения'),
            (URLError(TimeoutError('SECRET')),banner_blur.BannerError,'180 с'),
        ]
        for err,cls,expected in cases:
            with self.subTest(expected=expected),patch('banner_blur.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect=err
                with self.assertRaises(cls) as caught:banner_blur.request_boxes(b'x')
                self.assertIn(expected,str(caught.exception));self.assertNotIn('SECRET',str(caught.exception))

    def test_permanent_model_setup_error_stops_queue_without_retry(self):
        from service import Service
        from settings import Settings
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);settings=Settings(root/'config.json');settings.load()
            class Engine:
                def run(self,job,cfg,progress):
                    progress('Рендер','Поиск баннеров')
                    raise banner_blur.BannerSetupError('Модель не найдена')
            svc=Service(root,settings,None,pipeline_factory=lambda *a:Engine())
            job,_=svc.store.add('123','name');svc.worker_once()
            saved=svc.store.list_jobs()[0]
            self.assertEqual(saved['state'],'failed');self.assertIn('Модель не найдена',saved['detail'])
            self.assertFalse(any('повтор через' in e for _,e in svc.logs()))

    def test_ollama_protocol_and_bounds(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return json.dumps({'message':{'content':'{"banners":[]}'}}).encode()
        with patch('banner_blur.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            self.assertEqual(banner_blur.request_boxes(b'jpeg data'),[])
            request=opener.return_value.open.call_args.args[0]
            data=json.loads(request.data)
            self.assertEqual(data['model'],'qwen3-vl:4b');self.assertFalse(data['stream'])
            self.assertTrue(data['messages'][0]['images'][0]);self.assertIn('banners',data['format']['properties'])

    @unittest.skipUnless(shutil.which('ffmpeg'),'needs ffmpeg')
    def test_real_sampling_and_empty_detection(self):
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/'in.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-t','6','-c:v','libx264','-y',str(src)],check=True,timeout=30)
            events=[]
            with patch.object(banner_blur,'request_boxes',return_value=[]) as model:
                self.assertEqual(banner_blur.scan(src,[(0.,6.)],shutil.which('ffmpeg'),events.append,interval=2),[])
            self.assertEqual(model.call_count,3);self.assertTrue(all(c.args[0].startswith(b'\xff\xd8') for c in model.call_args_list))
            self.assertTrue(any('3/3' in e for e in events))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_clip_and_report_with_local_video(self):
        from settings import Settings
        from service import Service
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'test_input';folder.mkdir();src=folder/'test.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:duration=6','-t','6','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            settings=Settings(root/'config.json');cfg=settings.load()
            cfg['pipeline']['min_reaction_minutes']=0;cfg['pipeline']['silence']['enabled']=False
            cfg['pipeline']['remove_donations']=False;cfg['pipeline']['banner_blur']['enabled']=True;settings.save(cfg)
            finder=lambda *a:[{'start':1.,'end':5.,'title':'Тест'}]
            def speech(src,cache,cfg,progress,duration):return ([{'start':1.,'end':2.,'text':'проверка'}],duration)
            svc=Service(root,settings,finder,pipeline_factory=lambda root,finder:pipeline.Pipeline(root,finder,speech=speech))
            svc.queue_test_inputs()
            found=[{'start':1.,'end':3.,'box':[0,0,400,400]}]
            with patch.object(banner_blur,'scan',return_value=found),patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                svc.worker_once()
            job=svc.store.list_jobs()[0];self.assertEqual(job['state'],'done',job['detail'])
            report=next((root/'recordings').rglob('* — баннеры.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('00:00:01.000 — 00:00:03.000',report)
            self.assertIn('могут быть пропущены',report)
            self.assertEqual(len(list((root/'recordings').rglob('01 Тест.mp4'))),1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_blur_time_across_removed_interval_and_offset(self):
        from PIL import Image,ImageStat
        import io
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir();src=root/'input.mp4';out=root/'out.mp4'
            im=Image.new('RGB',(160,96));pixels=im.load()
            for y in range(96):
                for x in range(160):pixels[x,y]=((255,255,255) if (x//4+y//4)%2 else (0,0,0))
            im.save(root/'checker.png')
            subprocess.run(['ffmpeg','-v','error','-loop','1','-framerate','10','-i',str(root/'checker.png'),'-f','lavfi','-i','sine=frequency=440:duration=8','-t','8','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(src)],check=True,timeout=30)
            row={'start':101.,'end':107.,'donation_cuts':[{'start':102.,'end':103.}]}
            found=[{'start':3.,'end':5.,'box':[0,0,500,500]}]
            with patch.object(banner_blur,'scan',return_value=found),patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,row,100,{'enabled':False},banner_cfg={'enabled':True})
            self.assertEqual(keeps,[(1.,2.),(3.,7.)])
            def sd(t):
                frame=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(out),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],stdout=subprocess.PIPE,check=True,timeout=20).stdout
                return ImageStat.Stat(Image.open(io.BytesIO(frame)).convert('RGB').crop((8,8,70,40))).stddev[0]
            before,inside,after=sd(.5),sd(1.5),sd(3.5)
            self.assertLess(inside,before*.6,(before,inside,after));self.assertGreater(after,50)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_real_render_blurs_only_during_detected_window(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir(); src=root/'input.mp4';out=root/'out.mp4'
            image=Image.new('RGB',(160,96));p=image.load()
            for y in range(96):
                for x in range(160):p[x,y]=((255,255,255) if (x//4+y//4)%2 else (0,0,0))
            image.save(root/'checker.png')
            subprocess.run(['ffmpeg','-v','error','-loop','1','-framerate','10','-i',str(root/'checker.png'),'-f','lavfi','-i','sine=frequency=440:duration=4','-t','4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(src)],check=True,timeout=30)
            found=[{'start':0.,'end':2.,'box':[0,0,500,500]}]
            with patch.object(banner_blur,'scan',return_value=found),patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,{'start':0.,'end':4.},0,{'enabled':False},banner_cfg={'enabled':True})
            self.assertEqual(keeps.banner_scan,found);self.assertAlmostEqual(duration,4,delta=.3)
            def frame(t):
                b=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(out),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],stdout=subprocess.PIPE,check=True,timeout=20).stdout
                import io
                return Image.open(io.BytesIO(b)).convert('RGB')
            one=frame(1);three=frame(3)
            first=ImageStat.Stat(one.crop((8,8,70,40))).stddev[0]
            later=ImageStat.Stat(three.crop((8,8,70,40))).stddev[0]
            self.assertLess(first,later*.6,(first,later))
            self.assertGreater(ImageStat.Stat(one.crop((96,8,152,40))).stddev[0],50)

if __name__=='__main__':unittest.main()
