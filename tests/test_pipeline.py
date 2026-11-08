import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pipeline

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.downloads=[]; self.stages=[]
    def download(self,vod,quality,out,**kw):
        self.downloads.append((quality,kw.get('begin'),kw.get('end')))
        Path(out).write_bytes(b'x'*2000)
    def render(self,src,out,row,offset,cfg):
        Path(out).write_bytes(b'x'*2000)
        return [(0.2,2.0),(4.0,6.0)],3.8
    def speech(self,src,cache,cfg,progress,duration=None):
        return ([{'start':10.3,'end':11,'text':'один'},{'start':14.2,'end':15.,'text':'два'}],100.)
    def finder(self,*a): return [{'start':10.2,'end':16.,'title':'Тест'}]
    def run_it(self,finder=None,renderer=None):
        worker=pipeline.Pipeline(self.root,finder or self.finder,self.download,renderer or self.render,self.speech)
        with patch.object(pipeline,'media_duration',return_value=100.):
            return worker.run({'vod_id':'123','streamer':'name'}, {'quality':'1080p60','min_reaction_minutes':0},lambda a,b:self.stages.append((a,b)))
    def test_pipeline_saves_media_and_edited_clock(self):
        result=self.run_it()
        self.assertEqual(result['reactions'],1)
        self.assertEqual(self.downloads,[('160p30',None,None),('1080p60',10,16)])
        out=Path(result['outputs'][0]); self.assertTrue(out.exists())
        txt=out.with_suffix('.txt').read_text(encoding='utf-8-sig')
        self.assertIn('00:00:02',txt); self.assertIn('два',txt)
        self.assertTrue(out.with_suffix('.pipeline.json').exists())
        self.assertEqual(self.stages[-1][0],'Готово')
    def test_finished_clip_reused_without_redownload(self):
        self.run_it(); self.run_it()
        self.assertEqual(len(self.downloads),2)
    def test_failed_render_is_not_exposed_as_completed(self):
        def fail(*a): raise RuntimeError('renderer failed')
        with self.assertRaises(RuntimeError): self.run_it(renderer=fail)
        self.assertEqual(list((self.root/'recordings').rglob('*.pipeline.json')),[])
        self.assertFalse(any(p.parent.name!='_work' and p.name.endswith('.mp4') for p in (self.root/'recordings').rglob('*.mp4')))
    def test_bad_response_stops_before_clips(self):
        with self.assertRaises(pipeline.PipelineError): self.run_it(finder=lambda *a:[{'start':99,'end':101}])
        self.assertEqual(len(self.downloads),1)
    def test_empty_reaction_result_is_not_error(self):
        result=self.run_it(finder=lambda *a:[])
        self.assertEqual(result['outputs'],[]); self.assertEqual(result['reactions'],0)
    def test_speech_is_chunked_and_cached(self):
        video=self.root/'media.mp4'; video.write_bytes(b'x'*2000); calls=[]
        def fake(*a,**kw):
            calls.append((kw['start'],kw['end']))
            return [{'start':kw['start']+1,'end':kw['start']+2,'text':'слово'}]
        for _ in range(2):
            rows,duration=pipeline.speech_chunks(video,self.root/'cache',{},lambda *a:None,transcriber=fake,duration=1300)
            self.assertEqual(len(rows),3)
        self.assertEqual(calls,[(0,600),(600,1200),(1200,1300)])
    def test_invalid_local_segment_retries_in_halves_and_caches(self):
        import whisper_cpp
        video=self.root/'media.mp4';video.write_bytes(b'x'*2000);calls=[]
        def fake(*a,**kw):
            start,end=kw['start'],kw['end'];calls.append((start,end))
            if end-start==600: raise whisper_cpp.InvalidWhisperSegments('bad timings')
            return [{'start':start+1,'end':start+2,'text':'part'}]
        rows,_=pipeline.speech_chunks(video,self.root/'cache',{'groq_enabled':False},lambda *a:None,transcriber=fake,duration=600)
        self.assertEqual(calls,[(0,600),(0,300),(300,600)]);self.assertEqual([r['start'] for r in rows],[1,301])
        pipeline.speech_chunks(video,self.root/'cache',{'groq_enabled':False},lambda *a:None,transcriber=fake,duration=600)
        self.assertEqual(len(calls),3)
    def test_half_cache_recovers_after_failure_without_reprocessing(self):
        import whisper_cpp
        video=self.root/'media.mp4';video.write_bytes(b'x'*2000);calls=[];fail=[True]
        def fake(*a,**kw):
            start,end=kw['start'],kw['end'];calls.append((start,end))
            if end-start==600: raise whisper_cpp.InvalidWhisperSegments('bad')
            if start==300 and fail[0]: raise whisper_cpp.InvalidWhisperSegments('bad half')
            return [{'start':start+1,'end':start+2,'text':'part'}]
        def run():return pipeline.speech_chunks(video,self.root/'cache',{'groq_enabled':False},lambda *a:None,transcriber=fake,duration=600)
        with self.assertRaises(whisper_cpp.InvalidWhisperSegments):run()
        self.assertEqual(len(list((self.root/'cache').rglob('0000.json'))),0)
        fail[0]=False;run()
        self.assertEqual(calls,[(0,600),(0,300),(300,600),(300,600)])
    def test_non_timing_local_error_does_not_split(self):
        import transcribe
        video=self.root/'media.mp4';video.write_bytes(b'x'*2000);calls=[]
        def fake(*a,**kw):calls.append(1);raise transcribe.TranscribeError('no model')
        with self.assertRaises(transcribe.TranscribeError):
            pipeline.speech_chunks(video,self.root/'cache',{'groq_enabled':False},lambda *a:None,transcriber=fake,duration=600)
        self.assertEqual(len(calls),1)
    def test_bad_overlap_is_rejected(self):
        with self.assertRaises(pipeline.PipelineError):
            pipeline.validate_intervals([{'start':1,'end':10},{'start':9,'end':11}],100)
if __name__=='__main__': unittest.main()

class IncompleteDownloadTests(unittest.TestCase):
    def test_incomplete_download_not_promoted_and_retry_redownloads(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); calls=[]
            def download(vod,quality,out,**kw):
                calls.append(quality); Path(out).write_bytes(b'x'*2000)
            def speech(*args, **kwargs): raise RuntimeError('speech reached')
            engine=pipeline.Pipeline(root,lambda *a:[],download=download,speech=speech)
            job={'vod_id':'123','streamer':'name','duration_seconds':100}
            with patch.object(pipeline,'media_duration',return_value=40):
                with self.assertRaises(pipeline.IncompleteVod): engine.run(job,{})
            work=root/'recordings'/'name VOD 123'/'_work'
            self.assertFalse((work/'full-160p.mp4').exists())
            with patch.object(pipeline,'media_duration',return_value=100):
                with self.assertRaisesRegex(RuntimeError,'speech reached'): engine.run(job,{})
            self.assertEqual(calls,['160p30','160p30'])

    def test_short_old_cache_preserved_and_other_errors_do_not_move_it(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); work=root/'recordings'/'name VOD 123'/'_work'; work.mkdir(parents=True)
            src=work/'full-160p.mp4'; src.write_bytes(b'x'*2000)
            engine=pipeline.Pipeline(root,lambda *a:[])
            job={'vod_id':'123','streamer':'name','duration_seconds':100}
            with patch.object(pipeline,'media_duration',side_effect=pipeline.PipelineError('ffprobe')):
                with self.assertRaises(pipeline.PipelineError): engine.run(job,{})
            self.assertTrue(src.exists())
            with patch.object(pipeline,'media_duration',return_value=40):
                with self.assertRaises(pipeline.IncompleteVod): engine.run(job,{})
            self.assertFalse(src.exists()); self.assertEqual(len(list(work.glob('full-160p.incomplete-*.mp4'))),1)
