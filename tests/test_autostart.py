import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import windows_autostart
import single_instance

class StartupTests(unittest.TestCase):
    def test_autostart_uses_per_user_pythonw_and_can_disable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'Бот нарезок';root.mkdir();(root/'app.py').touch();(root/'pythonw.exe').touch()
            reg=MagicMock();reg.HKEY_CURRENT_USER=1;reg.KEY_SET_VALUE=2;reg.REG_SZ=3
            key=MagicMock();reg.OpenKey.return_value.__enter__.return_value=key
            with patch.object(windows_autostart,'os',types.SimpleNamespace(name='nt',path=os.path)),patch.object(windows_autostart.sys,'executable',str(root/'python.exe')),patch.dict(sys.modules,{'winreg':reg}):
                windows_autostart.set_autostart(root,True)
                args=reg.SetValueEx.call_args.args
                self.assertIn('pythonw.exe',args[4]);self.assertIn('app.py',args[4]);self.assertIn('"',args[4])
                windows_autostart.set_autostart(root,False)
                reg.DeleteValue.assert_called_once_with(key,windows_autostart.ENTRY)
    def test_second_instance_is_detected_and_handle_closed(self):
        import ctypes
        kernel=MagicMock();kernel.CreateMutexW.return_value=123
        fakeos=types.SimpleNamespace(name='nt',path=os.path)
        with patch.object(single_instance,'os',fakeos),patch.object(ctypes,'WinDLL',return_value=kernel,create=True),patch.object(ctypes,'get_last_error',return_value=183,create=True):
            with self.assertRaises(single_instance.AlreadyRunning):single_instance.acquire('/tmp/myproject')
        kernel.CloseHandle.assert_called_once_with(123)
    def test_first_instance_can_release_handle(self):
        import ctypes
        kernel=MagicMock();kernel.CreateMutexW.return_value=123
        fakeos=types.SimpleNamespace(name='nt',path=os.path)
        with patch.object(single_instance,'os',fakeos),patch.object(ctypes,'WinDLL',return_value=kernel,create=True),patch.object(ctypes,'get_last_error',return_value=0,create=True):
            lock=single_instance.acquire('/tmp/myproject');single_instance.release(lock)
        kernel.CloseHandle.assert_called_once_with(123)

if __name__=='__main__':unittest.main()
