import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_store import JobStore
from service import Service
from settings import Settings
from pipeline import Pipeline, PipelineError, media_duration
import stream_cutter

class LocalQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.folder=self.root/'test_input'; self.folder.mkdir()
        self.service=Service(self.root,Settings(self.root/'config.json'),object())

    def test_queue_scans_files_deduplicates_only_active_and_persists(self):
        (self.folder/'first.mp4').write_bytes(b'a'*2000)
        (self.folder/'second.MKV').write_bytes(b'b'*2000)
        (self.folder/'not-video.txt').write_text('no')
        (self.folder/'nested').mkdir(); (self.folder/'nested'/'x.mp4').touch()
        self.assertEqual(self.service.queue_test_inputs(),(2,0))
        self.assertEqual(self.service.queue_test_inputs(),(0,2))
        first=self.service.store.claim()
        self.assertEqual(first['local_name'],'first.mp4')
        self.service.store.finish(first['id'],'done')
        self.assertEqual(self.service.queue_test_inputs(),(1,1))
        rows=JobStore(self.service.store.path).list_jobs()
        self.assertEqual(len(rows),3); self.assertEqual(rows[0]['local_name'],'first.mp4')

    def test_invalid_names_and_changed_or_missing_input_fail_without_download(self):
        with self.assertRaises(ValueError): self.service.store.add_local('../outside.mp4',2000,0)
        path=self.folder/'first.mp4'; path.write_bytes(b'a'*2000)
        self.service.queue_test_inputs(); job=self.service.store.claim()
        path.write_bytes(b'different')
        worker=Pipeline(self.root,lambda *a:[],download=lambda *a,**k:self.fail('network download'))
        with self.assertRaisesRegex(PipelineError,'изменился'):worker.run(job,{})
        path.unlink()
        with self.assertRaisesRegex(PipelineError,'отсутствует'):worker.run(job,{})

    def test_local_source_survives_and_maps_timestamps(self):
        path=self.folder/'first.mp4'; path.write_bytes(b'a'*2000)
        self.service.queue_test_inputs(); job=self.service.store.claim(); observed=[]
        def speech(src,cache,cfg,progress,duration):
            observed.append(('speech',str(src))); return [{'start':10.5,'end':11,'text':'слово'}],100
        def finder(src,*args): observed.append(('finder',src)); return [{'start':10,'end':15,'title':'Реакция'}]
        def render(src,out,row,offset,cfg):
            self.assertEqual(Path(src),path); self.assertEqual(offset,0)
            Path(out).write_bytes(b'x'*2000); return [(10.,15.)],5.
        worker=Pipeline(self.root,finder,download=lambda *a,**k:self.fail('network download'),speech=speech,renderer=render)
        with patch('pipeline.media_duration',return_value=100):
            result=worker.run(job,{'min_reaction_minutes':0},lambda *a:None)
        self.assertTrue(path.exists()); out=Path(result['outputs'][0]); self.assertTrue(out.exists())
        self.assertIn('[00:00:00] слово',out.with_suffix('.txt').read_text(encoding='utf-8-sig'))
        self.assertEqual([x[0] for x in observed],['speech','finder'])

    def test_change_during_speech_stops_before_finder(self):
        path=self.folder/'first.mp4'; path.write_bytes(b'a'*2000)
        self.service.queue_test_inputs(); job=self.service.store.claim()
        def speech(*args,**kwargs):
            path.write_bytes(b'changed'); return [{'start':1,'end':2,'text':'x'}],10
        worker=Pipeline(self.root,lambda *a:self.fail('finder called'),speech=speech)
        with patch('pipeline.media_duration',return_value=10), self.assertRaisesRegex(PipelineError,'изменился'):
            worker.run(job,{'min_reaction_minutes':0})

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg/ffprobe unavailable')
class LocalMediaIntegrationTests(unittest.TestCase):
    def test_queue_to_real_video_and_transcript_with_stub_models(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); folder=root/'test_input'; folder.mkdir(); video=folder/'input.mp4'
            subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10',
                            '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','8',
                            '-c:v','libx264','-c:a','aac','-y',str(video)],check=True,timeout=30)
            settings=Settings(root/'config.json'); cfg=settings.load(); cfg['pipeline']['min_reaction_minutes']=0
            cfg['pipeline']['silence']['enabled']=False; settings.save(cfg)
            def speech(src,cache,cfg,progress,duration):
                progress('Транскрибация','Тестовый распознаватель')
                return [{'start':2.2,'end':2.8,'text':'Тестовая фраза'}],duration
            def finder(src,segs,duration,streamer,cfg,progress):
                return [{'start':2,'end':6,'title':'Тест'}]
            def engine(root,finder_arg): return Pipeline(root,finder,download=lambda *a,**k:self.fail('Twitch requested'),speech=speech)
            service=Service(root,settings,object(),pipeline_factory=engine)
            self.assertEqual(service.queue_test_inputs(),(1,0))
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                self.assertTrue(service.worker_once())
            row=service.store.list_jobs()[0]
            self.assertEqual(row['state'],'done',row['detail'])
            files=list((root/'recordings').rglob('*.pipeline.json')); self.assertEqual(len(files),1)
            out=files[0].parent/'Готовые нарезки'/files[0].name.replace('.pipeline.json','.mp4')
            self.assertAlmostEqual(media_duration(out),4,delta=.5)
            report=next(out.parent.glob('* — удалённая тишина.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('выключено',report)
            self.assertIn('Тестовая фраза',out.with_suffix('.txt').read_text(encoding='utf-8-sig'))
            self.assertTrue(video.exists())
            self.assertEqual(sorted(x.name for x in folder.iterdir()), ['input.mp4'])

    def test_real_silence_cut_and_transcript_alignment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); folder=root/'test_input'; folder.mkdir(); video=folder/'input.mp4'
            expression=r'aevalsrc=if(between(t\,3\,7)\,0\,0.5*sin(2*PI*440*t)):s=16000:d=12'
            subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10',
                '-f','lavfi','-i',expression,'-t','12','-c:v','libx264','-c:a','aac','-y',str(video)],check=True,timeout=30)
            settings=Settings(root/'config.json'); cfg=settings.load(); cfg['pipeline']['min_reaction_minutes']=0
            cfg['pipeline']['silence'].update(enabled=True,min_cut=1.0,max_gap=1.0); settings.save(cfg)
            def speech(src,cache,cfg,progress,duration):
                progress('Транскрибация','Тестовый распознаватель')
                return [{'start':1,'end':2,'text':'первая'},{'start':9,'end':10,'text':'вторая'}],duration
            def finder(src,segs,duration,streamer,cfg,progress):
                return [{'start':0,'end':12,'title':'Тест'}]
            service=Service(root,settings,object(),pipeline_factory=lambda root,_: Pipeline(root,finder,speech=speech))
            service.queue_test_inputs()
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                service.worker_once()
            row=service.store.list_jobs()[0]; self.assertEqual(row['state'],'done',row['detail'])
            clip=next((root/'recordings').rglob('01 Тест.mp4'))
            self.assertLess(media_duration(clip),10.5)
            text=clip.with_suffix('.txt').read_text(encoding='utf-8-sig')
            self.assertIn('[00:00:01] первая',text)
            self.assertIn('[00:00:05] вторая',text)
            self.assertEqual(sorted(x.name for x in folder.iterdir()),['input.mp4'])
