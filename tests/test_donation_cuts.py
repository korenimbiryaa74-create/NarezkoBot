import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pipeline
import stream_cutter
from chatgpt_finder import ChatGPTFinder
from settings import Settings
from service import Service

class FakeWeb:
    def close(self): pass
    def __init__(self,**kw): self.timeout=0;self.calls=0
    def ask(self,prompt,files,until):
        self.calls+=1
        text='{"donation_cuts":[{"reaction":1,"start":"00:00:03.000","end":"00:00:06.000","confidence":"high"}]}'
        assert until(text) and 'донат' in prompt and Path(files[0]).is_file()
        return {'text':text}

class DonationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.rows=[{'start':0.,'end':12.,'title':'Видео'}]
        self.segs=[{'start':1.,'end':2.,'text':'начало'}, {'start':3.,'end':6.,'text':'донат'}, {'start':8.,'end':9.,'text':'продолжение'}]
    def test_valid_request_cache_and_bounds(self):
        web=FakeWeb();f=ChatGPTFinder(self.root,web_factory=lambda **kw:web)
        first=f.find_donations(self.segs,self.rows,lambda *a:None)
        second=f.find_donations(self.segs,self.rows,lambda *a:None)
        self.assertEqual(first,[{'reaction':1,'start':3.,'end':6.}]);self.assertEqual(first,second);self.assertEqual(web.calls,1)
    def test_invalid_or_unsafe_intervals_are_skipped_not_fatal(self):
        # Сомнительный донат остаётся в видео, задание не останавливается.
        f=ChatGPTFinder(self.root,web_factory=lambda **kw:self.fail('browser must not be used'))
        for cut in [
            {'reaction':1,'start':2,'end':13,'confidence':'high'},
            {'reaction':2,'start':2,'end':3,'confidence':'high'},
            {'reaction':1,'start':2,'end':9,'confidence':'high'},
            {'reaction':1,'start':2,'end':3,'confidence':'low'},
            {'reaction':1,'start':4,'end':3,'confidence':'high'},
        ]:
            with self.subTest(cut=cut):
                self.assertEqual(f.find_donations(self.segs,self.rows,lambda *a:None,manual=json.dumps({'donation_cuts':[cut]})),[])
        with self.assertRaises(Exception): f.find_donations(self.segs,self.rows,lambda *a:None,manual='{"donation_cuts":')
        merged=f.find_donations(self.segs,self.rows,lambda *a:None,manual=json.dumps({'donation_cuts':[{'reaction':1,'start':2,'end':4,'confidence':'high'},{'reaction':1,'start':3,'end':5,'confidence':'high'}]}))
        self.assertEqual(merged,[{'reaction':1,'start':2.,'end':5.}])
    def test_subtraction_union_and_offset(self):
        self.assertEqual(pipeline.subtract_intervals([(1,4),(6,10)],[(2,7),(8,9)]),[(1,2),(7,8),(9,10)])
        self.assertEqual(pipeline.validate_donation_cuts([{'reaction':1,'start':2,'end':15}],self.rows),[])
        self.assertEqual(pipeline.validate_donation_cuts([{'reaction':1,'start':0,'end':4},{'reaction':1,'start':6,'end':10}],self.rows),[])
        self.assertEqual(pipeline.validate_donation_cuts([{'reaction':1,'start':3,'end':5}],self.rows),[{'reaction':1,'start':3.,'end':5.}])
    def test_empty_answer_is_not_failure(self):
        f=ChatGPTFinder(self.root)
        self.assertEqual(f.find_donations(self.segs,self.rows,lambda *a:None,manual='{"donation_cuts":[]}'),[])
    def test_manual_donation_answer_saved_for_failed_job(self):
        svc=Service(self.root,Settings(self.root/'config.json'),None);job,_=svc.store.add('123','name');svc.store.claim();svc.store.finish(job['id'],'failed','x')
        data='{"donation_cuts":[{"reaction":1,"start":"00:00:03","end":"00:00:06","confidence":"high"}]}'
        self.assertEqual(svc.save_manual_reactions(job['id'],data),1)
        saved=pipeline.load_json(pipeline.manual_donation_path(self.root,job['id']));self.assertEqual(saved['job'],pipeline.job_identity(svc.store.list_jobs()[0]))
        self.assertFalse(pipeline.manual_answer_path(self.root,job['id']).exists())

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class RealDonationRenderTest(unittest.TestCase):
    def test_twitch_fragment_offset_is_applied(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir();src=root/'fragment.mp4';out=root/'clip.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:duration=12','-t','12','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            row={'start':10.,'end':22.,'title':'Тест','donation_cuts':[{'start':13.,'end':16.}]}
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,row,10,{'enabled':False})
            self.assertEqual(keeps,[(0.,3.),(6.,12.)]);self.assertAlmostEqual(duration,9,delta=.3)

    def test_silence_and_donation_are_both_cut(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir();src=root/'input.mp4';out=root/'clip.mp4'
            audio=r'aevalsrc=if(between(t\,3\,6)\,0\,0.5*sin(2*PI*440*t)):s=16000:d=12'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i',audio,'-t','12','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            row={'start':0.,'end':12.,'title':'Тест','donation_cuts':[{'start':7.,'end':9.}]}
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,row,0,{'enabled':True,'min_cut':1.,'max_gap':1.})
            self.assertLess(duration,9);self.assertGreater(duration,6)
            self.assertTrue(all(not (a<7 and b>9) for a,b in keeps));self.assertTrue(any(3 < a < 7 for a,b in keeps))
            self.assertGreater(len(keeps),2)

    def test_discussion_keeps_donation_but_cuts_silence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'_work').mkdir();src=root/'input.mp4';out=root/'clip.mp4'
            audio=r'aevalsrc=if(between(t\,3\,6)\,0\,0.5*sin(2*PI*440*t)):s=16000:d=12'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i',audio,'-t','12','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            row={'type':'discussion','start':0.,'end':12.,'title':'Ответ на донат','donation_cuts':[{'start':7.,'end':9.}]}
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,out,row,0,{'enabled':True,'min_cut':1.,'max_gap':1.})
            self.assertLess(duration,11);self.assertGreater(duration,8)
            self.assertTrue(any(a<=7 and b>=9 for a,b in keeps),keeps)
            self.assertTrue(any(3<a<7 for a,b in keeps),keeps)

    def test_donation_removed_and_transcript_tracks_edit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'test_input';folder.mkdir();src=folder/'test.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=12','-t','12','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            settings=Settings(root/'config.json');cfg=settings.load();cfg['pipeline']['min_reaction_minutes']=0;cfg['pipeline']['silence']['enabled']=False;settings.save(cfg)
            class Finder:
                def __call__(self,*a):return [{'start':0.,'end':12.,'title':'Тест'}]
                def find_donations(self,segs,rows,progress,manual=None):return [{'reaction':1,'start':3.,'end':6.}]
            def speech(src,cache,cfg,progress,duration): return ([{'start':1.,'end':2.,'text':'before'},{'start':3.5,'end':4.5,'text':'donation'},{'start':8.,'end':9.,'text':'after'}],duration)
            finder=Finder();service=Service(root,settings,finder,pipeline_factory=lambda root,finder:pipeline.Pipeline(root,finder,speech=speech))
            service.queue_test_inputs()
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'): service.worker_once()
            row=service.store.list_jobs()[0];self.assertEqual(row['state'],'done',row['detail'])
            clip=next((root/'recordings').rglob('01 Тест.mp4'))
            self.assertAlmostEqual(pipeline.media_duration(clip),9,delta=.3)
            text=clip.with_suffix('.txt').read_text(encoding='utf-8-sig')
            self.assertIn('before',text);self.assertNotIn('donation',text);self.assertIn('[00:00:05] after',text)
            report=next(clip.parent.glob('* — удалённые донаты.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('00:00:03.000 — 00:00:06.000',report)
            self.assertAlmostEqual(pipeline.media_duration(src),12,delta=.3)

if __name__=='__main__':unittest.main()
