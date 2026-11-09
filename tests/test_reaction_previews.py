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
    def test_prompt_requests_short_clickable_labels_not_generic_streamer(self):
        prompt=reactions.WHOLE_PROMPT
        for text in ('максимум 4 слова и 32 символа', 'ВЗЛОМАЛИ ДВЕРЬ',
                     'ПОПАЛИСЬ ГОЛУБЧИКИ', '«стример смотрит»', 'не выдумывай'):
            self.assertIn(text,prompt)

    def test_long_labels_flagged_not_silently_truncated(self):
        label='ПОЛИЦИЯ РАЗБИРАЕТСЯ С БЫТОВЫМИ ВЫЗОВАМИ'
        with (patch.object(rp,'choose_cutout',return_value=Path('x.png')),
             patch.object(rp,'find_candidates',return_value=([{'id':'6EH2wmSPbMA','title':'x','channel':'y'}],[])),
             patch.object(rp,'download_thumbnail',return_value=(object(),'https://example.com')),
             patch.object(rp,'render_preview') as render):
            info=rp.generate('/unused','name',{'type':'reaction','preview_text':label},[],Path('/unused/x.jpg'))
        self.assertEqual(info['preview_text'],label);self.assertTrue(info['warnings'])
        self.assertEqual(render.call_args.args[2],label)

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

    def test_local_test_passes_configured_streamer_not_test_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test_input').mkdir();video=root/'test_input'/'video.mp4';video.write_bytes(b'x'*2000)
            stamp=video.stat();seen=[]
            def finder(src,segs,duration,login,cfg,progress):seen.append(login);return []
            def speech(*a,**kw):return ([{'start':1,'end':2,'text':'пример'}],60.)
            engine=pipeline.Pipeline(root,finder,speech=speech)
            job={'vod_id':'test-'+'a'*32,'streamer':'test','local_name':video.name,'local_size':stamp.st_size,'local_mtime_ns':stamp.st_mtime_ns}
            with patch.object(pipeline,'media_duration',return_value=60):
                engine.run(job,{'min_reaction_minutes':0,'preview':{'enabled':False,'test_streamer':'kussia88'}})
                engine.run(job,{'min_reaction_minutes':0,'preview':{'enabled':False,'test_streamer':''}})
            self.assertEqual(seen,['kussia88',''])

    def test_no_preview_for_discussion(self):
        with self.assertRaises(rp.PreviewError):rp.generate('/unused','name',{'type':'discussion'},[],Path('/tmp/a.jpg'))

class SavedClipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.vod='test-'+'a'*32
        self.job=self.root/'recordings'/'Тест один';self.ready=self.job/'Готовые нарезки'
        self.ready.mkdir(parents=True)
        self.clip=self.ready/'01 Тест.mp4';self.clip.write_bytes(b'video')
        self.row={'type':'reaction','start':542.412,'end':1742.240,'title':'Тест', 'preview_text':'ВЗЛОМАЛИ ДВЕРЬ'}
        self.write_metadata()
        self.clip.with_suffix('.txt').write_text('Транскрипт\n[00:00:05] Как взломали дверь\n',encoding='utf-8-sig')
    def write_metadata(self):
        (self.job/'reactions.json').write_text(json.dumps({'vod_id':self.vod,'reactions':[self.row]},ensure_ascii=False),encoding='utf-8')
        (self.job/'01 Тест.pipeline.json').write_text(json.dumps({'vod_id':self.vod,'reaction':self.row,'duration':1200},ensure_ascii=False),encoding='utf-8')
    def cache(self,filename='answer.json'):
        cache=self.root/'gpt_uploads'/'response_cache';cache.mkdir(parents=True,exist_ok=True)
        row=dict(self.row,start='00:09:02.412',end='00:29:02.240',preview_text='ВЗЛОМАЛИ ДВЕРЬ')
        (cache/filename).write_text(json.dumps({'text':json.dumps({'reactions':[row]},ensure_ascii=False)},ensure_ascii=False),encoding='utf-8')
    def test_generate_uses_saved_text_and_clip_transcript_without_chatgpt(self):
        prepared=rp.prepare_saved_clip(self.root,self.clip,'Kussia88')
        self.assertEqual(prepared['login'],'kussia88')
        self.assertEqual(prepared['row']['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
        self.assertEqual(prepared['row']['start'],0)
        self.assertEqual(prepared['segs'][0]['start'],5)
        def generate(root,login,row,segs,target,cancel=None):
            self.assertEqual(login,'kussia88');self.assertEqual(row['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
            target.write_bytes(b'jpg')
            return {'source_url':'https://example.org/test','preview_text':row['preview_text'],'warnings':[]}
        with patch.object(rp,'generate',side_effect=generate) as mocked:
            result=rp.generate_saved_clip(self.root,self.clip,'kussia88')
            self.assertFalse(result['existing']);self.assertEqual(result['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
            self.assertEqual(json.loads(Path(result['path']).with_suffix('.json').read_text(encoding='utf8'))['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
            result2=rp.generate_saved_clip(self.root,self.clip,'kussia88')
            self.assertTrue(result2['existing']);self.assertEqual(result2['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
            self.assertEqual(Path(result2['path']).read_bytes(),b'jpg');self.assertEqual(mocked.call_count,1)
    def test_old_row_can_recover_only_exact_unique_cached_answer(self):
        self.row.pop('preview_text');self.write_metadata();self.cache()
        self.assertEqual(rp.prepare_saved_clip(self.root,self.clip,'kussia88')['row']['preview_text'],'ВЗЛОМАЛИ ДВЕРЬ')
        self.cache('other.json')
        with self.assertRaisesRegex(rp.PreviewError,'несколько'):rp.prepare_saved_clip(self.root,self.clip,'kussia88')
    def test_missing_data_and_other_moment_fail_safely(self):
        self.row.pop('preview_text');self.write_metadata()
        with self.assertRaisesRegex(rp.PreviewError,'не найдена сохранённая надпись'):rp.prepare_saved_clip(self.root,self.clip,'kussia88')
        self.row.update(type='discussion',preview_text='НЕ НАДО');self.write_metadata()
        with self.assertRaisesRegex(rp.PreviewError,'не отмечена как реакция'):rp.prepare_saved_clip(self.root,self.clip,'kussia88')
    def test_existing_without_report_does_not_claim_text(self):
        target=self.clip.with_name(self.clip.stem+' — превью.jpg');target.write_bytes(b'previous')
        with patch.object(rp,'generate') as generate:
            result=rp.generate_saved_clip(self.root,self.clip,'kussia88')
        generate.assert_not_called();self.assertEqual(result['preview_text'],'');self.assertEqual(target.read_bytes(),b'previous')
    def test_report_alone_is_not_overwritten(self):
        report=self.clip.with_name(self.clip.stem+' — превью.json');report.write_text('{}',encoding='utf8')
        with self.assertRaisesRegex(rp.PreviewError,'отчёт без превью'):rp.generate_saved_clip(self.root,self.clip,'kussia88')
        self.assertEqual(report.read_text(encoding='utf8'),'{}')

if __name__=='__main__':unittest.main()
