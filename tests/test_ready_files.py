import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pipeline
from settings import Settings
from service import Service

class ReadyFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.job={'vod_id':'123','streamer':'alice','state':'done'}
        self.folder=pipeline.job_output_folder(self.root,self.job);(self.folder/'_work').mkdir(parents=True)
        self.rows=[{'start':542.412,'end':600.,'title':'Реакция'}]
        self.stem=pipeline.clip_stem(1,self.rows[0])
        (self.folder/'reactions.json').write_text(json.dumps({'vod_id':'123','reactions':self.rows}),encoding='utf8')
        (self.folder/(self.stem+'.pipeline.json')).write_text(json.dumps({'vod_id':'123','reaction':self.rows[0],'keep':[[.412,9.942],[11.862,58.0]]}),encoding='utf8')
        (self.folder/(self.stem+'.mp4')).write_bytes(b'video')
        (self.folder/(self.stem+'.txt')).write_text('транскрипт',encoding='utf8')
    def write_timeline(self,cuts=None):
        path=self.folder/'_work'/(self.stem+'.rendering.timeline.json')
        path.write_text(json.dumps({'cuts':cuts if cuts is not None else [{'start':9.942,'end':11.862}], 'keeps':[{'start':.412,'end':9.942},{'start':11.862,'end':58}]}),encoding='utf8')
    def test_vod_offsets_and_existing_idempotent(self):
        self.write_timeline();ready,outputs=pipeline.organize_completed(self.root,self.job)
        self.assertEqual(ready.name,'Готовые без тишины')
        self.assertEqual(Path(outputs[0]).read_bytes(),b'video')
        self.assertFalse((self.folder/(self.stem+'.mp4')).exists())
        self.assertTrue((ready/(self.stem+'.txt')).exists())
        report=(ready/(self.stem+' — удалённая тишина.txt')).read_text(encoding='utf-8-sig')
        self.assertIn('00:09:11.942 — 00:09:13.862',report);self.assertIn('1.920 с',report)
        self.assertEqual(pipeline.organize_completed(self.root,self.job),(ready,outputs))
    def test_local_time_is_not_offset(self):
        job={'vod_id':'test-'+32*'a','streamer':'test','local_name':'видео.mp4'}
        self.write_timeline([{'start':551.942,'end':553.862}])
        text=pipeline.cut_report(self.folder,job,1,self.rows[0],True)
        self.assertIn('00:09:11.942 — 00:09:13.862',text)
    def test_unknown_absence_disabled_and_empty_are_distinct(self):
        self.assertIn('недоступны',pipeline.cut_report(self.folder,self.job,1,self.rows[0],True))
        self.assertIn('выключено',pipeline.cut_report(self.folder,self.job,1,self.rows[0],False))
        self.write_timeline([])
        self.assertIn('не найдено',pipeline.cut_report(self.folder,self.job,1,self.rows[0],True))
        self.write_timeline([{'start':-100,'end':2}])
        self.assertIn('недоступны',pipeline.cut_report(self.folder,self.job,1,self.rows[0],True))
    def test_incomplete_move_recovers(self):
        self.write_timeline();ready=self.folder/pipeline.READY_SILENCE;ready.mkdir()
        (self.folder/(self.stem+'.mp4')).rename(ready/(self.stem+'.mp4'))
        result,_=pipeline.organize_completed(self.root,self.job)
        self.assertTrue((result/(self.stem+'.txt')).exists())
    def test_conflicting_file_is_not_overwritten(self):
        self.write_timeline();ready=self.folder/pipeline.READY_SILENCE;ready.mkdir()
        (ready/(self.stem+'.mp4')).write_bytes(b'other')
        with self.assertRaises(pipeline.PipelineError): pipeline.organize_completed(self.root,self.job)
        self.assertEqual((ready/(self.stem+'.mp4')).read_bytes(),b'other')
        self.assertTrue((self.folder/(self.stem+'.txt')).exists())
    def test_legacy_without_proven_plan_is_neutral(self):
        self.write_timeline();path=self.folder/'_work'/(self.stem+'.rendering.timeline.json')
        data=json.loads(path.read_text());data['keeps'][0]['end']=20;path.write_text(json.dumps(data))
        ready,_=pipeline.organize_completed(self.root,self.job)
        self.assertEqual(ready.name,'Готовые нарезки')
        self.assertIn('Нет достоверных', (ready/(self.stem+' — удалённая тишина.txt')).read_text(encoding='utf-8-sig'))
    def test_not_done_is_refused(self):
        self.job['state']='failed'
        with self.assertRaises(pipeline.PipelineError):pipeline.organize_completed(self.root,self.job)

if __name__=='__main__':unittest.main()
