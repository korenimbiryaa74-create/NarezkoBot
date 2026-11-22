import json
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import banner_blur
import youtube_titles as yt
from youtube_queue import YouTubeQueue


class TitleTests(unittest.TestCase):
    def test_streamer_and_local_test_detection(self):
        self.assertEqual(yt.streamer_name({'streamer':'Kussia88'}),'Каша')
        self.assertEqual(yt.streamer_name({'streamer':'test','local_name':'[11-8-26] Nix - Обсуждение.mp4'}),'Никс')
        self.assertEqual(yt.streamer_name({'streamer':'test','local_name':'[11-5-26] kussia88 - Запись.mp4'}),'Каша')
        self.assertEqual(yt.streamer_name({'streamer':'test','local_name':'Запись.mp4'}),'')

    def test_expected_formats_one_or_two_points(self):
        rows=[{'type':'reaction'},{'type':'discussion'},{'type':'discussion'}]
        response={'youtube_titles':[{'n':1,'target':'История про спорт'},
            {'n':2,'hook':'Вывернули спину','second':'Новая история лечения','topic':'кости и снимки спины'},
            {'n':3,'hook':'Одна история','second':'','topic':'здоровье'}]}
        titles=yt.format_titles(json.dumps(response,ensure_ascii=False),rows,'Каша')
        self.assertEqual(titles[0],'КАША РЕАГИРУЕТ НА: История про спорт')
        self.assertEqual(titles[1],'Вывернули спину! Новая история лечения | Каша про кости и снимки спины')
        self.assertEqual(titles[2],'Одна история | Каша про здоровье')
        self.assertEqual(yt.format_titles('```json\n'+json.dumps(response)+'\n```',rows,'Каша'),titles)

    def test_reject_missing_duplicate_incorrect_and_limit_length(self):
        rows=[{'type':'discussion'}]
        for data in ({'youtube_titles':[]},{'youtube_titles':[{'n':2,'hook':'a','topic':'b'}]},
                     {'youtube_titles':[{'n':1,'hook':'','topic':'b'}]}):
            with self.assertRaises(ValueError):yt.format_titles(json.dumps(data),rows,'Никс')
        data={'youtube_titles':[{'n':1,'hook':'Событие '*40,'second':'','topic':'кости и лечение'}]}
        title=yt.format_titles(json.dumps(data,ensure_ascii=False),rows,'Никс')[0]
        self.assertLessEqual(len(title.encode('utf-16-le'))//2,100)
        self.assertTrue(title.endswith('| Никс про кости и лечение'))

    def test_rendered_transcript_and_title_snapshot_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);video=root/'clip.mp4';video.write_bytes(b'video')
            video.with_suffix('.txt').write_text('готовый текст после вырезания донатов',encoding='utf-8')
            self.assertIn('готовый текст',yt.document([str(video)],[{'type':'reaction'}]))
            q=YouTubeQueue(root/'q.sqlite');q.bind('x','UC'+'a'*22,audience='not_kids')
            self.assertEqual(q.enqueue(1,'x',[video],titles=['X РЕАГИРУЕТ НА: тема']),1)
            self.assertEqual(q.enqueue(1,'x',[video],titles=['X РЕАГИРУЕТ НА: тема']),0)
            self.assertEqual(q.list_uploads()[0]['title'],'X РЕАГИРУЕТ НА: тема')
            with self.assertRaises(ValueError):q.enqueue(1,'x',[video],titles=['другое'])

class BlurFilterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg отсутствует в PATH')
    def test_actual_crop_at_edge_small_chroma_plane(self):
        # Requested 20px crop is clipped to 18px by FFmpeg at frame edge.
        graph=banner_blur.filter_for_chunk([(46,0,20,20,[(0,1)])])
        # Strictly less than half, including FFmpeg builds rejecting equality.
        self.assertIn('floor((min(w,h)-1)/2)',graph)
        self.assertIn('floor((min(cw,ch)-1)/2)',graph)
        result=subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=64x64:rate=1',
            '-vf',graph,'-frames:v','1','-f','null','-'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        def frame(filter_args):
            return subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=64x64:rate=1']+filter_args+
                ['-frames:v','1','-pix_fmt','yuv420p','-f','rawvideo','-'],capture_output=True,check=True).stdout
        before,after=frame([]),frame(['-vf',graph])
        changed=[i for i,(a,b) in enumerate(zip(before[:4096],after[:4096])) if a!=b]
        self.assertTrue(changed)
        self.assertTrue(all(i%64>=46 and i//64<20 for i in changed))


class DonationCacheTests(unittest.TestCase):
    def test_donations_reused_and_change_of_boundaries_requests_again(self):
        from chatgpt_finder import ChatGPTFinder
        response=json.dumps({'donation_cuts':[{'reaction':1,'start':'00:00:12.000','end':'00:00:15.000','confidence':'high'}]})
        class Web:
            def __init__(self,**kwargs):self.calls=0;self.status_callback=None
            def ask(self,prompt,files,until=None):
                self.calls+=1
                if until:self.assert_predicate=until(response)
                return {'text':response}
            def close(self):pass
        with tempfile.TemporaryDirectory() as d:
            web=Web();finder=ChatGPTFinder(d,web_factory=lambda **kw:web)
            segs=[{'start':12.,'end':15.,'text':'текст доната'}]
            moments=[{'type':'reaction','start':10.,'end':100.}]
            events=[];progress=lambda stage,msg:events.append(msg)
            first=finder.find_donations(segs,moments,progress)
            second=finder.find_donations(segs,moments,progress)
            self.assertEqual(first,second);self.assertEqual(web.calls,1)
            self.assertTrue(any('кэш ответа о донатах' in e for e in events))
            finder.find_donations(segs,[{'type':'reaction','start':10.,'end':101.}],progress)
            self.assertEqual(web.calls,2)


class TitleGenerationCacheTests(unittest.TestCase):
    def test_generated_title_is_cached_and_uses_rendered_transcript(self):
        from chatgpt_finder import ChatGPTFinder
        answer=json.dumps({'youtube_titles':[{'n':1,'target':'Сюжет о спорте'}]},ensure_ascii=False)
        class Web:
            def __init__(self,**kw):self.calls=0
            def close(self):pass
            def ask(self,prompt,files,until=None):
                self.calls+=1
                self.document=Path(files[0]).read_text(encoding='utf-8')
                if until and not until(answer):raise AssertionError('ответ не прошёл проверку')
                return {'text':answer}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);video=root/'готовое.mp4';video.write_bytes(b'video')
            video.with_suffix('.txt').write_text('Расшифровка готового видео про спорт',encoding='utf-8')
            web=Web();finder=ChatGPTFinder(root,web_factory=lambda **kw:web)
            job={'streamer':'kussia88'};outcome={'outputs':[str(video)],'moments':[{'type':'reaction'}]}
            progress=lambda *args:None
            self.assertEqual(finder.youtube_titles(job,outcome,progress),['КАША РЕАГИРУЕТ НА: Сюжет о спорте'])
            self.assertEqual(finder.youtube_titles(job,outcome,progress),['КАША РЕАГИРУЕТ НА: Сюжет о спорте'])
            self.assertEqual(web.calls,1)
            self.assertIn('Расшифровка готового видео',web.document)


if __name__=='__main__':unittest.main()
