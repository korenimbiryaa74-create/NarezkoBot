import unittest
from unittest.mock import MagicMock, patch
# The Linux test runner may not have Tk installed; widget methods are mocked.
try:
    from app import App
except ModuleNotFoundError as exc:
    if exc.name != 'tkinter': raise
    import sys, types
    from unittest.mock import patch
    fake=types.ModuleType('tkinter'); fake.TclError=type('TclError',(Exception,),{})
    fake.ttk=types.ModuleType('tkinter.ttk'); fake.messagebox=types.ModuleType('tkinter.messagebox'); fake.filedialog=types.ModuleType('tkinter.filedialog')
    with patch.dict(sys.modules, {'tkinter':fake,'tkinter.ttk':fake.ttk,'tkinter.messagebox':fake.messagebox,'tkinter.filedialog':fake.filedialog}):
        from app import App

class DiagnosticsUITests(unittest.TestCase):
    def setUp(self):
        self.app=App.__new__(App)
        self.app.window=MagicMock(); self.app.status=MagicMock(); self.app.events=MagicMock();self.app.notifications=MagicMock()
        self.app.jobs=MagicMock(); self.app.streams=MagicMock(); self.app.service=MagicMock()
    def test_copy_selected_job_detail(self):
        self.app.jobs.selection.return_value=('7',)
        self.app.service.store.list_jobs.return_value=[{'id':7,'local_name':'demo.mp4','streamer':'test','state':'retry','stage':'Ожидает повтора','detail':'TranscribeError: причина'}]
        self.app.copy_job_detail()
        text=self.app.window.clipboard_append.call_args.args[0]
        self.assertIn('demo.mp4',text);self.assertIn('TranscribeError: причина',text)
    def test_copy_all_events(self):
        self.app.service.logs.return_value=[(0,'Первая ошибка'),(1,'Вторая ошибка')]
        self.app.copy_events()
        text=self.app.window.clipboard_append.call_args.args[0]
        self.assertIn('Первая ошибка',text);self.assertIn('Вторая ошибка',text)
    def test_select_and_copy_readonly_event_text(self):
        self.app.events.get.return_value='выделенный текст'
        self.assertEqual(self.app.select_events(),'break')
        self.app.events.tag_add.assert_called_once_with('sel','1.0','end-1c')
        self.assertEqual(self.app.copy_event_selection(),'break')
        self.app.window.clipboard_append.assert_called_once_with('выделенный текст')
    def test_refresh_preserves_selected_event_text(self):
        self.app.service._monitor=None
        self.app.service.store.list_jobs.return_value=[]
        self.app.service.logs.return_value=[(0,'новые события')]
        self.app.jobs.selection.return_value=()
        self.app.streams.get_children.return_value=()
        self.app.jobs.get_children.return_value=()
        self.app.events.tag_ranges.return_value=('1.0','1.3')
        self.app.refresh()
        self.app.events.delete.assert_not_called()
        self.app.window.after.assert_called_once()

class ProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        import manual_blur
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        manual_blur.save_profile(self.root,'Kussia88',[])
        self.app=App.__new__(App);self.app.variables={'streamers':MagicMock()}
        self.app.variables['streamers'].get.return_value='other'
        self.app.config={'monitor':{'profiles_imported':False}}
        self.app.save=MagicMock(return_value=True);self.app.banner_info=MagicMock()
    def test_migrate_existing_profiles_once_without_removing_other_names(self):
        with patch.dict(App._import_profiles_once.__globals__,{'ROOT':self.root}):self.app._import_profiles_once()
        self.app.variables['streamers'].set.assert_called_once_with('other, kussia88')
        self.assertTrue(self.app.config['monitor']['profiles_imported']);self.app.save.assert_called_once()
        self.app._import_profiles_once();self.app.save.assert_called_once()
    def test_new_profile_adds_streamer_and_saves_settings(self):
        self.app._blur_saved('kussia88')
        self.app.variables['streamers'].set.assert_called_with('other, kussia88')
        self.app.save.assert_called_once()
    def test_new_profile_no_duplicate(self):
        self.app.variables['streamers'].get.return_value='KUSSIA88, other'
        self.app._blur_saved('kussia88')
        self.app.variables['streamers'].set.assert_called_with('KUSSIA88, other')
