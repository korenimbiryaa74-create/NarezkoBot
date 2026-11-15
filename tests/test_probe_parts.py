"""Д19, R1: stream_cutter.probe_parts — проверка частей рендера параллельно, без ffprobe и без видео."""
import random
import threading
import time
import unittest
from unittest.mock import patch

import stream_cutter as sc


class ProbePartsTests(unittest.TestCase):
    def test_order_progress_and_parallel(self):
        parts = [f'part{i:05d}.mkv' for i in range(45)]
        active = {'now': 0, 'max': 0}
        lock = threading.Lock()
        main = threading.get_ident()
        calls = []

        def fake_probe(path, ffprobe):
            self.assertEqual(ffprobe, 'ffprobe.exe')
            with lock:
                active['now'] += 1
                active['max'] = max(active['max'], active['now'])
            time.sleep(random.uniform(0.001, 0.02))   # части завершаются не по порядку
            with lock:
                active['now'] -= 1
            return {'path': path}

        def progress(msg):
            self.assertEqual(threading.get_ident(), main)  # колбэк — из основного потока, как раньше
            calls.append(msg)

        with patch.object(sc, 'probe', side_effect=fake_probe):
            got = sc.probe_parts(parts, 'ffprobe.exe', progress)
        self.assertEqual([x['path'] for x in got], parts)
        self.assertEqual(calls, ['Проверено частей: 20/45', 'Проверено частей: 40/45', 'Проверено частей: 45/45'])
        self.assertGreater(active['max'], 1)
        self.assertLessEqual(active['max'], sc.PROBE_WORKERS)

    def test_error_of_one_part_propagates(self):
        def fake_probe(path, ffprobe):
            if path == 'b':
                raise sc.CutterError('ffprobe failed: битая часть')
            return {}
        with patch.object(sc, 'probe', side_effect=fake_probe):
            with self.assertRaises(sc.CutterError):
                sc.probe_parts(['a', 'b', 'c'], 'ffprobe')

    def test_empty_and_no_callback(self):
        with patch.object(sc, 'probe', side_effect=AssertionError('не вызывать')):
            self.assertEqual(sc.probe_parts([], 'ffprobe', lambda m: self.fail(m)), [])
        with patch.object(sc, 'probe', return_value={'ok': 1}):
            self.assertEqual(sc.probe_parts(['one'], 'ffprobe'), [{'ok': 1}])


if __name__ == '__main__':
    unittest.main()
