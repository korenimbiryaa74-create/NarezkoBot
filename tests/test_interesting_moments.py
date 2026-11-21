import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import reactions
import pipeline
from chatgpt_finder import ChatGPTFinder

class MomentsTests(unittest.TestCase):
    def find(self,rows,segs=None,minutes=0,dur=5000):
        segs=segs if segs is not None else [{'start':0,'end':1,'text':'Начало'},{'start':1300,'end':1310,'text':'Тема'},{'start':1312,'end':1320,'text':'Переход'},{'start':2940,'end':2950,'text':'Конец'}]
        response=json.dumps({'reactions':rows},ensure_ascii=False)
        seen=[]
        def ask(instr,doc,fname,timeout,log,need):
            seen.append((instr,doc,need));return response
        logs=[]
        with tempfile.TemporaryDirectory() as d:
            result=reactions.find_reactions_whole(str(Path(d)/'video.mp4'),segs,dur,{'reactions':{'min_minutes':minutes}},extra={'chatgpt_doc':ask},log=logs.append)
        return result['reactions'],seen,logs
    def test_empty_result_and_prompt_allows_either_category(self):
        rows,seen,_=self.find([])
        self.assertEqual(rows,[]);prompt=seen[0][0]
        for text in ('reaction','discussion','может не быть','20–30 минут'):
            self.assertIn(text,prompt)
        self.assertEqual(seen[0][2],'reactions')
    def test_prompt_requests_safe_metadata_and_preview_with_timing(self):
        rows,seen,_=self.find([])
        self.assertEqual(len(seen),1)
        prompt=seen[0][0]
        for text in ('только итоговый JSON без рассуждений', 'не цитируй речь',
                     'Если момент нельзя описать безопасно, пропусти его',
                     'живыми, конкретными и понятными', 'preview_text', 'start', 'end'):
            self.assertIn(text,prompt)

    def test_safety_block_never_retries_or_switches_account(self):
        from chatgpt_errors import ChatGPTSafetyError
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'gpt_accounts'/'acc_02'/'profile').mkdir(parents=True)
            attempts=[]
            class Web:
                def __init__(self,**kw):self.account=Path(kw['profile']).parent.name
                def ask(self,*args,**kwargs):
                    attempts.append(self.account);raise ChatGPTSafetyError('blocked')
                def close(self):pass
            finder=ChatGPTFinder(root,web_factory=Web)
            with self.assertRaises(ChatGPTSafetyError):
                finder('v.mp4',[{'start':0,'end':60,'text':'пример'}],60,'name',{'min_reaction_minutes':0},lambda *a:None)
            self.assertEqual(attempts,['acc_01'])
            self.assertFalse((root/'gpt_uploads'/'response_cache').exists())

    def test_both_categories_preserved_and_sorted(self):
        result,_,_=self.find([{'type':'discussion','start':1000,'end':1300,'title':'История'},
                              {'type':'reaction','start':100,'end':900,'title':'Ролик'}])
        self.assertEqual([r['type'] for r in result],['reaction','discussion'])
        clean=pipeline.validate_intervals(result,5000)
        self.assertEqual([r['type'] for r in clean],['reaction','discussion'])
    def test_long_discussion_split_preserves_entire_span_near_pause(self):
        r=[{'type':'discussion','start':0,'end':2940,'title':'Длинное обсуждение'}]
        parts,_,logs=self.find(r,minutes=10)
        self.assertEqual(len(parts),2);self.assertEqual(parts[0]['start'],0);self.assertEqual(parts[-1]['end'],2940)
        self.assertEqual(parts[0]['end'],parts[1]['start'])
        self.assertTrue(all(600<=x['end']-x['start']<=1800 for x in parts))
        self.assertEqual(parts[0]['type'],'discussion');self.assertIn('часть 1/2',parts[0]['title'])
        self.assertEqual(len(pipeline.validate_intervals(parts,5000,600)),2)
        self.assertTrue(any('разделено' in s for s in logs))
    def test_reaction_is_not_split_and_overlap_is_rejected(self):
        rows,_,_=self.find([{'type':'reaction','start':0,'end':2940,'title':'Ролик'}])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['end'],2940)
        with self.assertRaises(reactions.ReactionsError):self.find([{'type':'reaction','start':0,'end':100},{'type':'discussion','start':90,'end':140}])
    def test_unknown_missing_and_out_of_range_refused(self):
        for row in ({'start':0,'end':100},{'type':'foo','start':0,'end':100},{'type':'discussion','start':0,'end':5001}):
            with self.subTest(row=row),self.assertRaises(reactions.ReactionsError):self.find([row])
    def test_old_manual_answer_is_a_reaction(self):
        answer='{"reactions":[{"start":0,"end":100,"title":"Видео"}]}'
        result=reactions.find_reactions_whole('v',[{'start':0,'end':100,'text':'test'}],100,{'reactions':{'min_minutes':0}},manual_answer=answer,log=lambda *_:None)
        self.assertEqual(result['reactions'][0]['type'],'reaction')
    def test_conflicting_minimum_does_not_silently_drop_parts(self):
        with self.assertRaisesRegex(reactions.ReactionsError,'минимальной длиной'):
            self.find([{'type':'discussion','start':0,'end':1860}],minutes=20)
    def test_pipeline_saves_type_and_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); job={'vod_id':'123','streamer':'name'}
            def download(vod,quality,out,**kwargs):Path(out).write_bytes(b'x'*2000)
            def speech(*args,**kwargs):return [{'start':1,'end':2,'text':'пример'}],60
            def find(*args):return [{'type':'discussion','start':1,'end':5,'title':'История','confidence':'high'}]
            def render(src,out,row,offset,cfg):Path(out).write_bytes(b'x'*2000);return [(0,4)],4
            engine=pipeline.Pipeline(root,find,download,render,speech)
            with patch.object(pipeline,'media_duration',return_value=60):result=engine.run(job,{'min_reaction_minutes':0})
            out=Path(result['outputs'][0]);text=out.with_suffix('.txt').read_text(encoding='utf-8-sig')
            self.assertIn('Тип момента: Обсуждение',text)
            data=json.loads((out.parent.parent/'reactions.json').read_text(encoding='utf8'))
            self.assertEqual(data['reactions'][0]['type'],'discussion')
    def test_donations_only_reactions_keep_original_indices(self):
        with tempfile.TemporaryDirectory() as d:
            f=ChatGPTFinder(d);rows=[{'start':0.,'end':100.,'type':'discussion'},{'start':100.,'end':200.,'type':'reaction'}]
            segs=[{'start':0,'end':200,'text':'донат'}]
            seen=[]
            def ask(prompt,doc,fname,timeout,log,need):
                seen.append(prompt)
                return '{"donation_cuts":[{"reaction":2,"start":"00:02:00","end":"00:02:10","confidence":"high"}]}'
            f._ask_document=ask
            self.assertEqual(f.find_donations(segs,rows,lambda *a:None),[{'reaction':2,'start':120.,'end':130.}])
            self.assertIn('2: 00:01:40',seen[0]); self.assertNotIn('1: 00:00:00',seen[0])
            bad='{"donation_cuts":[{"reaction":1,"start":"00:00:10","end":"00:00:20","confidence":"high"}]}'
            # Донат в обсуждении не вырезается и задание не падает (раньше это останавливало kussia88).
            self.assertEqual(f.find_donations(segs,rows,lambda *a:None,manual=bad),[])
            self.assertEqual(pipeline.validate_donation_cuts([{'reaction':1,'start':10,'end':20}],rows),[])
    def test_only_discussions_no_donation_request_and_silence_setting_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);job={'vod_id':'123','streamer':'name'}; seen=[]
            class Finder:
                def __call__(self,*a):return [{'type':'discussion','start':1,'end':5,'title':'Ответ на донат'}]
                def find_donations(self,*a,**kw):self.fail('donation search called')
            def download(vod,quality,out,**kwargs):Path(out).write_bytes(b'x'*2000)
            def speech(*a,**kw):return [{'start':1,'end':2,'text':'ответ'}],60
            def render(src,out,row,offset,cfg):
                seen.append((row,cfg));Path(out).write_bytes(b'x'*2000);return [(0,4)],4
            with patch.object(pipeline,'media_duration',return_value=60):
                pipeline.Pipeline(root,Finder(),download,render,speech).run(job,{'min_reaction_minutes':0,'remove_donations':True,'silence':{'enabled':True}})
            self.assertTrue(seen[0][1]['enabled']);self.assertNotIn('donation_cuts',seen[0][0])
    def test_prompt_allows_interesting_donation_reply(self):
        _,seen,_=self.find([])
        self.assertIn('ответ на донат',seen[0][0]);self.assertIn('не исключай их только потому',seen[0][0])

if __name__=='__main__':unittest.main()
