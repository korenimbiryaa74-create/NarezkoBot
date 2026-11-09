import tempfile
import unittest
from pathlib import Path
from youtube_queue import YouTubeQueue

CHANNEL_A = 'UC' + 'a' * 22
CHANNEL_B = 'UC' + 'b' * 22

class YouTubeQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = YouTubeQueue(self.root / 'state' / 'jobs.sqlite3')
        self.files = []
        for i in range(5):
            p = self.root / f'clip {i + 1}.mp4'
            p.write_bytes(bytes([i + 1]) * 20)
            self.files.append(p)

    def test_fast_slow_and_test_schedule(self):
        self.queue.bind('kussia88', CHANNEL_A, 16, audience='not_kids')
        self.queue.enqueue(1, 'KUSSIA88', self.files, now=1000)
        rows = self.queue.list_uploads()
        self.assertEqual([r['publish_at'] for r in rows], [None, 58600, 116200, 173800, 231400])
        self.queue.bind('fast', CHANNEL_B, 1, audience='not_kids')
        self.queue.enqueue(2, 'fast', self.files[:3], now=1000)
        self.assertEqual([r['publish_at'] for r in self.queue.list_uploads()[5:]], [None, 4600, 8200])
        self.queue.bind('__test__', 'UC'+'c'*22, 16, audience='not_kids')
        self.queue.enqueue(3, 'ignored', self.files[:3], is_test=True, now=1000)
        self.assertEqual([r['publish_at'] for r in self.queue.list_uploads()[8:]], [None, None, None])

    def test_idempotency_and_changed_file(self):
        self.queue.bind('a', CHANNEL_A, audience='not_kids')
        self.assertEqual(self.queue.enqueue(1, 'a', self.files[:2], now=1), 2)
        self.assertEqual(self.queue.enqueue(1, 'a', self.files[:2], now=2), 0)
        with self.assertRaises(ValueError):
            self.queue.enqueue(1, 'a', self.files[:1], now=3)
        self.files[0].write_bytes(b'changed')
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.list_uploads()[0]['state'], 'needs_review')
        self.assertIsNone(self.queue.claim())

    def test_uncertain_cannot_auto_retry_or_overtake(self):
        self.queue.bind('a', CHANNEL_A, audience='not_kids')
        self.queue.enqueue(1, 'a', self.files[:2], now=1)
        first = self.queue.claim()
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.recover_interrupted(), 1)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.list_uploads()[0]['state'], 'needs_review')
        with self.assertRaises(ValueError):
            self.queue.confirm(first['id'], CHANNEL_A, 'a' * 11)

    def test_explicit_audience_and_manual_receipt_reconciliation(self):
        self.queue.bind('a', CHANNEL_A)
        with self.assertRaises(ValueError): self.queue.enqueue(1,'a',self.files[:2])
        self.queue.bind('a', CHANNEL_A, audience='not_kids')
        self.queue.enqueue(1,'a',self.files[:2])
        first=self.queue.claim();self.queue.mark_uncertain(first['id'],'timeout')
        self.queue.confirm_reviewed(first['id'],'b'*11)
        second=self.queue.claim()
        self.assertEqual(second['clip_index'],2)
        with self.assertRaises(ValueError):self.queue.confirm(second['id'],CHANNEL_A,'b'*11)
        self.queue.mark_uncertain(second['id'],'needs check')
        self.queue.retry_reviewed(second['id'])
        self.assertEqual(self.queue.claim()['id'],second['id'])

    def test_uncertain_video_does_not_block_unrelated_jobs(self):
        self.queue.bind('a', CHANNEL_A, audience='not_kids')
        self.queue.bind('b', CHANNEL_B, audience='not_kids')
        self.queue.enqueue(1,'a',self.files[:1])
        self.queue.enqueue(2,'a',self.files[1:2])
        self.queue.enqueue(3,'b',self.files[2:3])
        first=self.queue.claim()
        # An in-progress browser also blocks the same profile in another job.
        other=self.queue.claim()
        self.assertEqual(other['profile'],'b')
        self.queue.confirm(other['id'],CHANNEL_B,'b'*11)
        self.queue.mark_uncertain(first['id'],'check studio')
        next_job=self.queue.claim()
        self.assertEqual(next_job['job_id'],2)
        self.queue.confirm_reviewed(first['id'],'a'*11)

    def test_submitted_from_previous_run_does_not_block_new_jobs(self):
        self.queue.bind('a',CHANNEL_A,audience='not_kids')
        self.queue.enqueue(1,'a',self.files[:1])
        first=self.queue.claim();self.queue.mark_submitted(first['id'],CHANNEL_A,'a'*11)
        self.assertEqual(self.queue.detach_previous_submitted(),1)
        self.assertEqual(self.queue.submitted_uploads('a'),[])
        old=self.queue.list_uploads()[0]
        self.assertEqual(old['state'],'uploading')
        self.assertEqual(old['tracking'],0)
        self.assertEqual(old['video_id'],'a'*11)
        # It is not confirmed or deleted, and the same job cannot duplicate it.
        self.assertEqual(self.queue.enqueue(1,'a',self.files[:1]),0)
        self.queue.enqueue(2,'a',self.files[1:2])
        self.assertEqual(self.queue.claim()['job_id'],2)

    def test_wait_reason_and_explicit_cancel(self):
        self.queue.bind('a',CHANNEL_A,audience='not_kids')
        self.queue.enqueue(1,'a',self.files[:2])
        self.assertIn('отключена',self.queue.waiting_reason(False))
        first=self.queue.claim(); self.queue.mark_uncertain(first['id'],'interrupted')
        self.assertIn('№'+str(first['id']),self.queue.waiting_reason())
        self.queue.cancel_reviewed_or_queued(first['id'])
        self.assertEqual(self.queue.waiting_reason(),'')
        second=self.queue.claim()
        with self.assertRaises(ValueError):self.queue.cancel_reviewed_or_queued(second['id'])
        self.queue.confirm(second['id'],CHANNEL_A,'a'*11)
        with self.assertRaises(ValueError):self.queue.cancel_reviewed_or_queued(second['id'])

    def test_clear_pending_keeps_uploading_and_confirmed(self):
        self.queue.bind('a',CHANNEL_A,audience='not_kids')
        self.queue.bind('b',CHANNEL_B,audience='not_kids')
        self.queue.bind('c','UC'+'c'*22,audience='not_kids')
        self.queue.enqueue(1,'a',self.files[:2])
        self.queue.enqueue(2,'b',self.files[2:3])
        self.queue.enqueue(3,'c',self.files[3:4])
        first=self.queue.claim();self.queue.mark_uncertain(first['id'],'uncertain')
        second=self.queue.claim();self.queue.confirm(second['id'],CHANNEL_B,'a'*11)
        third=self.queue.claim()
        self.assertEqual(third['profile'],'c')
        self.assertEqual(self.queue.clear_pending_uploads(),2)
        rows=self.queue.list_uploads()
        self.assertEqual([r['state'] for r in rows],['cancelled','cancelled','confirmed','uploading'])
        self.assertEqual(self.queue.clear_pending_uploads(),0)
        self.assertIsNone(self.queue.claim())
        # Cancelled records prevent silently re-enqueueing the same source job.
        self.assertEqual(self.queue.enqueue(1,'a',self.files[:2]),0)
        self.queue.confirm(third['id'],'UC'+'c'*22,'b'*11)

    def test_verified_receipt_and_channel_protection(self):
        self.queue.bind('a', CHANNEL_A, audience='not_kids')
        with self.assertRaises(ValueError): self.queue.bind('b', CHANNEL_A)
        self.queue.enqueue(1, 'a', self.files[:2])
        with self.assertRaises(ValueError): self.queue.bind('a', CHANNEL_B)
        first = self.queue.claim()
        with self.assertRaises(ValueError): self.queue.confirm(first['id'], CHANNEL_B, 'a' * 11)
        self.queue.confirm(first['id'], CHANNEL_A, 'a' * 11, now=10_000)
        second = self.queue.claim()
        self.assertEqual(second['clip_index'], 2)
        self.assertEqual(second['publish_at'], 13_600)

if __name__ == '__main__':
    unittest.main()
