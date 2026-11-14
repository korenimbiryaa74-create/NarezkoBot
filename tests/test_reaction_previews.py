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
import preview_local as pl
from chatgpt_finder import ChatGPTFinder

class Tests(unittest.TestCase):
    def test_title_and_label_from_one_request_are_preserved(self):
        calls=[]
        def ask(instr,doc,fname,timeout,log,need):
            calls.append(instr)
            return json.dumps({'reactions':[{'type':'reaction','start':1,'end':20,'title':'ИВАНА ЗОЛО ЗАДЕРЖАЛИ','preview_text':'ИВАНА ЗОЛО ЗАДЕРЖАЛИ','confidence':'high'},{'type':'discussion','start':21,'end':30,'title':'Разговор','preview_text':'НЕ ДОЛЖНО БЫТЬ','video_title':'Вывернули спину! Никс про лечение'}]},ensure_ascii=False)
        with tempfile.TemporaryDirectory() as d:
            rows=reactions.find_reactions_whole(str(Path(d)/'x.mp4'),[{'start':0,'end':30,'text':'текст'}],30,{'reactions':{'min_minutes':0}},extra={'chatgpt_doc':ask},log=lambda *_:None)['reactions']
        self.assertEqual(len(calls),1);self.assertIn('preview_text',calls[0]);self.assertEqual(rows[0]['preview_text'],'ИВАНА ЗОЛО ЗАДЕРЖАЛИ');self.assertEqual(rows[1]['preview_text'],'')
        clean=pipeline.validate_intervals(rows,30);self.assertEqual(clean[0]['preview_text'],rows[0]['preview_text'])
        self.assertEqual(clean[1]['video_title'],'Вывернули спину! Никс про лечение')
        import youtube_titles
        self.assertEqual(youtube_titles.title_from_moment(clean[0],'Каша'),'КАША СМОТРИТ: ИВАНА ЗОЛО ЗАДЕРЖАЛИ')
        self.assertEqual(youtube_titles.title_from_moment(clean[1],'Никс'),'Вывернули спину! Никс про лечение')
    def test_title_limits_do_not_cut_words(self):
        import youtube_titles
        self.assertEqual(reactions._word_limit('Полицейские разбирают необычные вызовы', 25),'Полицейские разбирают')
        self.assertEqual(youtube_titles._limit('Полицейские разбирают необычные вызовы',25),'Полицейские разбирают')

    def test_prompt_requests_short_clickable_labels_not_generic_streamer(self):
        prompt=reactions.WHOLE_PROMPT
        for text in ('строго 2–3 слова', 'ВЗЛОМАЛИ ДВЕРЬ',
                     'ПОПАЛИСЬ ГОЛУБЧИКИ', '«стример смотрит»', 'не выдумывай'):
            self.assertIn(text,prompt)

    def test_long_labels_reach_local_model_without_truncation(self):
        label='ПОЛИЦИЯ РАЗБИРАЕТСЯ С БЫТОВЫМИ ВЫЗОВАМИ'
        with (patch.object(pl,'check_install'),patch.object(rp,'choose_cutout',return_value=Path('x.png')),
             patch.object(rp,'choose_style',return_value=Path('style.jpg')),
             patch.object(rp,'resolve_source',return_value=([{'id':'6EH2wmSPbMA','title':'x','channel':'y'}],[])),
             patch.object(rp,'download_thumbnail',return_value=(object(),'https://example.com')),
             patch.object(pl,'generate_reaction') as render):
            info=rp.generate('/unused','name',{'type':'reaction','preview_text':label},[],Path('/unused/x.jpg'))
        self.assertEqual(info['preview_text'],label);self.assertTrue(info['warnings']);self.assertTrue(info['text_unverified'])
        self.assertEqual(render.call_args.args[4],label)

    def test_search_uses_only_relevant_snippets_not_entire_stream(self):
        row={'start':10.,'end':30.,'title':'НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ'}
        segs=[{'start':0,'text':'НЕ ПОПАДЁТ В ПОИСК'}, {'start':12,'text':'Посмотрим ролик про потерю человечности в тик токе современного мира'}]
        calls=[]
        class Run:
            returncode=0
            stdout=json.dumps({'entries':[{'id':'6EH2wmSPbMA','title':'НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ СОВРЕМЕННОГО МИРА'}, {'id':'EjMf14a8SnU','title':'КАША СМОТРИТ: НЕТ ЧЕЛОВЕЧНОСТИ В ТИК ТОКЕ'}]}).encode()
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
    def test_unknown_source_creates_labeled_synthetic_image_without_gpt(self):
        row={'type':'reaction','title':'Почему реакции стали популярны','preview_text':'РЕАКЦИИ ПОМОЙКА'}
        with (patch.object(pl,'check_install'),patch.object(rp,'choose_cutout',return_value=Path('x.png')),
              patch.object(rp,'choose_style',return_value=Path('style.jpg')),
              patch.object(rp,'resolve_source',side_effect=rp.SourceUncertain('нет источника')),
              patch.object(rp,'download_thumbnail') as download,patch.object(pl,'generate_reaction') as render):
            info=rp.generate('/unused','name',row,[],Path('/unused/x.jpg'))
        self.assertTrue(info['source_generated']);self.assertIsNone(info['source_url']);download.assert_not_called()
        self.assertEqual(render.call_args.kwargs['context'],row['title']);self.assertIsNone(render.call_args.args[3])
        self.assertTrue(any('не оригинальное превью' in x for x in info['warnings']))

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
            source={'id':'6EH2wmSPbMA','title':'ПОДТВЕРЖДЁННОЕ НАЗВАНИЕ','channel':'Автор','evidence':{'matches':120,'sections':3}}
            with (patch.object(pipeline,'media_duration',return_value=60),patch.object(rp,'generate',side_effect=generate),
                  patch.object(rp,'resolve_source',return_value=([source],['поиск'])) as resolve):
                result=engine.run({'vod_id':'123','streamer':'kussia88'},cfg,lambda *a:progress.append(a))
            resolve.assert_called_once()
            self.assertEqual(result['moments'][0]['video_title'],'КАША СМОТРИТ: ПОДТВЕРЖДЁННОЕ НАЗВАНИЕ')
            self.assertEqual(len(list(Path(result['folder']).glob('* — источник.json'))),1)
            self.assertEqual(len(result['outputs']),2);self.assertEqual(seen,[('kussia88','reaction')])
            self.assertEqual(len(list(Path(result['folder']).glob('* — ИИ-превью.jpg'))),1)
            self.assertEqual(len(list(Path(result['folder']).glob('* — ИИ-превью.json'))),1)
            self.assertTrue(any('требует проверки' in x[1] for x in progress))

    def test_source_title_also_resolves_when_preview_disabled_for_upload(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            def download(vod,quality,out,**kw):Path(out).write_bytes(b'x'*2000)
            def speech(*a,**kw):return ([{'start':1,'end':2,'text':'пример'}],60.)
            def finder(*a):return [{'start':1,'end':5,'title':'ОПИСАНИЕ','type':'reaction','preview_text':'ТЕСТ'}]
            def renderer(src,out,row,offset,cfg):Path(out).write_bytes(b'x'*2000);return [(0,4)],4.
            source={'id':'6EH2wmSPbMA','title':'Точное название','channel':'Автор','evidence':{'matches':120,'sections':3}}
            engine=pipeline.Pipeline(root,finder,download=download,renderer=renderer,speech=speech)
            cfg={'min_reaction_minutes':0,'preview':{'enabled':False},'silence':{'enabled':False},'_resolve_youtube_sources':True}
            with (patch.object(pipeline,'media_duration',return_value=60),
                  patch.object(rp,'resolve_source',return_value=([source],[])),patch.object(rp,'generate') as generate):
                result=engine.run({'vod_id':'123','streamer':'stintik'},cfg,lambda *a:None)
            generate.assert_not_called()
            self.assertEqual(result['moments'][0]['video_title'],'СТИНТ СМОТРИТ: Точное название')
            self.assertEqual(len(list(Path(result['folder']).glob('* — источник.json'))),1)

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
        def generate(root,login,row,segs,target,cancel=None,status=None):
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
    def test_new_copy_does_not_overwrite_existing_image_or_report(self):
        target=self.clip.with_name(self.clip.stem+' — ИИ-превью.jpg')
        target.write_bytes(b'previous');target.with_suffix('.json').write_text('old',encoding='utf8')
        def generate_copy(root,login,row,segs,out,**kw):
            out.write_bytes(b'new');return {'preview_text':row['preview_text'],'warnings':[],'source_url':'https://example.org'}
        with patch.object(rp,'generate',side_effect=generate_copy):
            result=rp.generate_saved_clip(self.root,self.clip,'kussia88',new_copy=True)
        self.assertFalse(result['existing']);self.assertEqual(target.read_bytes(),b'previous')
        self.assertEqual(target.with_suffix('.json').read_text(encoding='utf8'),'old')
        self.assertTrue(result['path'].endswith(' (2).jpg'));self.assertEqual(Path(result['path']).read_bytes(),b'new')

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
        target=self.clip.with_name(self.clip.stem+' — ИИ-превью.jpg');target.write_bytes(b'previous')
        with patch.object(rp,'generate') as generate:
            result=rp.generate_saved_clip(self.root,self.clip,'kussia88')
        generate.assert_not_called();self.assertEqual(result['preview_text'],'');self.assertEqual(target.read_bytes(),b'previous')
    def test_report_alone_is_not_overwritten(self):
        report=self.clip.with_name(self.clip.stem+' — ИИ-превью.json');report.write_text('{}',encoding='utf8')
        with self.assertRaisesRegex(rp.PreviewError,'отчёт без превью'):rp.generate_saved_clip(self.root,self.clip,'kussia88')
        self.assertEqual(report.read_text(encoding='utf8'),'{}')

class LocalModelGraphTests(unittest.TestCase):
    def test_three_reference_roles_and_headline_in_model_prompt(self):
        graph=pl.build_reaction_graph('style.png','person.png','source.png','ВЗЛОМАЛИ ДВЕРЬ',42)
        self.assertEqual(graph['4']['inputs']['image'],'style.png')
        self.assertEqual(graph['14']['inputs']['image'],'person.png')
        self.assertEqual(graph['15']['inputs']['image'],'source.png')
        for node in ('7','12'):
            self.assertEqual(graph[node]['inputs']['images.image0'],['4',0])
            self.assertEqual(graph[node]['inputs']['images.image1'],['14',0])
            self.assertEqual(graph[node]['inputs']['images.image2'],['15',0])
        prompt=graph['13']['inputs']['prompt']
        for part in ('ВЗЛОМАЛИ ДВЕРЬ','<ref_image_0>','<ref_image_1>','<ref_image_2>'):self.assertIn(part,prompt)
        self.assertNotIn('No text',prompt)
    def test_references_same_shape_and_original_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);cut=d/'person.png';style=d/'style.jpg'
            Image.new('RGBA',(300,404),(255,0,0,130)).save(cut)
            Image.new('RGB',(720,404),(30,60,90)).save(style)
            for i,source in enumerate((cut,style,Image.new('RGB',(640,480)))):
                target=d/f'ref{i}.png';pl._prepare_reference(source,target)
                with Image.open(target) as im:self.assertEqual(im.size,(1024,576))
            import io
            data=io.BytesIO();Image.new('RGB',(1024,576),(20,20,20)).save(data,'PNG')
            target=d/'output.jpg';pl._save_model_image(data.getvalue(),target);before=target.read_bytes()
            with self.assertRaises(pl.PreviewError):pl._save_model_image(data.getvalue(),target)
            self.assertEqual(target.read_bytes(),before)
    def test_single_style_reference_and_missing_install_fails_before_search(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'thumbnail_styles';folder.mkdir();(folder/'kussia88.jpg').write_bytes(b'x')
            self.assertEqual(rp.choose_style(d,'kussia88').name,'kussia88.jpg')
            (folder/'kussia88.png').write_bytes(b'x')
            with self.assertRaises(rp.PreviewError):rp.choose_style(d,'kussia88')
            with patch.object(rp,'find_candidates') as search:
                with self.assertRaises(pl.PreviewError):rp.generate(d,'kussia88',{'type':'reaction','preview_text':'ТЕСТ'},[],Path(d)/'x.jpg')
                search.assert_not_called()

    def test_full_local_flow_uses_three_refs_and_does_not_overlay_text(self):
        import io, types
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); home=root/'desktop'/'tools'/'ComfyUI_windows_portable';comfy=home/'ComfyUI'
            for p in [home/'python_embeded'/'python.exe',comfy/'main.py',comfy/'models'/'diffusion_models'/pl.MODEL,comfy/'models'/'text_encoders'/pl.ENCODER,comfy/'models'/'vae'/pl.VAE,comfy/'models'/'text_encoders'/pl.UPSAMPLER]:
                p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
            assets=root/'thumbnail_assets'/'kussia88';assets.mkdir(parents=True)
            Image.new('RGBA',(300,404),(240,10,10,150)).save(assets/'01.png')
            styles=root/'thumbnail_styles';styles.mkdir()
            Image.new('RGB',(720,404),(50,10,10)).save(styles/'kussia88.jpg')
            from PIL import ImageDraw
            buf=io.BytesIO();model=Image.new('RGB',(1024,576),'navy')
            ImageDraw.Draw(model).rectangle((40,390,520,465),fill='yellow')
            ImageDraw.Draw(model).rectangle((50,480,500,540),fill='white')
            model.save(buf,'PNG');saved_graph=[]
            class Reply:
                ok=True;content=buf.getvalue()
                def __init__(self,obj=None):self.obj=obj or {}
                def raise_for_status(self):pass
                def json(self):return self.obj
            class Session:
                trust_env=True
                def get(self,url,**kwargs):
                    if url.endswith('/system_stats'):return Reply()
                    if '/history/' in url:return Reply({'job1':{'outputs':{'10':{'images':[{'filename':'test.png'}]}}}})
                    if url.endswith('/view'):return Reply()
                    raise AssertionError(url)
                def post(self,url,json,**kwargs):saved_graph.append(json['prompt']);return Reply({'prompt_id':'job1'})
                def close(self):pass
            class Process:
                def __init__(self,*a,**k):self.stopped=False
                def poll(self):return 0 if self.stopped else None
                def terminate(self):self.stopped=True
                def wait(self,timeout):return 0
            output=root/'out.jpg';row={'type':'reaction','title':'полиция тест','preview_text':'ВЗЛОМАЛИ ДВЕРЬ','start':0,'end':10}
            source=Image.new('RGB',(1280,720),'blue')
            with (patch.object(pl.requests,'Session',Session),patch.object(pl.subprocess,'Popen',Process),
                  patch.object(pl,'_save_caption',side_effect=AssertionError('overlay used')),
                  patch.object(rp,'resolve_source',return_value=([{'id':'6EH2wmSPbMA','title':'полиция тест','channel':'x'}],['полиция тест'])),
                  patch.object(rp,'download_thumbnail',return_value=(source,'https://example.com/thumbnail.jpg'))):
                info=rp.generate(root,'kussia88',row,[{'start':1,'text':'полиция тест'}],output)
            self.assertTrue(output.is_file());self.assertTrue(info['text_unverified']);self.assertEqual(len(saved_graph),1)
            self.assertIn('ВЗЛОМАЛИ ДВЕРЬ',saved_graph[0]['13']['inputs']['prompt'])
            self.assertEqual(len(list((comfy/'input').glob('narezko_*.png'))),0)
            with Image.open(output) as im:self.assertEqual(im.size,(1280,720))

if __name__=='__main__':unittest.main()

class SourceOverrideTests(unittest.TestCase):
    def test_generic_false_match_rejected(self):
        class Run:
            returncode=0
            stdout=json.dumps({'entries':[{'id':'5PIZvgBGzLU','title':'Жена была уверена: муж никуда не денется'}]},ensure_ascii=False).encode()
        row={'start':0,'end':10,'title':'МУЖ И ЖЕНА МЕНЯЮТСЯ ПАРТНЁРАМИ ЭКСПЕРИМЕНТ НА ГРАНИ РАЗВОДА'}
        with self.assertRaises(rp.PreviewError):
            rp.find_candidates(row,[{'start':1,'text':'жена муж была никуда'}],lambda *a,**kw:Run())
    def test_link_validation(self):
        self.assertEqual(rp.youtube_video_id('https://www.youtube.com/watch?v=Lx_9n0PXg58'),'Lx_9n0PXg58')
        self.assertEqual(rp.youtube_video_id('https://youtu.be/Lx_9n0PXg58?t=20'),'Lx_9n0PXg58')
        for url in ('http://youtube.com/watch?v=Lx_9n0PXg58','https://youtube.com.evil.test/watch?v=Lx_9n0PXg58','https://youtube.com/watch?v=Lx_9n0PXg58&v=5PIZvgBGzLU'):
            with self.assertRaises(rp.PreviewError):rp.youtube_video_id(url)
    def test_manual_source_skips_search(self):
        with (patch.object(pl,'check_install'),patch.object(rp,'choose_cutout',return_value=Path('x.png')),
              patch.object(rp,'choose_style',return_value=Path('style.jpg')),
              patch.object(rp,'find_candidates') as search,
              patch.object(rp,'download_thumbnail',return_value=(object(),'https://example.com')) as download,
              patch.object(pl,'generate_reaction')):
            info=rp.generate('/unused','name',{'type':'reaction','preview_text':'ТЕПЛЫЙ ПРИЕМ'},[],Path('/unused/x.jpg'),source_url='https://www.youtube.com/watch?v=Lx_9n0PXg58')
        search.assert_not_called();download.assert_called_once_with('Lx_9n0PXg58');self.assertTrue(info['source_manually_selected'])
