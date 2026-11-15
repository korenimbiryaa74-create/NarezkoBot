# R2 (docs/RENDER_PROFILE.md, Д9): pipeline.render_clip включает аппаратный декод, проверка один раз на источник.
# Без сети и без настоящего ffmpeg: detect_hwaccel и рендер подменены.
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline
import stream_cutter

INFO = {'format': {'duration': '100'}, 'streams': [{'codec_type': 'audio'}, {'codec_type': 'video'}]}


class SourceHwaccelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'vod.mp4'
        self.src.write_bytes(b'x' * 10)
        pipeline._HWACCEL_CACHE.clear()
        self.addCleanup(pipeline._HWACCEL_CACHE.clear)

    def test_detected_once_per_source(self):
        with patch.object(stream_cutter, 'detect_hwaccel', return_value='cuda') as det:
            self.assertEqual(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'), 'cuda')
            self.assertEqual(pipeline.source_hwaccel('ffmpeg', str(self.src), 'h264_nvenc'), 'cuda')
        self.assertEqual(det.call_count, 1)
        self.assertEqual(det.call_args.args[2], 'auto')

    def test_new_file_or_encoder_is_checked_again(self):
        with patch.object(stream_cutter, 'detect_hwaccel', return_value='cuda') as det:
            pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc')
            self.src.write_bytes(b'y' * 20)  # перекачанный VOD: другой размер
            pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc')
            pipeline.source_hwaccel('ffmpeg', self.src, 'libx264')
        self.assertEqual(det.call_count, 3)

    def test_failed_detection_is_cached_as_cpu(self):
        with patch.object(stream_cutter, 'detect_hwaccel', return_value=None) as det:
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'))
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'))
        self.assertEqual(det.call_count, 1)

    def test_failed_detection_is_retried_after_ten_minutes(self):
        # замечание Ё7: видеокарта могла быть занята другим рендером — «CPU» не навсегда
        now = [1000.0]
        with patch.object(pipeline.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(stream_cutter, 'detect_hwaccel', side_effect=[None, 'cuda']) as det:
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'))
            now[0] += 599
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'))
            now[0] += 2
            self.assertEqual(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'), 'cuda')
            now[0] += 10000  # удачное определение не устаревает
            self.assertEqual(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'), 'cuda')
        self.assertEqual(det.call_count, 2)

    def test_none_missing_file_and_errors_fall_back_to_cpu(self):
        with patch.object(stream_cutter, 'detect_hwaccel', side_effect=AssertionError('не вызывать')):
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc', 'none'))
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc', None))
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', Path(self.tmp.name) / 'нет.mp4', 'h264_nvenc'))
        with patch.object(stream_cutter, 'detect_hwaccel', side_effect=FileNotFoundError('ffmpeg')):
            self.assertIsNone(pipeline.source_hwaccel('ffmpeg', self.src, 'h264_nvenc'))


class RenderClipHwaccelTests(unittest.TestCase):
    def _render(self, silence_cfg, hw):
        messages = []
        with patch.object(stream_cutter, 'require_tools', return_value=('ffmpeg', 'ffprobe')), \
             patch.object(stream_cutter, 'pick_encoder', return_value='h264_nvenc'), \
             patch.object(stream_cutter, 'probe', return_value=INFO), \
             patch.object(stream_cutter, 'render_file') as render, \
             patch.object(pipeline, 'source_hwaccel', return_value=hw) as det, \
             patch.object(pipeline, 'media_duration', return_value=10):
            pipeline.render_clip('vod.mp4', 'out.mp4', {'start': 30, 'end': 40}, 0, silence_cfg, messages.append)
        return render.call_args.args[1], det, messages

    def test_render_gets_hardware_decoder(self):
        args, det, messages = self._render({'enabled': False}, 'cuda')
        self.assertEqual(args.hwaccel_flag, 'cuda')
        self.assertFalse(args.fast)
        self.assertEqual(det.call_args.args[1:], ('vod.mp4', 'h264_nvenc', 'auto'))
        self.assertIn('Декод видео: cuda (видеокарта)', messages)
        self.assertLess(messages.index('Декод видео: cuda (видеокарта)'), messages.index('Запуск рендера'))

    def test_setting_none_is_passed_and_cpu_reported(self):
        args, det, messages = self._render({'enabled': False, 'hwaccel': 'none'}, None)
        self.assertIsNone(args.hwaccel_flag)
        self.assertEqual(det.call_args.args[3], 'none')
        self.assertIn('Декод видео: CPU', messages)

    def test_chunk_command_uses_decoder_and_cpu_retry_drops_it(self):
        with tempfile.TemporaryDirectory() as d:
            args = stream_cutter.parse_args([]); stream_cutter.apply_speed(args, 'turbo')
            args.vcodec = 'libx264'; args.blur_vf = None
            script = str(Path(d) / 'filter.txt')
            hw = stream_cutter.chunk_cmd('ffmpeg', 'video.mp4', [(1, 3)], 'o.mkv', args, 1, 'cuda', 1, [], script)
            cpu = stream_cutter.chunk_cmd('ffmpeg', 'video.mp4', [(1, 3)], 'o.mkv', args, 1, None, 1, [], script)
        self.assertEqual(hw[hw.index('-hwaccel') + 1], 'cuda')
        self.assertLess(hw.index('-hwaccel'), hw.index('-i'))
        self.assertNotIn('-hwaccel', cpu)


if __name__ == '__main__':
    unittest.main()
