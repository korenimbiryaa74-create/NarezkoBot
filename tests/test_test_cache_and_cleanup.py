import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pipeline
from job_store import JobStore
from service import Service
from settings import Settings
from monitor import Monitor
from test_diagnostics_ui import App

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'test_input').mkdir()
        self.video=self.root/'test_input'/'тест.mp4'; self.video.write_bytes(b'original video')
        self.store=JobStore(self.root/'state'/'jobs.sqlite3')
        self.calls=[]; self.logs=[]
        self.cfg={'min_reaction_minutes':0,'speech':{'groq_enabled':False}}
    def add(self):
        st=self.video.stat()
        self.store.add_local(self.video.name,st.st_size,st.st_mtime_ns)
        return self.store.claim()
    def transcriber(self,*args,**kw):
        self.calls.append((kw['start'],kw['end']))
        return [{'start':kw['start']+1,'end':kw['start']+2,'text':'распознано'}]
    def run_job(self,job):
        engine=pipeline.Pipeline(self.root,lambda *a:[])
        with patch('pipeline.media_duration',return_value=900), patch('whisper_cpp.transcribe_file',side_effect=self.transcriber):
            return engine.run(job,self.cfg,lambda stage,detail:self.logs.append(detail))
    def test_repeated_test_reuses_cache_after_history_cleanup(self):
        first=self.add(); self.run_job(first); self.assertEqual(len(self.calls),2)
        self.store.finish(first['id'],'done'); self.store.clear_history()
        second=self.add(); self.run_job(second)
        self.assertNotEqual(first['vod_id'],second['vod_id'])
        self.assertEqual(len(self.calls),2)
        self.assertTrue(any('кэш' in x for x in self.logs))
        self.assertTrue(self.video.exists())
    def test_groq_cache_reused_for_new_test(self):
        self.cfg['speech']={'groq_enabled':True,'groq_api_key':'fake-key'}
        def cloud(*args,**kw):
            self.calls.append(kw['start'])
            return [{'start':kw['start']+1,'end':kw['start']+2,'text':'облако'}]
        def run(job):
            engine=pipeline.Pipeline(self.root,lambda *a:[])
            with patch('pipeline.media_duration',return_value=900), patch('groq_speech.transcribe_chunk',side_effect=cloud):
                engine.run(job,self.cfg,lambda stage,detail:self.logs.append(detail))
        first=self.add(); run(first); self.store.finish(first['id'],'done')
        run(self.add()); self.assertEqual(self.calls,[0,600])
        self.assertTrue(any('Groq (кэш)' in x for x in self.logs))
        for file in (self.root/'state'/'test_transcripts').rglob('*.json'):
            self.assertNotIn('fake-key',file.read_text())

    def test_same_size_and_mtime_but_changed_content_is_not_reused(self):
        first=self.add(); self.run_job(first); self.store.finish(first['id'],'done')
        st=self.video.stat(); self.video.write_bytes(b'changed video!'); os.utime(self.video,ns=(st.st_atime_ns,st.st_mtime_ns))
        self.assertEqual(self.video.stat().st_size,st.st_size)
        second=self.add(); self.run_job(second); self.assertEqual(len(self.calls),4)
    def test_legacy_cache_migrates_for_matching_test_including_hidden(self):
        first=self.add(); old=self.root/'recordings'/f"Тест {pipeline.safe_name(self.video.stem)} {first['vod_id']}"/'_work'/'transcripts'
        with patch('whisper_cpp.transcribe_file',side_effect=self.transcriber):
            pipeline.speech_chunks(self.video,old,self.cfg['speech'],lambda *a:None,duration=900)
        self.store.finish(first['id'],'done'); self.store.clear_history()
        second=self.add(); self.run_job(second)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(any('Перенесён кэш' in x for x in self.logs))
    def test_legacy_identity_is_not_imported_to_another_content_hash(self):
        first=self.add(); old=self.root/'recordings'/f"Тест {pipeline.safe_name(self.video.stem)} {first['vod_id']}"/'_work'/'transcripts'
        with patch('whisper_cpp.transcribe_file',side_effect=self.transcriber):
            pipeline.speech_chunks(self.video,old,self.cfg['speech'],lambda *a:None,duration=900)
        self.store.finish(first['id'],'done'); second=self.add(); self.run_job(second)
        self.store.finish(second['id'],'done')
        st=self.video.stat(); self.video.write_bytes(b'changed video!'); os.utime(self.video,ns=(st.st_atime_ns,st.st_mtime_ns))
        self.run_job(self.add()); self.assertEqual(len(self.calls),4)

    def test_changed_video_does_not_import_legacy_cache(self):
        first=self.add(); old=self.root/'recordings'/f"Тест {pipeline.safe_name(self.video.stem)} {first['vod_id']}"/'_work'/'transcripts'
        with patch('whisper_cpp.transcribe_file',side_effect=self.transcriber):
            pipeline.speech_chunks(self.video,old,self.cfg['speech'],lambda *a:None,duration=900)
        self.store.finish(first['id'],'done'); self.video.write_bytes(b'different video length')
        second=self.add(); self.run_job(second); self.assertEqual(len(self.calls),4)

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.store=JobStore(self.root/'state'/'jobs.sqlite3')
    def test_history_and_queue_keep_running_stream_tracking_and_dedup(self):
        a,_=self.store.add('1','nix'); self.store.claim(); self.store.finish(a['id'],'done')
        b,_=self.store.add('2','nix'); self.store.claim(); self.store.finish(b['id'],'failed')
        c,_=self.store.add('3','nix'); self.store.claim()
        d,_=self.store.add('4','nix')
        mon=Monitor(self.store,object());
        with self.store._connect() as db:
            db.execute("INSERT INTO observed_streams(stream_id,login,user_id,title,started_at,state,updated_at) VALUES ('abc','nix','1','t','t','waiting_vod',0)")
        self.assertEqual(self.store.clear_history(),2)
        self.assertEqual({r['id'] for r in self.store.list_jobs()},{c['id'],d['id']})
        self.assertEqual(self.store.clear_queue(),1)
        self.assertEqual([r['id'] for r in self.store.list_jobs()],[c['id']])
        self.assertEqual(mon.status()[0]['state'],'waiting_vod')
        self.assertFalse(self.store.add('1','nix')[1]); self.assertFalse(self.store.add('4','nix')[1])
        self.store.finish(c['id'],'done'); self.assertEqual(self.store.clear_history(),1)
        self.assertEqual(self.store.list_jobs(),[])
        self.assertEqual(self.store.clear_queue(),0)
    def test_retry_cancelled_and_cannot_be_claimed(self):
        a,_=self.store.add('1','nix'); self.store.claim(); self.store.finish(a['id'],'retry')
        self.assertEqual(self.store.clear_queue(),1); self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.events(a['id'])[-1]['stage'],'Отменено')
    def test_queue_clear_serializes_against_claim(self):
        import concurrent.futures, threading
        for n in range(20):
            store=JobStore(self.root/f'concurrent-{n}.sqlite3'); job,_=store.add(str(n+1),'nix')
            barrier=threading.Barrier(2)
            def claim(): barrier.wait(); return store.claim()
            def clear(): barrier.wait(); return store.clear_queue()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f=pool.submit(claim); g=pool.submit(clear); claimed=f.result(); removed=g.result()
            if claimed:
                self.assertEqual(removed,0); self.assertEqual(store.list_jobs()[0]['state'],'running')
                store.finish(job['id'],'done')
            else:
                self.assertEqual(removed,1); self.assertEqual(store.list_jobs(),[])

    def test_rejected_ui_confirmation_no_changes(self):
        app=App.__new__(App); app.service=MagicMock(); app.status=MagicMock()
        with patch.object(App.clear_queue.__globals__['messagebox'],'askyesno',return_value=False,create=True):
            app.clear_queue(); app.clear_history()
        app.service.store.clear_queue.assert_not_called(); app.service.store.clear_history.assert_not_called()
    def test_confirmed_ui_calls(self):
        app=App.__new__(App); app.service=MagicMock(); app.status=MagicMock()
        with patch.object(App.clear_queue.__globals__['messagebox'],'askyesno',return_value=True,create=True):
            app.clear_queue(); app.clear_history()
        app.service.store.clear_queue.assert_called_once(); app.service.store.clear_history.assert_called_once()

if __name__=='__main__': unittest.main()
