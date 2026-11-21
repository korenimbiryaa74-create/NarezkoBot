import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import clip_recovery as cp
import job_timing
import local_intake
import pipeline
import stream_cutter as sc
from service import Service
from settings import Settings


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)

    def test_bundle_replay_after_video_or_text_rename(self):
        for with_text in [False,True]:
            folder=self.root/str(with_text);folder.mkdir(); out=folder/'out.mp4'; rendered=folder/'staging.mp4'
            txt=folder/'out.txt';meta=folder/'out.pipeline.json';journal=folder/'journal'
            rendered.write_bytes(b'video'); m={'key':'k','duration':5.0}
            cp.save(journal,{'key':'k','output':out.name,'stamp':cp.stamp(rendered),'meta':m,'text':'тест'})
            os.replace(rendered,out)
            if with_text:txt.write_text('тест',encoding='utf-8-sig')
            cp.complete_bundle(journal,'k',rendered,out,txt,meta,lambda p:5.)
            self.assertEqual(txt.read_text('utf-8-sig'),'тест');self.assertEqual(cp.read(meta),m);self.assertFalse(journal.exists())

    def test_bundle_never_adopts_a_foreign_file(self):
        o=self.root/'o.mp4';o.write_bytes(b'foreign'); j=self.root/'journal';t=self.root/'o.txt'
        cp.save(j,{'output':o.name,'key':'k','stamp':[5,1],'meta':{'duration':1},'text':'text'})
        with self.assertRaisesRegex(RuntimeError,'изменилось'):cp.complete_bundle(j,'k',self.root/'tmp',o,t,self.root/'meta')
        self.assertEqual(o.read_bytes(),b'foreign');self.assertFalse(t.exists());self.assertTrue(j.exists())

    def test_fragment_identity_stamp_and_duration(self):
        p=self.root/'clip.mp4';p.write_bytes(b'clip');ident=['123','1080p60',0,5]
        cp.save(p.with_suffix('.source.json'),{'identity':ident,'stamp':cp.stamp(p),'duration':5})
        self.assertTrue(cp.cached_fragment(p,ident,5,lambda p:5))
        self.assertFalse(cp.cached_fragment(p,[*ident[:-1],6],5,lambda p:5))
        self.assertFalse(cp.cached_fragment(p,ident,5,lambda p:4))
        p.write_bytes(b'changed');self.assertFalse(cp.cached_fragment(p,ident,5,lambda p:5))

    def test_local_import_does_not_replace_existing(self):
        s=self.root/'demo.mp4';s.write_bytes(b'first');dest=self.root/'inputs';p=local_intake.import_file(s,dest)
        s.write_bytes(b'second');self.assertRaises(FileExistsError,local_intake.import_file,s,dest)
        self.assertEqual(p.read_bytes(),b'first');self.assertEqual(list(dest.glob('*.partial')),[])

    @unittest.skipUnless(os.name=='nt','Windows sharing modes')
    def test_open_writer_deferred_then_enqueued_once(self):
        svc=Service(self.root,Settings(self.root/'config.json'),object()); d=self.root/'test_input';d.mkdir();p=d/'demo.mp4'
        with p.open('wb') as f:
            f.write(b'part');f.flush();self.assertEqual(svc.queue_test_inputs(),(0,0));self.assertEqual(svc.pending_input_count,1)
            f.write(b'two')
        self.assertEqual(svc.queue_test_inputs(True),(1,0));self.assertEqual(svc.queue_test_inputs(True),(0,0));self.assertEqual(svc.store.claim()['local_size'],7)

    def test_timing_separates_mux_cache_and_queue_wait(self):
        events=[{'stage':s,'detail':d,'timestamp':t} for t,s,d in [(0,'Запуск',''),(1,'Скачивание','из кэша'),(3,'Рендер',''),(5,'Рендер','Склейка частей'),(8,'Ожидает повтора',''),(500,'Запуск',''),(501,'Готово','')]]
        text=job_timing.summary(events,900);self.assertIn('Склейка: 00:00:03',text);self.assertIn('Скачивание: 00:00:02 · использован кэш',text);self.assertIn('Попыток: 2',text)

    def test_exit_wakes_downloader_without_scanning(self):
        out=self.root/'raw';proc=Mock();proc.returncode=0;proc.poll.return_value=None;proc.wait.return_value=0
        with patch('pipeline.subprocess.Popen',return_value=proc),patch('pipeline._parts_state',side_effect=AssertionError('unnecessary scan')):
            self.assertEqual(pipeline._run_download(['stub'],self.root/'cli',{},out).returncode,0)
        proc.wait.assert_called_once_with(timeout=15)

    def test_longest_first_keeps_timeline_order(self):
        order=[]; jobs=[(0,1),(1,7),(7,10)]
        def fake(cmd,idx,progress,log): order.append(idx);return 0
        with patch.object(sc,'_exec',side_effect=fake): parts=sc.run_jobs('ffmpeg',jobs,lambda *a:[],sc.Progress(10,3),str(self.root),None,1)
        self.assertEqual(order,[1,2,0]);self.assertEqual([Path(p).name for p in parts],[f'part{i:05d}.mkv' for i in range(3)])

    def test_mux_reports_stderr_exit_and_disk(self):
        with patch.object(sc,'run',return_value=subprocess.CompletedProcess([],7,'','disk error')):
            with self.assertRaisesRegex(sc.CutterError,'FFmpeg 7; свободно .*disk error'):
                sc.concat_parts('ffmpeg',[str(self.root/'part00000.mkv')],str(self.root/'out.mp4'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs ffmpeg')
    def test_real_retry_after_mux_failure_does_not_encode(self):
        src=self.root/'src.mp4';out=self.root/'out.mp4'; ff=shutil.which('ffmpeg')
        subprocess.run([ff,'-nostdin','-v','error','-f','lavfi','-i','testsrc2=s=160x90:r=25','-f','lavfi','-i','sine=frequency=440','-t','4','-c:v','libx264','-threads','1','-c:a','aac',str(src)],check=True,timeout=30)
        row={'start':0,'end':3};cfg={'enabled':False,'hwaccel':'none','speed':'quality'}
        with patch.object(sc,'pick_encoder',return_value='libx264'):
            with patch.object(sc,'concat_parts',side_effect=sc.CutterError('mux')):
                with self.assertRaisesRegex(sc.CutterError,'mux'):pipeline.render_clip(src,out,row,0,cfg)
            self.assertTrue(list(self.root.rglob('parts.json')))
            with patch.object(sc,'_exec',side_effect=AssertionError('unexpected re-encode')):
                keeps,duration=pipeline.render_clip(src,out,row,0,cfg)
        self.assertTrue(out.exists());self.assertAlmostEqual(duration,3,delta=.3);self.assertFalse(list(self.root.rglob('parts.json')))
