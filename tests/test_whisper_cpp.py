import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import whisper_cpp
import transcribe

class WhisperCppTests(unittest.TestCase):
    def test_offsets_are_relative_to_source(self):
        data={'transcription':[{'text':' привет ', 'offsets':{'from':1200,'to':2300}}]}
        self.assertEqual(whisper_cpp.parse_segments(data,600,600),[{'start':601.2,'end':602.3,'text':'привет'}])
    def test_bad_timestamps_rejected(self):
        with self.assertRaises(transcribe.TranscribeError):
            whisper_cpp.parse_segments({'transcription':[{'text':'x','offsets':{'from':0,'to':999999}}]},0,10)
    def test_clamps_small_overshoot_and_skips_empty_segments(self):
        data={'transcription':[{'text':' ','offsets':None},{'text':'a','offsets':{'from':-20,'to':500}},{'text':'b','offsets':{'from':9900,'to':10200}}]}
        rows=whisper_cpp.parse_segments(data,600,10)
        self.assertEqual([(r['start'],r['end']) for r in rows],[(600.,600.5),(609.9,610.)])
    def test_bad_timestamps_report_safe_numeric_context(self):
        data={'transcription':[{'text':'PRIVATE TEXT','offsets':{'from':9000,'to':45000}}]}
        with self.assertRaises(whisper_cpp.InvalidWhisperSegments) as err: whisper_cpp.parse_segments(data,600,10)
        self.assertIn('сегмент 1/1',str(err.exception));self.assertIn('9–45',str(err.exception))
        self.assertNotIn('PRIVATE TEXT',str(err.exception))
    def test_invalid_text_segment_not_silently_discarded(self):
        for offsets in ({'from':'nan','to':10},{'from':True,'to':10},{'from':11000,'to':11000},{'from':8000,'to':2000}):
            with self.assertRaises(whisper_cpp.InvalidWhisperSegments):
                whisper_cpp.parse_segments({'transcription':[{'text':'abc','offsets':offsets}]},0,10)
    def test_exact_last_whisper_overrun_from_log_is_bounded_and_marked(self):
        rows=whisper_cpp.parse_segments({'transcription':[{'text':'final', 'offsets':{'from':299000,'to':329000}}]},8100,300)
        self.assertEqual(rows,[{'start':8399.,'end':8400.,'text':'final','timing_warning':'end_clipped'}])
    def test_bad_nonfinal_and_severe_overrun_still_rejected(self):
        for segments in ([{'text':'x','offsets':{'from':290000,'to':320000}},{'text':'next','offsets':{'from':299000,'to':300000}}],
                         [{'text':'x','offsets':{'from':290000,'to':330501}}]):
            with self.assertRaises(whisper_cpp.InvalidWhisperSegments): whisper_cpp.parse_segments({'transcription':segments},0,300)
    def test_absent_tools_gives_clear_error(self):
        with patch.object(whisper_cpp,'CLI',Path('/does-not-exist')):
            with self.assertRaises(transcribe.TranscribeError): whisper_cpp.transcribe_file('any')
    def test_cli_result_and_time_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'проект'; cli=root/'whisper'/'whisper-cli.exe'; model=root/'whisper-model'/'model.bin'
            cli.parent.mkdir(parents=True); model.parent.mkdir(); cli.touch(); model.touch()
            def run(cmd, **kw):
                import json
                self.assertEqual(cmd[cmd.index('-m')+1], str(Path('..')/'whisper-model'/'model.bin'))
                self.assertEqual(kw['cwd'],str(cli.parent))
                out=Path(cmd[cmd.index('-of')+1]).with_suffix('.json')
                out.write_text(json.dumps({'transcription':[{'text':'ok','offsets':{'from':1000,'to':2000}}]}))
                class Result: returncode=0; stderr=''
                return Result()
            with patch.object(whisper_cpp,'CLI',cli), patch.object(whisper_cpp,'MODEL',model), \
                 patch.object(transcribe,'find_tools',return_value=('ffmpeg','ffprobe')), \
                 patch.object(transcribe,'probe',return_value={'format':{'duration':100},'streams':[{'codec_type':'audio'}]}), \
                 patch.object(transcribe,'extract_audio'), patch.object(whisper_cpp.subprocess,'run',side_effect=run):
                self.assertEqual(whisper_cpp.transcribe_file('v',start=50,end=60),[{'start':51.,'end':52.,'text':'ok'}])

if __name__=='__main__': unittest.main()
