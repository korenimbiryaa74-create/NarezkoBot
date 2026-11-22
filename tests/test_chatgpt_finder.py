import tempfile
import unittest
from pathlib import Path
from chatgpt_finder import ChatGPTFinder

class Web:
    calls=0
    def __init__(self,**kw): self.offscreen=False; self.timeout=0
    def ask(self,prompt,files,until):
        self.calls+=1
        answer='{"reactions":[{"start":"00:00:10","end":"00:00:40","title":"Видео","type":"reaction"}]}'
        assert Path(files[0]).read_text(encoding='utf8') and until(answer)
        return {'text':answer}
    def close(self): pass

class Tests(unittest.TestCase):
    def test_full_transcript_and_cached_answer(self):
        with tempfile.TemporaryDirectory() as d:
            web=Web();finder=ChatGPTFinder(d,web_factory=lambda **kw:web)
            args=(str(Path(d)/'audio.mp4'),[{'start':0,'end':60,'text':'Тест'}],60,'nix',{'min_reaction_minutes':0},lambda *a:None)
            first=finder(*args); second=finder(*args)
            self.assertEqual(first,second); self.assertEqual(first[0]['start'],10)
            self.assertEqual(web.calls,1)
            self.assertIsNone(finder._web)
    def test_unconfirmed_delivery_is_not_retried(self):
        class Broken(Web):
            def ask(self,*a,**kw): self.calls+=1; raise RuntimeError('uncertain delivery')
        with tempfile.TemporaryDirectory() as d:
            web=Broken();finder=ChatGPTFinder(d,web_factory=lambda **kw:web)
            with self.assertRaisesRegex(RuntimeError,'uncertain'):
                finder(str(Path(d)/'audio.mp4'),[{'start':0,'end':60,'text':'Тест'}],60,'nix',{'min_reaction_minutes':0},lambda *a:None)
            self.assertEqual(web.calls,1)
if __name__=='__main__':unittest.main()
