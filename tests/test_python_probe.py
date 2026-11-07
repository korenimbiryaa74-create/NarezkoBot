import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

PROBE = Path(__file__).resolve().parents[1] / 'desktop' / 'python_probe.py'
spec = importlib.util.spec_from_file_location('python_probe', PROBE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

class PythonProbeTests(unittest.TestCase):
    def test_requires_python_315_and_64bit(self):
        self.assertTrue(probe.acceptable((3,15,0), 8))
        self.assertFalse(probe.acceptable((3,15,0), 4))
        self.assertFalse(probe.acceptable((3,14,1), 8))
        self.assertFalse(probe.acceptable((3,16,0), 8))
    def test_probe_process_reports_correct_status(self):
        result = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True)
        expected = probe.acceptable(sys.version_info, __import__('struct').calcsize('P'))
        self.assertEqual(result.returncode == 0, expected)
        if expected: self.assertEqual(Path(result.stdout.strip()), Path(sys.executable))
        else: self.assertIn('Need 64-bit Python 3.15', result.stderr)
    def test_installer_uses_probe_file_not_inline_quoted_code_for_version(self):
        installer = PROBE.with_name('install.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("Join-Path $PSScriptRoot 'python_probe.py'", installer)
        self.assertNotIn('struct.calcsize("P")', installer)
        self.assertIn('PYTHON_MANAGER_AUTOMATIC_INSTALL', installer)
        self.assertIn("Read-Host 'Путь к python.exe'", installer)

if __name__ == '__main__': unittest.main()
