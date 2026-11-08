import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import manual_blur
from monitor import Monitor
from service import Service
from settings import Settings

class API:
    live=None
    archive=[]
    failure=False
    def stream(self, login):
        if self.failure: raise RuntimeError('secret should never appear')
        return self.live
    def videos(self, uid): return self.archive

class MonitorNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.settings=Settings(self.root/'config.json')
        cfg=self.settings.load();cfg['monitor']['enabled']=True
        cfg['twitch']={'client_id':'id','client_secret':'secret'};cfg['streamers']=['kussia88'];self.settings.save(cfg)
        self.api=API()
        self.svc=Service(self.root,self.settings,None,api_factory=lambda *args:self.api)
        self.mon=self.svc._get_monitor(cfg);self.now=1000;self.mon.now=lambda:self.now
        self.live={'id':'1234','user_id':'9','started_at':'2026-01-01T00:00:00Z','title':'Stream'}
    def messages(self):return [x['message'] for x in reversed(self.svc.store.notifications())]
    def test_lifecycle_survives_restart_and_never_duplicates(self):
        self.api.live=self.live;self.svc.monitor_once();self.svc.monitor_once()
        self.api.failure=True;self.svc.monitor_once();self.svc.monitor_once()
        self.assertEqual(self.mon.status()[0]['state'],'live')
        self.api.failure=False;self.api.live=None;self.svc.monitor_once();self.svc.monitor_once()
        self.api.archive=[{'id':'40','stream_id':'old','duration':'1h'}, {'id':'41','stream_id':'1234','duration':'2h'}]
        self.svc.monitor_once();self.now+=121;self.svc.monitor_once()
        self.assertEqual([j['vod_id'] for j in self.svc.store.list_jobs()],['41'])
        class Engine:
            def run(self,job,cfg,progress):return {'outputs':['a','b'],'folder':'safe'}
        self.svc.pipeline_factory=lambda *args:Engine()
        self.svc.worker_once()
        msgs=self.messages()
        for needle in ('обнаружен эфир','мониторинг Twitch недоступен','мониторинг Twitch восстановлен','эфир закончился','запись найдена','началась обработка','готовых нарезок: 2'):
            self.assertEqual(sum(needle in m for m in msgs),1,(needle,msgs))
        self.assertFalse(any('secret' in m for m in msgs))
        restored=Service(self.root,self.settings,None,api_factory=lambda *args:self.api)
        restored.monitor_once()
        self.assertEqual(len(restored.store.notifications()),len(msgs))
        self.assertEqual(restored.store.list_jobs()[0]['state'],'done')
    def test_pending_wait_and_clear_preserves_dedup_and_queue(self):
        self.api.live=self.live;self.mon.tick('kussia88');self.api.live=None
        self.mon.tick('kussia88');self.mon.tick('kussia88');self.now+=21601
        self.mon.tick('kussia88');self.mon.tick('kussia88')
        self.assertEqual(sum('6 часов' in m for m in self.messages()),1)
        self.svc.store.clear_notifications();self.assertEqual(self.messages(),[])
        self.mon.tick('kussia88');self.assertEqual(self.messages(),[])
        self.api.archive=[{'id':'41','stream_id':'1234','duration':'2h'}]
        self.mon.tick('kussia88');self.now+=121;self.mon.tick('kussia88')
        self.assertEqual(len(self.svc.store.list_jobs()),1)
        self.assertTrue(any('запись найдена' in m for m in self.messages()))
    def test_pending_vod_is_kept_after_removing_streamer_and_restarting(self):
        self.api.live=self.live;self.mon.tick('kussia88');self.api.live=None;self.mon.tick('kussia88');self.mon.tick('kussia88')
        cfg=self.settings.load();cfg['streamers']=[];self.settings.save(cfg)
        self.api.archive=[{'id':'41','stream_id':'1234','duration':'2h'}]
        restarted=Service(self.root,self.settings,None,api_factory=lambda *args:self.api)
        mon=restarted._get_monitor(cfg);mon.now=lambda:self.now
        restarted.monitor_once();self.now+=121;restarted.monitor_once()
        self.assertEqual(restarted.store.list_jobs()[0]['vod_id'],'41')
    def test_profile_list_and_unfinished_failure(self):
        manual_blur.save_profile(self.root,'NAME',[]);manual_blur.save_profile(self.root,'other',[[0,0,400,400]])
        self.assertEqual(manual_blur.profile_logins(self.root),['name','other'])
        class Engine:
            def run(self,job,cfg,progress):raise ValueError('do not show me')
        self.svc.pipeline_factory=lambda *args:Engine()
        self.svc.store.add('888','name');self.svc.worker_once()
        messages=self.messages();self.assertTrue(any('обработка остановлена' in m for m in messages))
        self.assertFalse(any('do not show me' in m for m in messages))

if __name__=='__main__':unittest.main()
