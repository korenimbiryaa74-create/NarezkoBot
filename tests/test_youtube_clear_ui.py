"""Bulk queue action requires confirmation and reports how many were cleared."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from youtube_queue import YouTubeQueue


def stub(name, **kwargs):
    module=types.ModuleType(name)
    for key,value in kwargs.items():setattr(module,key,value)
    return module

class ClearUITests(unittest.TestCase):
    def test_clear_action_asks_then_reports(self):
        prompts=[];messages=[];logs=[];answer=[False]
        messagebox=stub('tkinter.messagebox',askyesno=lambda title,text:(prompts.append(text) or answer[0]),
                        showinfo=lambda title,text:messages.append(text),showerror=lambda *a:self.fail(str(a)))
        tkinter=stub('tkinter',ttk=stub('tkinter.ttk'),messagebox=messagebox,filedialog=stub('tkinter.filedialog'))
        modules={'tkinter':tkinter,'settings':stub('settings',Settings=object,import_legacy_groq_key=lambda x:None),
                 'service':stub('service',Service=object),'chatgpt_finder':stub('chatgpt_finder',ChatGPTFinder=object)}
        for name in ('banner_blur','stream_cutter','manual_blur','windows_autostart','single_instance'):modules[name]=stub(name)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,modules):
            sys.modules.pop('app',None)
            try:
                app=importlib.import_module('app').App.__new__(importlib.import_module('app').App)
                queue=YouTubeQueue(Path(tmp)/'jobs.sqlite3');queue.bind('__test__','UC'+'t'*22,audience='not_kids')
                video=Path(tmp)/'test.mp4';video.write_bytes(b'video')
                queue.enqueue(1,'ignored',[video],is_test=True)
                app.service=types.SimpleNamespace(youtube_queue=queue,log=logs.append,resume_youtube_after_review=lambda:None)
                app.youtube_clear_queue()
                self.assertEqual(queue.list_uploads()[0]['state'],'queued')
                answer[0]=True;app.youtube_clear_queue()
                self.assertEqual(queue.list_uploads()[0]['state'],'cancelled')
                self.assertIn('ВСЕХ профилей',prompts[-1]);self.assertIn('1',messages[-1]);self.assertIn('1',logs[-1])
            finally:sys.modules.pop('app',None)

if __name__=='__main__':unittest.main()
