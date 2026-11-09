"""Exercise channel-list UI logic without a real display or YouTube account."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from youtube_queue import YouTubeQueue


def stub(name, **attrs):
    m=types.ModuleType(name)
    for k,v in attrs.items():setattr(m,k,v)
    return m


class Variable:
    def __init__(self,value=''):self.value=value
    def set(self,value):self.value=value
    def get(self):return self.value


class Tree:
    def __init__(self):self.rows={};self.order=[];self.selected=[]
    def get_children(self):return tuple(self.order)
    def insert(self,parent,index,iid,values):self.rows[iid]=tuple(values);self.order.insert(index,iid)
    def item(self,iid,option=None,**kw):
        if 'values' in kw:self.rows[iid]=tuple(kw['values'])
        return self.rows[iid] if option=='values' else {'values':self.rows[iid]}
    def move(self,iid,parent,index):self.order.remove(iid);self.order.insert(index,iid)
    def delete(self,iid):self.order.remove(iid);del self.rows[iid]
    def selection(self):return tuple(self.selected)


class YouTubeProfileUITests(unittest.TestCase):
    def setUp(self):
        tkinter=stub('tkinter',ttk=stub('tkinter.ttk'),messagebox=stub('tkinter.messagebox'),filedialog=stub('tkinter.filedialog'))
        modules={'tkinter':tkinter,'settings':stub('settings',Settings=object,import_legacy_groq_key=lambda x:None),
                 'service':stub('service',Service=object),'chatgpt_finder':stub('chatgpt_finder',ChatGPTFinder=object)}
        for name in ['banner_blur','stream_cutter','manual_blur','windows_autostart','single_instance']:
            modules[name]=stub(name)
        self.patcher=patch.dict(sys.modules,modules);self.patcher.start();self.addCleanup(self.patcher.stop)
        sys.modules.pop('app',None);self.addCleanup(sys.modules.pop,'app',None)
        self.module=importlib.import_module('app')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.queue=YouTubeQueue(Path(self.tmp.name)/'state.sqlite3')
        self.app=self.module.App.__new__(self.module.App)
        self.app.service=types.SimpleNamespace(youtube_queue=self.queue)
        self.app.youtube_profiles=Tree()
        self.app.variables={key:Variable() for key in ('youtube_profile','youtube_speed','youtube_audience')}

    def test_saved_bindings_visible_even_without_uploads_and_update(self):
        self.queue.bind('test','UC'+'a'*22,1,audience='kids')
        self.queue.bind('__test__','UC'+'b'*22,16,audience='not_kids')
        self.assertEqual(self.queue.list_uploads(),[])
        self.app.refresh_youtube_profiles()
        tree=self.app.youtube_profiles
        self.assertEqual(tree.order,['__test__','test'])
        self.assertEqual(tree.rows['__test__'],('__test__ (тестовый)','UC'+'b'*22,'Все сразу','Не специально для детей'))
        self.assertEqual(tree.rows['test'][2:],('1 час','Для детей'))
        tree.selected=['test'];self.app.youtube_profile_selected()
        self.assertEqual(self.app.variables['youtube_profile'].get(),'test')
        self.assertEqual(self.app.variables['youtube_audience'].get(),'Предназначено для детей')
        self.app.variables['youtube_speed'].set('несохранённое значение')
        self.app.refresh_youtube_profiles()
        self.assertEqual(self.app.variables['youtube_speed'].get(),'несохранённое значение')
        self.assertEqual(tree.selection(),('test',))
        self.queue.bind('test','UC'+'a'*22,16,audience='not_kids')
        self.app.refresh_youtube_profiles()
        self.assertEqual(tree.rows['test'][2:],('16 часов','Не специально для детей'))
        tree.selected=['__test__'];self.app.youtube_profile_selected()
        self.assertEqual(self.app.variables['youtube_profile'].get(),'__test__')
        self.assertEqual(self.app.variables['youtube_speed'].get(),'Медленно: 16 часов')

    def test_missing_audience_is_visible(self):
        self.queue.bind('a','UC'+'a'*22)
        self.app.refresh_youtube_profiles()
        self.assertEqual(self.app.youtube_profiles.rows['a'][3],'Не указана')
        self.app.youtube_profiles.selected=['a'];self.app.youtube_profile_selected()
        self.assertEqual(self.app.variables['youtube_audience'].get(),'Выберите')

if __name__=='__main__':unittest.main()
