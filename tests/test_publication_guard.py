import json
import tempfile
import unittest
from pathlib import Path
from publication_guard import assess, BLOCK, REVIEW
from youtube_queue import YouTubeQueue

class PublicationTests(unittest.TestCase):
    def test_known_shows_and_unconfirmed_reaction(self):
        for text in ('МУЖСКОЕ / ЖЕНСКОЕ', 'сегодня Мужское Женское', 'Пусть говорят'):
            kind, detail = assess({'type':'reaction'},text)
            self.assertEqual(kind,'block'); self.assertTrue(detail.startswith(BLOCK))
        self.assertEqual(assess({'type':'reaction'},'неизвестное шоу')[0],'review')
    def test_queued_reactions_require_review_and_known_shows_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); job=root/'job'; ready=job/'Готовые нарезки'; ready.mkdir(parents=True)
            q=YouTubeQueue(root/'q.db');q.bind('__test__','UC'+'A'*22,audience='not_kids')
            files=[]
            for i,text in enumerate(('неизвестное шоу','Мужское / Женское'),1):
                p=ready/(f'{i:02} clip.mp4');p.write_bytes(b'video');files.append(p)
                (ready/(p.stem+'.txt')).write_text(text,encoding='utf8')
                (job/(p.stem+'.pipeline.json')).write_text(json.dumps({'reaction':{'type':'reaction'}}),encoding='utf8')
            q.enqueue(1,'x',files,is_test=True)
            self.assertIsNone(q.claim()); first=q.list_uploads()[0]
            self.assertEqual(first['state'],'needs_review');self.assertTrue(first['detail'].startswith(REVIEW))
            with self.assertRaises(ValueError):q.confirm_reviewed(first['id'],'Lx_9n0PXg58')
            self.assertIsNone(q.claim()); second=q.list_uploads()[1]
            self.assertTrue(second['detail'].startswith(BLOCK))
            with self.assertRaises(ValueError):q.retry_reviewed(second['id'])
            q.retry_reviewed(first['id']);self.assertEqual(q.claim()['id'],first['id'])
    def test_missing_metadata_fails_closed(self):
        from publication_guard import assess_file
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(assess_file(Path(d)/'lost.mp4')[0],'review')
