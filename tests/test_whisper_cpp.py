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
    def test_absent_tools_gives_clear_error(self):
        with patch.object(whisper_cpp,'CLI',Path('/does-not-exist')):
            with self.assertRaises(transcribe.TranscribeError): whisper_cpp.transcribe_file('any')
    def test_cli_result_and_time_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cli=root/'whisper-cli.exe'; model=root/'model.bin'
            cli.touch(); model.touch()
            def run(cmd, **kw):
                import json
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
