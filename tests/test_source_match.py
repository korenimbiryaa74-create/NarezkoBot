import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import source_match as sm
import reaction_previews as rp
import reactions
import pipeline
import youtube_titles as yt

class SourceMatchTests(unittest.TestCase):
    def setUp(self):
        sm._rate_limited_until=0
        self.row={'start':0,'end':500,'type':'reaction','title':'Тема исходного ролика','source_url_hint':'https://www.youtube.com/watch?v=AAAAAAAAAAA'}
        self.segs=[{'start':i*10,'end':i*10+9,'text':' '.join('слово'+str(i*25+j) for j in range(25))} for i in range(16)]
    def tearDown(self):sm._rate_limited_until=0
    def response(self,cmd,**kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({'entries':[{'id':'BBBBBBBBBBB','title':'Другой ролик'}]}).encode())
    def matched(self,vid,segs,row):
        if vid!='AAAAAAAAAAA':return None
        return {'id':vid,'title':'Настоящее название','channel':'Автор','evidence':{'matches':100,'sections':3}}
    def test_matching_requires_long_repeated_content_not_common_topic(self):
        text=' '.join(s['text'] for s in self.segs)
        proof=sm.caption_evidence(self.segs,self.row,text)
        self.assertGreater(proof['matches'],50);self.assertEqual(proof['sections'],4)
        self.assertEqual(sm.caption_evidence(self.segs,self.row,'несовпадающие слова '*1000)['matches'],0)
    def test_suggested_url_is_independently_verified(self):
        found,qs=sm.resolve(self.row,self.segs,runner=self.response,verifier=self.matched)
        self.assertEqual(found[0]['id'],'AAAAAAAAAAA')
        self.assertEqual(yt.title_from_moment({'type':'reaction','title':'описание','verified_source_title':found[0]['title']},'Стинт'),'СТИНТ СМОТРИТ: Настоящее название')
        with self.assertRaises(rp.SourceUncertain):sm.resolve(self.row,self.segs,runner=self.response,verifier=lambda *args:None)
    def test_malicious_hint_never_becomes_candidate(self):
        self.row['source_url_hint']='https://evil.example/?v=AAAAAAAAAAA';seen=[]
        def verify(vid,*args):seen.append(vid);return None
        with self.assertRaises(rp.SourceUncertain):sm.resolve(self.row,self.segs,runner=self.response,verifier=verify)
        self.assertNotIn('AAAAAAAAAAA',seen);self.assertEqual(seen,['BBBBBBBBBBB'])
    def test_ambiguous_sources_are_not_accepted(self):
        def verify(vid,*args):return {'id':vid,'title':'copy','channel':'','evidence':{'matches':100,'sections':3}}
        with self.assertRaises(rp.SourceUncertain):sm.resolve(self.row,self.segs,runner=self.response,verifier=verify)
    def test_rate_limit_stops_and_blocks_subsequent_requests(self):
        seen=[]
        def verify(vid,*args):seen.append(vid);raise sm.RateLimited()
        with self.assertRaisesRegex(rp.SourceUncertain,'ограничил'):sm.resolve(self.row,self.segs,runner=self.response,verifier=verify)
        self.assertEqual(len(seen),1)
        with self.assertRaisesRegex(rp.SourceUncertain,'ограничил'):sm.resolve(self.row,self.segs,runner=lambda *a,**k: self.fail('retried'),verifier=verify)
    def test_gpt_hints_survive_same_timestamp_response(self):
        prompt=reactions.WHOLE_PROMPT
        self.assertIn('source_url',prompt);self.assertIn('source_search',prompt);self.assertIn('не выдумывай url',prompt.lower())
        answer=json.dumps({'reactions':[{'start':1,'end':50,'type':'reaction','title':'Тема','preview_text':'ТЕСТ','source_url':'https://youtu.be/AAAAAAAAAAA','source_search':'Автор тема'}]},ensure_ascii=False)
        calls=[]
        def ask(*args,**kw):calls.append(1);return answer
        with tempfile.TemporaryDirectory() as d:
            result=reactions.find_reactions_whole(d+'/test.mp4',[{'start':1,'end':50,'text':'фраза'}],60,{'reactions':{'min_minutes':0}},extra={'chatgpt_doc':ask},log=lambda *a:None)
        self.assertEqual(len(calls),1)
        row=pipeline.validate_intervals(result['reactions'],60)[0]
        self.assertEqual(row['source_url_hint'],'https://youtu.be/AAAAAAAAAAA');self.assertEqual(row['source_search_hint'],'Автор тема')
        self.assertEqual(yt.streamer_name({'local_name':'[11-8-26] Stintik - запись.mp4'}),'Стинт')

if __name__=='__main__':unittest.main()
