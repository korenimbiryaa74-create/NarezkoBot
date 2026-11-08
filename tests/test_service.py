import tempfile
import unittest
from pathlib import Path
from service import Service
from settings import Settings

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.settings=Settings(self.root/'config.json')
        self.cfg=self.settings.load(); self.settings.save(self.cfg)
    def test_configuration_validation_and_preservation(self):
        self.cfg['streamers']=['NiX','nix']; saved=self.settings.save(self.cfg)
        self.assertEqual(saved['streamers'],['nix'])
        original=(self.root/'config.json').read_text()
        saved['proxy']={'enabled':True,'url':'invalid'}
        with self.assertRaises(ValueError):self.settings.save(saved)
        self.assertEqual((self.root/'config.json').read_text(),original)
    def test_job_completion_and_stage(self):
        class Engine:
            def __init__(self,*a):pass
            def run(self,job,cfg,progress):
                progress('Тест','1/1'); return {'outputs':['a'],'folder':'here'}
        svc=Service(self.root,self.settings,object(),pipeline_factory=Engine)
        job,_=svc.store.add('99','nix'); self.assertTrue(svc.worker_once())
        self.assertEqual(svc.store.list_jobs()[0]['state'],'done')
        self.assertEqual(svc.store.events(job['id'])[-2]['stage'],'Тест')
    def test_failure_does_not_auto_retry(self):
        class Engine:
            def __init__(self,*a):pass
            def run(self,*a): raise RuntimeError('uncertain')
        svc=Service(self.root,self.settings,object(),pipeline_factory=Engine)
        svc.store.add('99','nix'); svc.worker_once()
        self.assertEqual(svc.store.list_jobs()[0]['state'],'failed'); self.assertFalse(svc.worker_once())
    def test_monitor_disabled_no_api(self):
        def fail(*a):raise AssertionError('not expected')
        svc=Service(self.root,self.settings,object(),api_factory=fail)
        svc.monitor_once()
if __name__=='__main__':unittest.main()

class RecoveryTests(unittest.TestCase):
    def test_safe_stage_recovers_but_model_stage_needs_review(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); s=Service(root,Settings(root/'config.json'),object())
            a,_=s.store.add('1','a'); b,_=s.store.add('2','b')
            s.store.claim();s.store.update_stage(a['id'],'Скачивание')
            s.store.claim();s.store.update_stage(b['id'],'Поиск реакций')
            s.store.recover_interrupted(safe_stages=('Скачивание',))
            rows={r['id']:r for r in s.store.list_jobs()}
            self.assertEqual(rows[a['id']]['state'],'retry')
            self.assertEqual(rows[b['id']]['state'],'failed')

class DiagnosticTests(unittest.TestCase):
    def test_retry_keeps_and_logs_redacted_transcription_error(self):
        from transcribe import TranscribeError
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); settings=Settings(root/'config.json'); cfg=settings.load()
            cfg['twitch']['client_secret']='my-private-secret'
            cfg['proxy'].update(enabled=True,url='socks5://user:password@example.com:1080')
            settings.save(cfg)
            class Engine:
                def __init__(self,*a):pass
                def run(self,job,cfg,progress):
                    progress('Транскрибация','часть 1')
                    raise TranscribeError('CUDA failure my-private-secret socks5://user:password@example.com:1080')
            service=Service(root,settings,object(),pipeline_factory=Engine)
            service.store.add('1','nix'); service.worker_once()
            row=service.store.list_jobs()[0]; log='\n'.join(s for _,s in service.logs())
            self.assertEqual(row['state'],'retry')
            self.assertIn('CUDA failure',row['detail']); self.assertIn('CUDA failure',log)
            for text in (row['detail'],log):
                self.assertNotIn('my-private-secret',text)
                self.assertNotIn('password',text)
