import importlib
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import youtube_studio as studio

CHANNEL = 'UC'+'x'*22

def stub(name, **attributes):
    module=types.ModuleType(name)
    for key,value in attributes.items(): setattr(module,key,value)
    return module

class Pipeline: pass
class InvalidRenderTimeline(Exception): pass
class BannerSetupError(Exception): pass
class BannerResponseError(Exception): pass
class FakeSettings:
    def __init__(self):
        self.cfg={'youtube':{'enabled':True},'pipeline':{'speech':{}},
                  'twitch':{'client_secret':''},'proxy':{'url':'','enabled':False}}
    def load(self): return self.cfg

class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        modules={
            'monitor':stub('monitor',Monitor=object,TwitchClient=object),
            'pipeline':stub('pipeline',Pipeline=Pipeline,atomic_json=lambda *a:None,
                manual_answer_path=lambda *a:None,manual_donation_path=lambda *a:None,
                job_identity=lambda *a:None,organize_completed=lambda *a:None),
            'reactions':stub('reactions'),
            'stream_cutter':stub('stream_cutter',InvalidRenderTimeline=InvalidRenderTimeline),
            'bot_debug':stub('bot_debug',redact=str),
            'banner_blur':stub('banner_blur',BannerSetupError=BannerSetupError,BannerResponseError=BannerResponseError),
        }
        self.patcher=patch.dict(sys.modules,modules)
        self.patcher.start();self.addCleanup(self.patcher.stop)
        sys.modules.pop('service',None)
        self.service=importlib.import_module('service')
        self.addCleanup(sys.modules.pop,'service',None)

    def make_service(self,local=False):
        root=self.root
        class Engine:
            def __init__(self,*args):pass
            def run(self,job,cfg,progress):
                files=[]
                for i in range(3):
                    p=root/f'clip {i}.mp4';p.write_bytes(b'video'+bytes([i]));files.append(str(p))
                return {'outputs':files,'folder':str(root)}
        svc=self.service.Service(root,FakeSettings(),object(),pipeline_factory=Engine)
        profile='__test__' if local else 'kussia88'
        svc.youtube_queue.bind(profile,CHANNEL,16,audience='not_kids')
        if local: svc.store.add_local('source.mp4',42,1)
        else: svc.store.add('123','kussia88')
        return svc

    def test_completed_job_enqueues_then_publishes_to_bound_channel(self):
        svc=self.make_service()
        self.assertTrue(svc.worker_once())
        self.assertEqual(svc.store.list_jobs()[0]['state'],'done')
        uploads=svc.youtube_queue.list_uploads()
        self.assertEqual([u['profile'] for u in uploads],['kussia88']*3)
        calls=[]
        class Uploader:
            def upload(self,row):calls.append(row);return 'a'*11
        svc.youtube_studio=Uploader()
        self.assertTrue(svc.youtube_once())
        self.assertEqual(calls[0]['channel_id'],CHANNEL)
        rows=svc.youtube_queue.list_uploads()
        self.assertEqual(rows[0]['state'],'confirmed')
        self.assertAlmostEqual(rows[1]['publish_at']-rows[0]['updated_at'],16*3600)
        self.assertAlmostEqual(rows[2]['publish_at']-rows[0]['updated_at'],32*3600)

    def test_test_account_all_immediate_and_uncertainty_stops_batch(self):
        svc=self.make_service(local=True)
        self.assertTrue(svc.worker_once())
        self.assertEqual([r['publish_at'] for r in svc.youtube_queue.list_uploads()],[None]*3)
        class Broken:
            def upload(self,row): raise RuntimeError('connection lost')
        svc.youtube_studio=Broken()
        self.assertTrue(svc.youtube_once())
        self.assertFalse(svc.youtube_once())
        self.assertEqual([r['state'] for r in svc.youtube_queue.list_uploads()],['needs_review','queued','queued'])

    def test_failed_job_is_never_uploaded(self):
        svc=self.make_service()
        job=svc.store.claim()
        p=self.root/'one.mp4';p.write_bytes(b'v')
        svc.youtube_queue.enqueue(job['id'],'kussia88',[p])
        class Upload:
            def upload(self,row):raise AssertionError('must not upload')
        svc.youtube_studio=Upload()
        self.assertFalse(svc.youtube_once())
        svc.store.finish(job['id'],'failed')
        self.assertFalse(svc.youtube_once())

class BrowserFlowTests(unittest.TestCase):
    def test_channel_url_validation(self):
        self.assertEqual(studio.channel_from_url('https://studio.youtube.com/channel/'+CHANNEL+'/videos'),CHANNEL)
        for url in ('https://evil.example/channel/'+CHANNEL,'https://studio.youtube.com.evil.example/channel/'+CHANNEL,'http://studio.youtube.com/channel/'+CHANNEL):
            self.assertIsNone(studio.channel_from_url(url))

    def test_receipt_id_and_schedule_parsing(self):
        self.assertEqual(studio.video_from_url('https://youtu.be/'+'a'*11),'a'*11)
        self.assertIsNone(studio.video_from_url('https://evil.example/'+'a'*11))
        self.assertEqual(str(studio.parse_date('Nov 9, 2026')),'2026-11-09')
        self.assertEqual(str(studio.parse_time('1:15 PM')),'13:15:00')
        with self.assertRaises(studio.StudioError):studio.local_schedule_timestamp(9999999999,'no timezone',now=1)
        with self.assertRaises(studio.StudioError):studio.local_schedule_timestamp(100,'UTC+00:00',now=99)

    def test_public_and_scheduled_go_through_distinct_visibility_steps(self):
        class Tab:
            def fill(self,*args): pass
        calls=[]
        def click(tab,selector,timeout=30):calls.append(selector)
        def wait(tab,expr,timeout,message):
            return ['https://youtu.be/'+'a'*11] if 'ytcp-video-share-dialog' in expr else True
        row={'profile':'x','channel_id':CHANNEL,'file_path':'/video.mp4','title':'title','audience':'not_kids','publish_at':None}
        obj=studio.YouTubeStudio('/tmp')
        with patch.object(studio,'Browser',lambda *a:nullcontext(Tab())),patch.object(studio,'channel_ready',return_value=CHANNEL),patch.object(studio,'click',side_effect=click),patch.object(studio,'wait',side_effect=wait),patch.object(studio,'attach_file'),patch.object(studio,'selected',return_value=True),patch.object(studio,'set_schedule') as schedule,patch.object(studio.time,'sleep'):
            self.assertEqual(obj.upload(row),'a'*11)
            self.assertIn('tp-yt-paper-radio-button[name="PUBLIC"]',calls)
            schedule.assert_not_called()
            calls.clear();row['publish_at']=1234567890
            self.assertEqual(obj.upload(row),'a'*11)
            self.assertNotIn('tp-yt-paper-radio-button[name="PUBLIC"]',calls)
            schedule.assert_called_once()

if __name__=='__main__':unittest.main()
