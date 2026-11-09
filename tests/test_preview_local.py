import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import preview_local as p
from desktop import preview_download_models as dl

class PreviewTests(unittest.TestCase):
    def test_graph_is_local_and_uses_reference(self):
        g=p.build_graph('ref.png','Тема ролика','Каша',123)
        self.assertEqual(g['4']['inputs']['image'],'ref.png')
        self.assertEqual(g['7']['inputs']['images.image0'],['4',0])
        self.assertEqual(g['8']['inputs']['seed'],123)
        self.assertIn('Тема ролика',g['13']['inputs']['prompt'])
        self.assertTrue(all(not n['class_type'].lower().startswith('api') for n in g.values()))
        with self.assertRaises(p.PreviewError):p.build_graph('x','тема','неизвестно',0)

    def test_missing_install_and_invalid_title(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(p.PreviewError):p.check_install(t)
        for v in ('', ' '*5, 'x'*91):
            with self.assertRaises(p.PreviewError):p.safe_label(v)

    def test_caption_cyrillic(self):
        with tempfile.TemporaryDirectory() as t:
            fonts=Path(t)/'Fonts';fonts.mkdir()
            import shutil
            font=next((x for x in (Path('/usr/share/fonts/msttcore/arialbd.ttf'),Path('C:/Windows/Fonts/arialbd.ttf')) if x.is_file()),None)
            if font is None:self.skipTest('Arial Bold is not installed')
            shutil.copy(font,fonts/'arialbd.ttf')
            source=io.BytesIO();Image.new('RGB',(640,360),'navy').save(source,'PNG')
            out=Path(t)/'out.jpg'
            with patch.dict('os.environ',{'WINDIR':t}):p._save_caption(source.getvalue(),'РУССКИЙ ТЕКСТ',out)
            with Image.open(out) as im:self.assertEqual(im.size,(1280,720))

    def test_cancel_wait(self):
        import threading
        e=threading.Event();e.set()
        with self.assertRaises(p.PreviewError):p._sleep(1,e)

    def test_downloader_does_not_replace_existing_model(self):
        import sys, types
        calls=[]
        fake=types.SimpleNamespace(hf_hub_download=lambda **kw:calls.append(kw))
        with tempfile.TemporaryDirectory() as t,patch.dict(sys.modules,{'huggingface_hub':fake}):
            name=next(iter(dl.FILES));path=Path(t)/name;path.parent.mkdir(parents=True);path.write_bytes(b'not correct')
            with self.assertRaises(RuntimeError):dl.download(t)
            self.assertEqual(path.read_bytes(),b'not correct');self.assertEqual(calls,[])

    def test_one_clip_no_youtube_and_cleans_up_own_server(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);home=root/'portable';(home/'ComfyUI').mkdir(parents=True);video=root/'ролик.mp4';video.write_bytes(b'original')
            class Process:
                terminated=False
                def poll(self):return None if not self.terminated else 0
                def terminate(self):self.terminated=True
                def wait(self,timeout):return 0
            process=Process()
            class R:
                ok=True
                def __init__(self,data=None,content=b''):self.data=data;self.content=content
                def raise_for_status(self):pass
                def json(self):return self.data
            class Session:
                trust_env=True
                def get(self,url,**kwargs):
                    self_test.assertTrue(url.startswith('http://127.0.0.1:'))
                    if '/system_stats' in url:return R({})
                    if '/history/' in url:return R({'job':{'outputs':{'10':{'images':[{'filename':'x.png'}]}}}})
                    return R(content=b'image')
                def post(self,url,**kwargs):
                    self_test.assertFalse(self.trust_env);return R({'prompt_id':'job'})
                def close(self):pass
            self_test=self
            def frame(video,dest,progress):dest.write_bytes(b'frame')
            def save(data,title,path):path.write_bytes(data)
            with patch.object(p,'check_install',return_value=home),patch.object(p,'choose_frame',side_effect=frame),patch.object(p,'_save_caption',side_effect=save),patch.object(p.subprocess,'Popen',return_value=process),patch.object(p.requests,'Session',return_value=Session()):
                result=p.generate_test(root,video,'Тема','Никс')
            self.assertEqual(result.read_bytes(),b'image');self.assertEqual(video.read_bytes(),b'original');self.assertTrue(process.terminated)
            self.assertEqual(list((home/'ComfyUI'/'input').iterdir()),[])

if __name__=='__main__':unittest.main()
