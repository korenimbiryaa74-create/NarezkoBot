import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import pipeline
import reactions
import settings
import reaction_previews as rp
from chatgpt_finder import ChatGPTFinder

class Tests(unittest.TestCase):
    def test_title_and_label_from_one_request_are_preserved(self):
        calls=[]
        def ask(instr,doc,fname,timeout,log,need):
            calls.append(instr)
            return json.dumps({'reactions':[{'type':'reaction','start':1,'end':20,'title':'ИВАНА ЗОЛО ЗАДЕРЖАЛИ','preview_text':'ИВАНА ЗОЛО ЗАДЕРЖАЛИ','confidence':'high'},{'type':'discussion','start':21,'end':30,'title':'Разговор','preview_text':'НЕ ДОЛЖНО БЫТЬ'}]},ensure_ascii=False)
        with tempfile.TemporaryDirectory() as d:
            rows=reactions.find_reactions_whole(str(Path(d)/'x.mp4'),[{'start':0,'end':30,'text':'текст'}],30,{'reactions':{'min_minutes':0}},extra={'chatgpt_doc':ask},log=lambda *_:None)['reactions']
        self.assertEqual(len(calls),1);self.assertIn('preview_text',calls[0]);self.assertEqual(rows[0]['preview_text'],'ИВАНА ЗОЛО ЗАДЕРЖАЛИ');self.assertEqual(rows[1]['preview_text'],'')
        clean=pipeline.validate_intervals(rows,30);self.assertEqual(clean[0]['preview_text'],rows[0]['preview_text'])
    def test_search_uses_only_relevant_snippets_not_entire_stream(self):
        row={'start':10.,'end':30.,'title':'НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ'}
        segs=[{'start':0,'text':'НЕ ПОПАДЁТ В ПОИСК'}, {'start':12,'text':'Посмотрим ролик про потерю человечности в тик токе'}]
        calls=[]
        class Run:
            returncode=0
            stdout=json.dumps({'entries':[{'id':'6EH2wmSPbMA','title':'НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ'}, {'id':'EjMf14a8SnU','title':'КАША СМОТРИТ: НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ'}]}).encode()
        def runner(cmd,**kw):calls.append(cmd);return Run()
        results,qs=rp.find_candidates(row,segs,runner)
        self.assertEqual(results[0]['id'],'6EH2wmSPbMA');self.assertEqual(len(results),1)
        self.assertFalse(any('попадёт' in q for q in qs));self.assertTrue(all('--flat-playlist' in c for c in calls))
    def test_weak_search_match_is_rejected(self):
        class Run:
            returncode=0
            stdout=json.dumps({'entries':[{'id':'6EH2wmSPbMA','title':'ТИК ТОК другой ролик'}]}).encode()
        with self.assertRaises(rp.PreviewError):
            rp.find_candidates({'start':0,'end':3,'title':'НЕОБЫЧНЫЙ ЭКСПЕРИМЕНТ'}, [{'start':1,'text':'смотрим какой то ролик'}],lambda *a,**k:Run())
    def test_old_settings_are_compatible_and_test_login_normalized(self):
        cfg=settings.validate({'pipeline':{'preview':{'enabled':True,'test_streamer':'Kussia88'}}})
        self.assertTrue(cfg['pipeline']['preview']['enabled'])
        self.assertEqual(cfg['pipeline']['preview']['test_streamer'],'kussia88')
        self.assertFalse(settings.validate({})['pipeline']['preview']['enabled'])
    def test_missing_evidence_and_invalid_asset_fail(self):
        with self.assertRaises(rp.PreviewError):rp.find_candidates({'start':0,'end':1,'title':''},[])
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(rp.PreviewError):rp.choose_cutout(d,'../other',{})
            with self.assertRaises(rp.PreviewError):rp.choose_cutout(d,'kussia88',{})
    def test_real_composite_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'subject.png'; Image.new('RGBA',(300,404),(255,0,0,128)).save(path)
            target=Path(d)/'test.jpg'; background=Image.new('RGB',(1280,720),(30,80,100))
            rp.render_preview(background,path,'НЕТ ЧЕЛОВЕЧНОСТИ',target)
            with Image.open(target) as im:self.assertEqual(im.size,(1280,720))
            original=target.read_bytes()
            with self.assertRaises(rp.PreviewError):rp.render_preview(background,path,'ТЕКСТ',target)
            self.assertEqual(original,target.read_bytes())
    def test_pipeline_uses_reaction_preview_only_and_keeps_video_results(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); progress=[]; seen=[]
            def download(vod,quality,out,**kw): Path(out).write_bytes(b'x'*2000)
            def speech(*a,**kw):return ([{'start':1,'end':2,'text':'пример'}],60.)
            def finder(*a):return [{'start':1,'end':5,'title':'Реакция','type':'reaction','preview_text':'ТЕСТ'}, {'start':6,'end':10,'title':'Разговор','type':'discussion'}]
            def renderer(src,out,row,offset,cfg):Path(out).write_bytes(b'x'*2000);return [(0,4)],4.
            def generate(root,login,row,segs,target):
                seen.append((login,row['type']));Path(target).write_bytes(b'jpg')
                return {'status':'требует проверки','source_url':'https://www.youtube.com/watch?v=6EH2wmSPbMA'}
            engine=pipeline.Pipeline(root,finder,download=download,renderer=renderer,speech=speech)
            cfg={'min_reaction_minutes':0,'preview':{'enabled':True},'silence':{'enabled':False}}
            with patch.object(pipeline,'media_duration',return_value=60),patch.object(rp,'generate',side_effect=generate):
                result=engine.run({'vod_id':'123','streamer':'kussia88'},cfg,lambda *a:progress.append(a))
            self.assertEqual(len(result['outputs']),2);self.assertEqual(seen,[('kussia88','reaction')])
            self.assertEqual(len(list(Path(result['folder']).glob('* — превью.jpg'))),1)
            self.assertEqual(len(list(Path(result['folder']).glob('* — превью.json'))),1)
            self.assertTrue(any('требует проверки' in x[1] for x in progress))

    def test_no_preview_for_discussion(self):
        with self.assertRaises(rp.PreviewError):rp.generate('/unused','name',{'type':'discussion'},[],Path('/tmp/a.jpg'))

if __name__=='__main__':unittest.main()
