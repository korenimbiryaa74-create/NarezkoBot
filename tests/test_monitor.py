import tempfile
import unittest
from pathlib import Path
from job_store import JobStore
from monitor import Monitor, parse_twitch_duration

class Fake:
    live=None
    archive=[]
    fail=False
    def stream(self, login):
        if self.fail: raise RuntimeError('network')
        return self.live
    def videos(self, user_id): return self.archive

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=JobStore(Path(self.tmp.name)/'jobs.db')
        self.api=Fake(); self.now=1000
        self.monitor=Monitor(self.store,self.api,stability_seconds=120,now=lambda:self.now)
        self.live={'id':'stream-1','user_id':'u1','started_at':'2026-01-01T00:00:00Z','title':'Тест'}
    def test_error_does_not_mean_offline(self):
        self.api.live=self.live; self.monitor.tick('NiX')
        self.api.fail=True
        with self.assertRaises(RuntimeError): self.monitor.tick('nix')
        self.assertEqual(self.monitor.status()[0]['state'],'live')
    def test_matching_stable_vod_and_restart(self):
        self.api.live=self.live; self.monitor.tick('nix')
        self.api.live=None; self.monitor.tick('nix'); self.monitor.tick('nix')
        self.api.archive=[{'id':'11','stream_id':'old','duration':'4h'}, {'id':'12','stream_id':'stream-1','duration':'1h'}]
        self.monitor.tick('nix'); self.now+=121
        self.monitor=Monitor(JobStore(self.store.path),self.api,stability_seconds=120,now=lambda:self.now)
        self.monitor.tick('nix'); self.monitor.tick('nix')
        self.assertEqual([j['vod_id'] for j in self.store.list_jobs()],['12'])
        self.assertEqual(self.monitor.status()[0]['state'],'queued')
    def test_duration_change_resets_stability(self):
        self.api.live=self.live; self.monitor.tick('nix'); self.api.live=None
        self.monitor.tick('nix'); self.monitor.tick('nix')
        self.api.archive=[{'id':'12','stream_id':'stream-1','duration':'1h'}]
        self.monitor.tick('nix'); self.now+=121
        self.api.archive[0]['duration']='2h'; self.monitor.tick('nix')
        self.assertEqual(self.store.list_jobs(),[])
        self.now+=121; self.monitor.tick('nix'); self.assertEqual(len(self.store.list_jobs()),1)
    def test_unmatched_video_never_queued(self):
        self.api.live=self.live; self.monitor.tick('nix'); self.api.live=None
        self.monitor.tick('nix'); self.monitor.tick('nix')
        self.api.archive=[{'id':'99','stream_id':'another','duration':'2h'}]
        self.now+=2000; self.monitor.tick('nix')
        self.assertEqual(self.store.list_jobs(),[])
if __name__=='__main__': unittest.main()

class DurationTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_twitch_duration('1h2m3s'),3723)
        self.assertEqual(parse_twitch_duration('2h'),7200)
        self.assertEqual(parse_twitch_duration('19s'),19)
        for bad in ('', '0s', '3m2h', '-1s', '1hgarbage'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): parse_twitch_duration(bad)

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=JobStore(Path(self.tmp.name)/"jobs.db")
        self.api=Fake(); self.now=1000
        self.monitor=Monitor(self.store,self.api,stability_seconds=120,now=lambda:self.now)
        self.live={"id":"stream-1","user_id":"u1","started_at":"2026-01-01T00:00:00Z","title":"Тест"}
    def test_duration_reaches_queue_and_missing_candidate_resets_stability(self):
        self.api.live=self.live; self.monitor.tick('nix'); self.api.live=None
        self.monitor.tick('nix'); self.monitor.tick('nix')
        self.api.archive=[{'id':'12','stream_id':'stream-1','duration':'1h2m3s'}]
        self.monitor.tick('nix'); self.now+=300
        self.api.archive=[]; self.monitor.tick('nix')
        self.api.archive=[{'id':'12','stream_id':'stream-1','duration':'1h2m3s'}]
        self.monitor.tick('nix'); self.assertEqual(self.store.list_jobs(),[])
        self.now+=121; self.monitor.tick('nix')
        self.assertEqual(self.store.list_jobs()[0]['duration_seconds'],3723)
