import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import banner_blur
import manual_blur
import pipeline
import stream_cutter

try:
    import cv2, numpy as np
except ImportError:
    cv2 = None

FF = shutil.which('ffmpeg') and shutil.which('ffprobe')


def make_banner_video(folder):
    """10 s 640x360 moving background; textured banner at (400,30) from 2.0 to 6.0 s."""
    rng = np.random.default_rng(7)
    banner = rng.integers(0, 255, (60, 180, 3), dtype=np.uint8)
    cv2.putText(banner, 'PROMO', (20, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
    png = Path(folder) / 'banner.png'; cv2.imwrite(str(png), banner)
    src = Path(folder) / 'in.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=25', '-i', str(png),
                    '-filter_complex', "[0][1]overlay=400:30:enable='between(t,2,6)'", '-t', '10',
                    '-c:v', 'libx264', '-g', '25', '-pix_fmt', 'yuv420p', '-y', str(src)], check=True, timeout=60)
    return src


class BannerTests(unittest.TestCase):
    @unittest.skipUnless(cv2 is not None and FF, 'needs OpenCV and ffmpeg')
    def test_template_detection_with_refined_bounds(self):
        ff = shutil.which('ffmpeg')
        with tempfile.TemporaryDirectory() as d:
            src = make_banner_video(d); samples = Path(d) / 'samples'
            frame = banner_blur.full_frame_at(src, 4.0, ff)
            banner_blur.save_template(frame, (400, 30, 180, 60), 'promo', folder=samples)
            templates = banner_blur.load_templates(samples)
            events = []
            found = banner_blur.scan(src, [(0., 10.)], ff, events.append, interval=2, templates=templates)
            self.assertEqual(len(found), 1, found)
            self.assertAlmostEqual(found[0]['start'], 2.0, delta=0.4)
            self.assertTrue(5.9 <= found[0]['end'] <= 6.6, found[0])
            self.assertEqual(found[0]['name'], 'promo')
            x0, y0, x1, y1 = found[0]['box']
            self.assertTrue(x0 <= 625 and y0 <= 83 and x1 >= 906 and y1 >= 250, found[0]['box'])
            self.assertEqual(banner_blur.scan(src, [(7., 10.)], ff, None, interval=2, templates=templates), [])

    @unittest.skipUnless(cv2 is not None and FF, 'needs OpenCV and ffmpeg')
    def test_no_samples_is_setup_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(banner_blur.BannerSetupError):
                banner_blur.scan(Path(d) / 'x.mp4', [(0., 1.)], shutil.which('ffmpeg'), None, templates=[])

    @unittest.skipUnless(cv2 is not None, 'needs OpenCV')
    def test_flat_or_huge_sample_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                banner_blur.save_template(np.zeros((360, 640, 3), np.uint8), (10, 10, 100, 50), folder=d)
            noise = np.random.default_rng(1).integers(0, 255, (360, 640, 3), dtype=np.uint8)
            with self.assertRaises(ValueError):
                banner_blur.save_template(noise, (0, 0, 600, 300), folder=d)
            path = banner_blur.save_template(noise, (10, 10, 100, 50), 'Тест', folder=d)
            self.assertTrue(path.exists() and path.with_suffix('.json').exists())
            self.assertEqual(banner_blur.template_count(d), 1)

    def test_timed_regions_clip_to_chunk(self):
        det = [{'start': 1.0, 'end': 3.0, 'box': [0, 0, 500, 500], 'name': 'a', 'score': 1.0}]
        self.assertEqual(banner_blur.timed_regions(det, 5.0, 8.0, 640, 360), [])
        self.assertTrue(banner_blur.timed_regions(det, 2.0, 8.0, 640, 360))

    def test_fixed_profile_command_does_not_use_removed_filter_script_option(self):
        from types import SimpleNamespace
        args=SimpleNamespace(timed_banners=[
            {'start':0.,'end':5.,'box':[0,0,300,300]},
            {'start':0.,'end':5.,'box':[600,100,950,400]}],
            banner_output_size=(640,360), blur_vf=None,
            vcodec='libx264', preset='ultrafast', crf=23,
            acodec='aac',audio_bitrate='128k')
        with tempfile.TemporaryDirectory() as d:
            script=Path(d)/'русский путь'/'filter.txt'
            cmd=stream_cutter.chunk_cmd('ffmpeg','input.mp4',[(0.,5.)],'out.mp4',args,1,None,1,[],str(script))
        self.assertNotIn('-filter_script:v',cmd)
        self.assertIn('-vf',cmd)
        graph=cmd[cmd.index('-vf')+1]
        self.assertEqual(graph.count('boxblur='),2)
        self.assertFalse(script.exists())

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

    def test_response_error_stops_without_retry(self):
        from service import Service
        from settings import Settings
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);settings=Settings(root/'config.json')
            class Engine:
                def run(self,job,cfg,progress):
                    progress('Рендер','Поиск баннеров')
                    raise banner_blur.BannerResponseError('Ответ неверен')
            svc=Service(root,settings,None,pipeline_factory=lambda *a:Engine())
            svc.store.add('123','name');svc.worker_once()
            self.assertEqual(svc.store.list_jobs()[0]['state'],'failed')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_clip_and_report_with_local_video(self):
        from settings import Settings
        from service import Service
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'test_input';folder.mkdir();src=folder/'test.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:duration=6','-t','6','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            settings=Settings(root/'config.json');cfg=settings.load()
            cfg['pipeline']['min_reaction_minutes']=0;cfg['pipeline']['silence']['enabled']=False
            cfg['pipeline']['remove_donations']=False;cfg['pipeline']['banner_blur'].update(enabled=True,test_streamer='name');settings.save(cfg)
            manual_blur.save_profile(root,'name',[[0,0,400,400]])
            finder=lambda *a:[{'start':1.,'end':5.,'title':'Тест'}]
            def speech(src,cache,cfg,progress,duration):return ([{'start':1.,'end':2.,'text':'проверка'}],duration)
            svc=Service(root,settings,finder,pipeline_factory=lambda root,finder:pipeline.Pipeline(root,finder,speech=speech))
            svc.queue_test_inputs()
            with patch.object(banner_blur,'scan',side_effect=AssertionError('automatic scan should not run')),patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                svc.worker_once()
            job=svc.store.list_jobs()[0];self.assertEqual(job['state'],'done',job['detail'])
            report=next((root/'recordings').rglob('* — баннеры.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('00:00:01.000 — 00:00:05.000',report)
            self.assertIn('Ручной профиль',report)
            self.assertEqual(len(list((root/'recordings').rglob('01 Тест.mp4'))),1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_no_blur_profile_completes_and_writes_report(self):
        from settings import Settings
        from service import Service
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'test_input';folder.mkdir();src=folder/'test.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:duration=4','-t','4','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            settings=Settings(root/'config.json');cfg=settings.load()
            cfg['pipeline']['min_reaction_minutes']=0;cfg['pipeline']['silence']['enabled']=False
            cfg['pipeline']['remove_donations']=False;cfg['pipeline']['banner_blur'].update(enabled=True,test_streamer='someone');settings.save(cfg)
            manual_blur.save_profile(root,'someone',[])
            def speech(src,cache,cfg,progress,duration):return ([{'start':1.,'end':2.,'text':'проверка'}],duration)
            finder=lambda *a:[{'start':0.,'end':3.,'title':'Тест'}]
            svc=Service(root,settings,finder,pipeline_factory=lambda root,finder:pipeline.Pipeline(root,finder,speech=speech))
            svc.queue_test_inputs()
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):svc.worker_once()
            job=svc.store.list_jobs()[0];self.assertEqual(job['state'],'done',job['detail'])
            report=next((root/'recordings').rglob('* — баннеры.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('someone',report);self.assertIn('Без блюра',report)

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
            profile={'version':1,'mode':'regions','boxes':[[0,0,500,500]]}
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,row,100,{'enabled':False},banner_cfg={'enabled':True,'profile':profile})
            self.assertEqual(keeps,[(1.,2.),(3.,7.)])
            def sd(t):
                frame=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(out),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],stdout=subprocess.PIPE,check=True,timeout=20).stdout
                return ImageStat.Stat(Image.open(io.BytesIO(frame)).convert('RGB').crop((8,8,70,40))).stddev[0]
            before,inside,after=sd(.5),sd(1.5),sd(3.5)
            self.assertLess(max(before,inside,after),25,(before,inside,after))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_real_render_blurs_fixed_regions_for_full_clip(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir(); src=root/'input.mp4';out=root/'out.mp4'
            image=Image.new('RGB',(160,96));p=image.load()
            for y in range(96):
                for x in range(160):p[x,y]=((255,255,255) if (x//4+y//4)%2 else (0,0,0))
            image.save(root/'checker.png')
            subprocess.run(['ffmpeg','-v','error','-loop','1','-framerate','10','-i',str(root/'checker.png'),'-f','lavfi','-i','sine=frequency=440:duration=4','-t','4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(src)],check=True,timeout=30)
            found=[{'start':0.,'end':4.,'box':[0,0,500,500]}, {'start':0.,'end':4.,'box':[600,500,1000,1000]}]
            profile={'version':1,'mode':'regions','boxes':[[0,0,500,500],[600,500,1000,1000]]}
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,{'start':0.,'end':4.},0,{'enabled':False},banner_cfg={'enabled':True,'profile':profile})
            self.assertEqual(keeps.banner_scan,found);self.assertAlmostEqual(duration,4,delta=.3)
            def frame(t):
                b=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(out),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],stdout=subprocess.PIPE,check=True,timeout=20).stdout
                import io
                return Image.open(io.BytesIO(b)).convert('RGB')
            one=frame(1);three=frame(3)
            first=ImageStat.Stat(one.crop((8,8,70,40))).stddev[0]
            later=ImageStat.Stat(three.crop((8,8,70,40))).stddev[0]
            self.assertLess(max(first,later),25,(first,later))
            self.assertLess(ImageStat.Stat(one.crop((105,55,150,88))).stddev[0],25)
            self.assertGreater(ImageStat.Stat(one.crop((96,8,152,40))).stddev[0],50)

if __name__=='__main__':unittest.main()
