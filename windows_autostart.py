"""Optional per-user Windows logon startup, no elevation required."""
from pathlib import Path
import os
import subprocess
import sys

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
ENTRY = 'NarezkoBotDesktop'


def set_autostart(root, enabled):
    if os.name != 'nt':
        raise RuntimeError('Автозапуск доступен только в Windows.')
    import winreg
    root=Path(root).resolve()
    app=root/'app.py'
    exe=Path(os.path.abspath(sys.executable)).with_name('pythonw.exe')
    if enabled and (not app.is_file() or not exe.is_file()):
        raise RuntimeError('Не найден app.py или pythonw.exe. Проверьте установку приложения.')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY,0,winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key,ENTRY,0,winreg.REG_SZ,subprocess.list2cmdline([str(exe),str(app)]))
        else:
            try:winreg.DeleteValue(key,ENTRY)
            except FileNotFoundError:pass
