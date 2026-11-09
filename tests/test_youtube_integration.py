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
        class Finder:
            def youtube_titles(self,*args):raise AssertionError('Генерация названий должна быть выключена')
        svc=self.service.Service(root,FakeSettings(),Finder(),pipeline_factory=Engine)
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
        self.assertEqual([u['title'] for u in uploads],['clip 0','clip 1','clip 2'])
        calls=[]
        class Uploader:
            def upload(self,row,progress=None):calls.append(row);return 'a'*11
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
            def upload(self,row,progress=None): raise RuntimeError('connection lost')
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
            def upload(self,row,progress=None):raise AssertionError('must not upload')
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

    def test_check_prompt_is_confirmed_only_once_then_receipt(self):
        messages=[]
        class Tab:
            def __init__(self):self.states=iter([{'links':None,'warning':True},{'links':None,'warning':True},
                {'links':['https://youtu.be/'+'a'*11],'warning':False}]);self.clicks=0
            def js(self,script):
                if script==studio._CONFIRM_CHECKS_WARNING:self.clicks+=1;return 'clicked'
                return next(self.states)
        tab=Tab()
        with patch.object(studio.time,'sleep'):
            self.assertEqual(studio.wait_for_receipt(tab,progress=messages.append,timeout=10),'a'*11)
        self.assertEqual(tab.clicks,1);self.assertEqual(len(messages),1)

    def test_receipt_and_warning_must_be_unambiguous(self):
        class Tab:
            def __init__(self,ambiguous=False):self.ambiguous=ambiguous
            def js(self,script):
                if self.ambiguous:return 'ambiguous' if script==studio._CONFIRM_CHECKS_WARNING else {'links':None,'warning':True}
                return {'links':['https://youtu.be/'+'a'*11,'https://youtu.be/'+'b'*11],'warning':False}
        for ambiguous in (False,True):
            with self.assertRaises(studio.StudioError):studio.wait_for_receipt(Tab(ambiguous),timeout=10)

    def test_waits_for_processing_before_advancing(self):
        class Tab:
            def js(self,script):return {'links':[], 'warning':False}
        class Browser:
            def open_monitor(self,channel):return object()
        messages=[]
        with patch.object(studio,'monitor_status',side_effect=['processing','processing','ready']),patch.object(studio.time,'sleep'):
            result=studio.wait_for_receipt(Tab(),progress=messages.append,timeout=10,browser=Browser(),channel_id=CHANNEL,candidate_id='a'*11)
        self.assertEqual(result,'a'*11)
        self.assertTrue(any('обработк' in msg for msg in messages))

    def test_processing_error_leaves_browser_open(self):
        class Browser:
            last=None
            def __init__(self,*args):self.keep_open=False;Browser.last=self
            def __enter__(self):return Tab()
            def __exit__(self,*args):pass
        class Tab:
            def fill(self,*args):pass
        row={'profile':'x','channel_id':CHANNEL,'file_path':'/video.mp4','title':'title','audience':'not_kids','publish_at':None}
        with patch.object(studio,'Browser',Browser),patch.object(studio,'channel_ready',return_value=CHANNEL),patch.object(studio,'click'),patch.object(studio,'wait',return_value=True),patch.object(studio,'attach_file'),patch.object(studio,'wait_for_title',return_value=studio._TITLE_SELECTORS[0]),patch.object(studio,'selected',return_value=True),patch.object(studio,'upload_video_id',return_value='a'*11),patch.object(studio,'wait_for_receipt',side_effect=studio.StudioError('timeout')),patch.object(studio.time,'sleep'):
            with self.assertRaises(studio.StudioError):studio.YouTubeStudio('/tmp').upload(row)
        self.assertTrue(Browser.last.keep_open)

    def test_title_wait_accepts_safe_fallback_and_rejects_ambiguous(self):
        class Tab:
            def __init__(self,state):self.state=state
            def js(self,script):return self.state
        fallback=studio._TITLE_SELECTORS[1]
        self.assertEqual(studio.wait_for_title(Tab({'selector':fallback}),timeout=1),fallback)
        with self.assertRaises(studio.StudioError):
            studio.wait_for_title(Tab({'ambiguous':True}),timeout=1)

    def test_title_wait_times_out_without_reupload(self):
        class Tab:
            def js(self,script):return {'dialog':1,'title':False}
        with patch.object(studio.time,'sleep'):
            with self.assertRaisesRegex(studio.StudioError,'черновики'):
                studio.wait_for_title(Tab(),timeout=.001)

    def test_browser_is_not_terminated_for_uncertain_upload(self):
        class Proc:
            terminated=False
            def terminate(self):self.terminated=True
        browser=studio.Browser('/tmp','test');proc=Proc();browser.proc=proc;browser.keep_open=True
        browser.__exit__(None,None,None)
        self.assertFalse(proc.terminated)

    def test_temporary_monitor_reload_error_does_not_duplicate_upload(self):
        class Tab:
            def js(self,script):return {'links':None,'warning':False}
        class Browser:
            def open_monitor(self,channel):return object()
        with patch.object(studio,'monitor_status',side_effect=[studio.StudioError('reloading'),'processing','ready']),patch.object(studio.time,'sleep'):
            self.assertEqual(studio.wait_for_receipt(Tab(),timeout=10,browser=Browser(),channel_id=CHANNEL,candidate_id='a'*11),'a'*11)

    def test_monitor_requires_matching_channel(self):
        class Tab:
            def js(self,script):return {'url':'https://studio.youtube.com/channel/UC'+'z'*22+'/videos/upload','state':'ready'}
        self.assertEqual(studio.monitor_status(Tab(),CHANNEL,'a'*11,False),'unknown')

    def test_public_and_scheduled_go_through_distinct_visibility_steps(self):
        class Tab:
            def fill(self,*args): pass
        calls=[]
        def click(tab,selector,timeout=30):calls.append(selector)
        def wait(tab,expr,timeout,message):
            return ['https://youtu.be/'+'a'*11] if 'ytcp-video-share-dialog' in expr else True
        row={'profile':'x','channel_id':CHANNEL,'file_path':'/video.mp4','title':'title','audience':'not_kids','publish_at':None}
        obj=studio.YouTubeStudio('/tmp')
        with patch.object(studio,'Browser',lambda *a:nullcontext(Tab())),patch.object(studio,'channel_ready',return_value=CHANNEL),patch.object(studio,'click',side_effect=click),patch.object(studio,'wait',side_effect=wait),patch.object(studio,'attach_file'),patch.object(studio,'wait_for_title',return_value=studio._TITLE_SELECTORS[0]),patch.object(studio,'selected',return_value=True),patch.object(studio,'upload_video_id',return_value='a'*11),patch.object(studio,'wait_for_receipt',return_value='a'*11),patch.object(studio,'set_schedule') as schedule,patch.object(studio.time,'sleep'):
            self.assertEqual(obj.upload(row),'a'*11)
            self.assertIn('tp-yt-paper-radio-button[name="PUBLIC"]',calls)
            schedule.assert_not_called()
            calls.clear();row['publish_at']=1234567890
            self.assertEqual(obj.upload(row),'a'*11)
            self.assertNotIn('tp-yt-paper-radio-button[name="PUBLIC"]',calls)
            schedule.assert_called_once()

if __name__=='__main__':unittest.main()
