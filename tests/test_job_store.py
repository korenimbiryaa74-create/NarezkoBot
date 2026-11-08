import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from job_store import JobStore

class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'state'/'jobs.sqlite3'
        self.store = JobStore(self.path)
    def test_deduplication(self):
        a, created = self.store.add('123','nix','stream'); b, again = self.store.add('123','nix')
        self.assertTrue(created); self.assertFalse(again); self.assertEqual(a['id'],b['id'])
        with self.assertRaises(ValueError): self.store.add('../1','nix')
    def test_stage_and_events(self):
        job,_ = self.store.add('12','nix'); self.store.claim()
        self.store.update_stage(job['id'],'Рендер','Нарезка 1','recordings/nix')
        self.store.finish(job['id'],'done')
        self.assertEqual(self.store.list_jobs()[0]['state'],'done')
        self.assertEqual([r['stage'] for r in self.store.events(job['id'])], ['Ожидает обработки','Запуск','Рендер','Готово'])
        with self.assertRaises(ValueError): self.store.update_stage(job['id'],'Oops')
    def test_retry_and_recovery(self):
        job,_ = self.store.add('12','nix'); self.store.claim()
        self.store.finish(job['id'],'retry',retry_after=30)
        self.assertIsNone(self.store.claim()); self.assertEqual(self.store.claim(now=10**12)['attempts'],2)
        self.assertEqual(JobStore(self.path).recover_interrupted(),1)
        self.assertEqual(self.store.list_jobs()[0]['state'],'failed')
        self.store.retry(job['id']); self.assertEqual(self.store.claim()['attempts'],3)
    def test_concurrent_claims_are_unique(self):
        for i in range(20): self.store.add(str(i),'nix')
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(30)))
        ids = [r['id'] for r in claims if r]
        self.assertEqual(len(ids),20); self.assertEqual(len(set(ids)),20)
if __name__ == '__main__': unittest.main()

class MigrationTests(unittest.TestCase):
    def test_old_database_migration_preserves_queued_jobs(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'old.db'
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, vod_id TEXT UNIQUE NOT NULL, streamer TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'Ожидает обработки', detail TEXT NOT NULL DEFAULT '', output_dir TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("INSERT INTO jobs (vod_id,streamer,created_at,updated_at) VALUES ('1','nix',0,0)")
            db.close()
            store=JobStore(path)
            row,created=store.add('1','nix',duration_seconds=3600)
            self.assertFalse(created); self.assertEqual(row['duration_seconds'],3600)
            self.assertEqual(store.claim()['duration_seconds'],3600)
            self.assertEqual(JobStore(path).list_jobs()[0]['vod_id'],'1')
