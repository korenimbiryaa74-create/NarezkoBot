import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from settings import Settings
from service import Service

class API:
    def __init__(self): self.online={}; self.error=set()
    def stream(self, name):
        if name in self.error: raise RuntimeError('private error')
        return self.online.get(name)
    def videos(self, uid): return []

class WatchDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.settings=Settings(self.root/'config.json')
        c=self.settings.load();c['monitor']['enabled']=True;c['streamers']=['kussia88','second']
        c['twitch']={'client_id':'id','client_secret':'secret'};self.cfg=self.settings.save(c)
        self.api=API();self.service=Service(self.root,self.settings,None,api_factory=lambda *a:self.api)
    def running(self):
        t=MagicMock();t.name='twitch-monitor';t.is_alive.return_value=True;self.service._threads=[t]
    def test_two_names_visible_when_stopped(self):
        rows=self.service.stream_rows(self.cfg)
        self.assertEqual([r[0] for r in rows],['kussia88','second'])
        self.assertTrue(all(r[2]=='Мониторинг остановлен' for r in rows))
    def test_unknown_then_live_offline_failure_recovery(self):
        self.running();self.assertTrue(all(r[2]=='Ожидание проверки' for r in self.service.stream_rows(self.cfg)))
        self.api.online['kussia88']={'id':'123','user_id':'9','started_at':'now'}
        self.service.monitor_once();rows=self.service.stream_rows(self.cfg)
        self.assertEqual(rows[0][:3],('kussia88','123','В эфире'));self.assertEqual(rows[1][2],'Не в эфире')
        self.api.error.add('kussia88');self.service.monitor_once();rows=self.service.stream_rows(self.cfg)
        self.assertEqual(rows[0][2],'Ошибка проверки');self.assertNotIn('private',str(rows))
        self.api.error.clear();self.api.online.clear();self.service.monitor_once()
        self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Не в эфире')
        self.service.settings_changed();self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Ожидание проверки')
    def test_stale_and_stopped_never_claim_live(self):
        self.running();self.service._watch_update('kussia88','live',0,'123')
        self.service._watch_state['kussia88']['checked_at']=time.time()-9999
        self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Нет свежих данных')
        self.service.stop_event.set();self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Мониторинг остановлен')
    def test_removed_streamer_pending_visible_after_restart(self):
        self.api.online['kussia88']={'id':'123','user_id':'9','started_at':'now'}
        mon=self.service._get_monitor(self.cfg);mon.tick('kussia88');self.api.online.clear();mon.tick('kussia88');mon.tick('kussia88')
        c=dict(self.cfg);c['streamers']=['second'];restarted=Service(self.root,self.settings,None)
        rows=restarted.stream_rows(c)
        self.assertEqual(rows[0][0],'second');self.assertEqual(rows[1][:2],('kussia88','123'))
        self.assertIn('Ожидает запись',rows[1][2])
    def test_skip_is_durable_and_specific_to_one_stream(self):
        self.running();login='kussia88'
        self.api.online[login]={'id':'123','user_id':'9','started_at':'now'}
        self.service.monitor_once()
        self.assertTrue(self.service.set_stream_skipped(login,'123',True))
        self.service=Service(self.root,self.settings,None,api_factory=lambda *a:self.api);self.running()
        self.service.monitor_once();self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Эфир пропускается')
        self.api.videos=MagicMock(return_value=[])
        self.api.online.clear();self.service.monitor_once();self.service.monitor_once()
        self.api.videos.assert_not_called()
        self.assertFalse(self.service.set_stream_skipped(login,'123',False))
        self.api.online[login]={'id':'123','user_id':'9','started_at':'now'}
        self.service.monitor_once();self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Эфир пропускается')
        self.api.online[login]['id']='124';self.service.monitor_once()
        self.assertEqual(self.service.stream_rows(self.cfg)[0][:3],(login,'124','В эфире'))
        self.assertFalse(self.service.set_stream_skipped(login,'123',False))
        self.api.videos.return_value=[{'stream_id':'124','id':'200','duration':'2h'}]
        self.service._monitor.stability_seconds=0
        self.api.online.clear()
        for _ in range(3):self.service.monitor_once()
        self.assertEqual([j['vod_id'] for j in self.service.store.list_jobs()],['200'])

    def test_unskip_while_live_restores_vod_tracking(self):
        self.running();login='kussia88'
        self.api.online[login]={'id':'123','user_id':'9','started_at':'now'}
        self.service.monitor_once()
        self.assertFalse(self.service.set_stream_skipped('second','123',True))
        self.assertTrue(self.service.set_stream_skipped(login,'123',True))
        self.assertTrue(self.service.set_stream_skipped(login,'123',False))
        self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'В эфире')
        self.api.online.clear();self.service.monitor_once();self.service.monitor_once()
        self.assertIn(login,self.service._monitor.pending_logins())

    def test_late_result_after_settings_change_not_used(self):
        self.service.settings_changed();self.service._watch_update('kussia88','live',0,'123')
        self.running();self.assertEqual(self.service.stream_rows(self.cfg)[0][2],'Ожидание проверки')

if __name__=='__main__':unittest.main()
