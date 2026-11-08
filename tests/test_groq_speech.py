import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests
import groq_speech
import pipeline
from settings import Settings, import_legacy_groq_key
from service import Service

class GroqTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.video=self.root/'video.mp4'; self.video.write_bytes(b'x'*2000)
        self.events=[]; self.calls=[]
        self.cfg={'groq_enabled':True,'groq_api_key':'fake-secret'}
    def progress(self,*args): self.events.append(args)
    def local(self,*a,**kw):
        self.calls.append(('local',kw['start']))
        return [{'start':kw['start']+1,'end':kw['start']+2,'text':'local'}]
    def groq(self,*a,**kw):
        self.calls.append(('groq',kw['start']))
        return [{'start':kw['start']+1,'end':kw['start']+2,'text':'cloud'}]
    def run_speech(self, groq=None):
        return pipeline.speech_chunks(self.video,self.root/'cache',self.cfg,self.progress,transcriber=self.local,groq_transcriber=groq or self.groq,duration=1300)
    def test_groq_first_and_resume_cache(self):
        rows,_=self.run_speech(); self.assertEqual([r['text'] for r in rows],['cloud']*3)
        self.run_speech(); self.assertEqual(self.calls,[('groq',0),('groq',600),('groq',1200)])
        for p in (self.root/'cache').rglob('*.json'): self.assertNotIn('fake-secret',p.read_text())
    def test_fallback_remaining_chunks_and_reuse(self):
        def fail_second(*a,**kw):
            if kw['start']==600: raise groq_speech.GroqError('Groq вернул HTTP 429')
            return self.groq(*a,**kw)
        rows,_=self.run_speech(fail_second)
        self.assertEqual([r['text'] for r in rows],['cloud','local','local'])
        self.assertIn('HTTP 429',str(self.events)); self.assertEqual(self.calls,[('groq',0),('local',600),('local',1200)])
        self.run_speech(); self.assertEqual(len(self.calls),3)
    def test_no_key_skips_network(self):
        self.cfg['groq_api_key']=''; self.run_speech(); self.assertEqual([x[0] for x in self.calls],['local']*3)
    def test_invalid_groq_segment_falls_back_without_cache_poisoning(self):
        rows,_=self.run_speech(lambda *a,**kw:[{'start':float('nan'),'end':2,'text':'bad'}])
        self.assertEqual([r['text'] for r in rows],['local']*3)
        self.assertEqual(len(list((self.root/'cache').rglob('*.json'))),3)
    def test_corrupt_local_cache_is_recomputed(self):
        self.cfg['groq_enabled']=False; self.run_speech()
        file=next((self.root/'cache').rglob('0000.json')); data=json.loads(file.read_text()); data['segments']=[{'start':99999,'end':99999,'text':'bad'}];file.write_text(json.dumps(data))
        self.calls=[]; self.run_speech(); self.assertEqual(self.calls,[('local',0)])
    def test_legacy_import_only_key(self):
        path=self.root/'old.json'; path.write_text(json.dumps({'timecodes':{'groq_api_key':' fake-secret '},'other':'private'}))
        self.assertEqual(import_legacy_groq_key(path),'fake-secret')
        path.write_text('{}')
        with self.assertRaises(ValueError): import_legacy_groq_key(path)
    def test_parse_rejects_bad_json_and_out_of_range(self):
        for payload in ({},{'segments':[{'start':1,'end':1000,'text':'x'}]},{'segments':[{'start':0,'end':1,'text':None}]}):
            with self.assertRaises(groq_speech.GroqError): groq_speech.parse_segments(payload,600,100)
        self.assertEqual(groq_speech.parse_segments({'segments':[{'start':1,'end':2,'text':' x '}]},600,100),[{'start':601.,'end':602.,'text':'x'}])
    def test_real_ffmpeg_and_mock_http(self):
        media=self.root/'sample.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=500:duration=3','-y',str(media)],check=True)
        session=MagicMock(); session.proxies={}; response=MagicMock(status_code=200); response.json.return_value={'segments':[{'start':0.25,'end':1.5,'text':'hi'}]};session.post.return_value=response
        with patch.object(groq_speech.requests,'Session',return_value=session):
            rows=groq_speech.transcribe_chunk(str(media),start=1,end=3,api_key='fake-secret',proxy_url='socks5://example.com:1080')
        self.assertEqual(rows[0]['start'],1.25); self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies['https'],'socks5h://example.com:1080');session.close.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs['data']['response_format'],'verbose_json')
    def test_network_error_does_not_leak_secret_or_retry(self):
        media=self.root/'sample.wav'; subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-y',str(media)],check=True)
        session=MagicMock();session.proxies={};session.post.side_effect=requests.ConnectionError('fake-secret')
        with patch.object(groq_speech.requests,'Session',return_value=session):
            with self.assertRaises(groq_speech.GroqError) as exc: groq_speech.transcribe_chunk(str(media),start=0,end=2,api_key='fake-secret')
        self.assertNotIn('fake-secret',str(exc.exception));session.post.assert_called_once()
    def test_403_code_allowlist_only(self):
        media=self.root/'sample.wav'; subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-y',str(media)],check=True)
        session=MagicMock();session.proxies={}; response=MagicMock(status_code=403);session.post.return_value=response
        response.json.return_value={'error':{'code':'model_permission_blocked_project','message':'fake-secret'}}
        with patch.object(groq_speech.requests,'Session',return_value=session):
            with self.assertRaises(groq_speech.GroqError) as exc: groq_speech.transcribe_chunk(str(media),start=0,end=2,api_key='fake-secret')
        self.assertIn('модель запрещена проектом',str(exc.exception)); self.assertNotIn('fake-secret',str(exc.exception))
        response.json.return_value={'error':{'code':'fake-secret','message':'fake-secret'}}
        with patch.object(groq_speech.requests,'Session',return_value=session):
            with self.assertRaises(groq_speech.GroqError) as exc: groq_speech.transcribe_chunk(str(media),start=0,end=2,api_key='fake-secret')
        self.assertNotIn('fake-secret',str(exc.exception)); self.assertIn('HTTP 403',str(exc.exception))
    def test_service_masks_groq_secret_and_passes_proxy(self):
        settings=Settings(self.root/'config.json'); cfg=settings.load(); cfg['pipeline']['speech']['groq_api_key']='fake-secret'
        cfg['proxy'].update(enabled=True,url='http://example.com:8080',groq=True); settings.save(cfg)
        class Engine:
            def __init__(self,*args): pass
            def run(self,job,cfg,progress):
                assert cfg['speech']['groq_proxy_url']=='http://example.com:8080'
                progress('Транскрибация','fake-secret'); raise RuntimeError('fake-secret')
        svc=Service(self.root,settings,None,pipeline_factory=Engine);job,_=svc.store.add('123','nix');svc.worker_once()
        for text in [str(svc.logs()),str(svc.store.events(job['id'])),str(svc.store.list_jobs())]: self.assertNotIn('fake-secret',text)
