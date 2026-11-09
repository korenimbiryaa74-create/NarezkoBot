"""Batch uploads keep transfers open while processing continues."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from job_store import JobStore
from youtube_queue import YouTubeQueue
import youtube_studio as studio

CHANNEL='UC'+'x'*22
class Stop:
    def __init__(self):self.stopped=False
    def is_set(self):return self.stopped
    def wait(self,n):pass
class Browser:
    last=None
    def __init__(self,*a):self.keep_open=False;self.tabs=0;Browser.last=self
    def __enter__(self):return object()
    def __exit__(self,*a):pass
    def open_upload_tab(self,channel):self.tabs+=1;return object()
    def open_monitor(self,channel):return object()

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);db=self.root/'db.sqlite'
        self.store=JobStore(db);self.queue=YouTubeQueue(db)
        self.queue.bind('test',CHANNEL,audience='not_kids')
        self.store.add('123','test');job=self.store.claim();self.store.finish(job['id'],'done')
        self.files=[]
        for i in range(3):
            p=self.root/f'{i}.mp4';p.write_bytes(b'video'+bytes([i]));self.files.append(p)
        self.queue.enqueue(job['id'],'test',self.files,now=1000)
        self.first=self.queue.claim(finished_only=True);self.obj=studio.YouTubeStudio(self.root)
        self.events=[];self.stop=Stop()
    def batch(self):
        self.obj.upload_batch(self.queue,self.first,'test',CHANNEL,self.stop,
            lambda i,m:self.events.append(('progress',i)),lambda i,v:self.events.append(('confirmed',i)))
    def test_submits_entire_batch_before_processing_any_video(self):
        ids=iter(['a'*11,'b'*11,'c'*11])
        def submit(tab,browser,row,progress=None):
            browser.keep_open=True;self.events.append(('submit',row['id']));return next(ids)
        def status(tab,channel,video,scheduled):
            self.events.append(('monitor',video));return 'ready'
        with patch.object(studio,'Browser',Browser),patch.object(studio,'channel_ready'),patch.object(self.obj,'submit',side_effect=submit),patch.object(studio,'monitor_status',side_effect=status):self.batch()
        self.assertEqual([e[0] for e in self.events if e[0] in ('submit','monitor')],['submit']*3+['monitor']*3)
        self.assertEqual([r['state'] for r in self.queue.list_uploads()],['confirmed']*3)
        self.assertEqual(Browser.last.tabs,2);self.assertFalse(Browser.last.keep_open)
    def test_failure_keeps_previously_submitted_id_without_retry(self):
        def submit(tab,browser,row,progress=None):
            browser.keep_open=True
            if row['clip_index']==2:
                browser.attempt_may_upload=True
                raise studio.StudioError('connection lost')
            return 'a'*11
        with patch.object(studio,'Browser',Browser),patch.object(studio,'channel_ready'),patch.object(self.obj,'submit',side_effect=submit),patch.object(studio,'monitor_status',return_value='ready') as monitor:
            with self.assertRaises(studio.StudioError):self.batch()
        rows=self.queue.list_uploads()
        self.assertEqual([(r['state'],r['submitted']) for r in rows],[('confirmed',1),('needs_review',0),('queued',0)])
        self.assertTrue(Browser.last.keep_open)
        monitor.assert_called_once()
        self.assertEqual(self.queue.recover_interrupted(),0)
    def test_processing_recovery_never_resubmits(self):
        self.queue.mark_submitted(self.first['id'],CHANNEL,'a'*11,now=10000)
        self.assertEqual(self.queue.recover_interrupted(),0)
        # No other clips eligible: cancel remaining records explicitly.
        self.queue.clear_pending_uploads();self.first=None
        with patch.object(studio,'Browser',Browser),patch.object(studio,'channel_ready'),patch.object(self.obj,'submit',side_effect=AssertionError('duplicate')),patch.object(studio,'monitor_status',return_value='ready'):self.batch()
        self.assertEqual(self.queue.list_uploads()[0]['state'],'confirmed')
    def test_warning_then_submission_does_not_wait_for_processing(self):
        class Tab:
            def __init__(self):self.states=iter([{'links':None,'warning':True},{'links':['https://youtu.be/'+'a'*11],'warning':False}])
            def js(self,script):return 'clicked' if script==studio._CONFIRM_CHECKS_WARNING else next(self.states)
        with patch.object(studio.time,'sleep'):
            self.assertEqual(studio.wait_for_submission(Tab(),candidate_id='a'*11,timeout=5),'a'*11)
    def test_processing_dialog_requires_known_video_id(self):
        class Tab:
            def js(self,script):return {'links':None,'warning':False,'processing':True}
        self.assertEqual(studio.wait_for_submission(Tab(),candidate_id='a'*11,timeout=1),'a'*11)
        with patch.object(studio.time,'sleep'):
            with self.assertRaises(studio.StudioError):studio.wait_for_submission(Tab(),timeout=.001)

    def test_new_tab_failure_marks_claimed_record_for_review(self):
        class BadBrowser(Browser):
            def open_upload_tab(self,channel):raise studio.StudioError('failed to open')
        def submit(tab,browser,row,progress=None):browser.keep_open=True;return 'a'*11
        with patch.object(studio,'Browser',BadBrowser),patch.object(studio,'channel_ready'),patch.object(self.obj,'submit',side_effect=submit),patch.object(studio,'monitor_status',return_value='ready'):
            with self.assertRaises(studio.StudioError):self.batch()
        self.assertEqual([r['state'] for r in self.queue.list_uploads()],['confirmed','needs_review','queued'])
        self.assertFalse(Browser.last.keep_open)

    def test_new_upload_tab_is_foregrounded_without_closing_old_tab(self):
        calls=[]
        class OldTab:
            def call(self,method,**kwargs):
                calls.append((method,kwargs));return {'targetId':'new'}
        class NewTab:
            def __init__(self,url):self.url=url
            def call(self,method,**kwargs):calls.append((method,kwargs));return {}
        browser=studio.Browser(self.root,'test');browser.port=1234;browser.tab=OldTab()
        pages=[{'id':'new','webSocketDebuggerUrl':'ws://127.0.0.1/new'}]
        with patch.object(studio,'CDP',NewTab),patch.object(studio,'local_json',return_value=pages):
            tab=browser.open_upload_tab(CHANNEL)
        self.assertIs(browser.upload_tabs[0],tab)
        self.assertFalse(calls[0][1]['background'])
        self.assertTrue(any(method=='Page.bringToFront' for method,_ in calls))

    def test_first_submission_anchors_schedule_only_once(self):
        self.queue.mark_submitted(self.first['id'],CHANNEL,'a'*11,now=10000)
        self.queue.confirm(self.first['id'],CHANNEL,'a'*11,now=20000)
        self.assertEqual(self.queue.list_uploads()[1]['publish_at'],13600)

if __name__=='__main__':unittest.main()
