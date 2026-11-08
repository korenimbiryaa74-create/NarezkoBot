import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pipeline
import stream_cutter
import full_cycle
from service import Service
from settings import Settings

class CommandTests(unittest.TestCase):
    def test_pcm_decode_restricted_to_range(self):
        class Result: returncode=0; stdout=b'data'; stderr=b''
        with patch('stream_cutter.run_bytes',return_value=Result()) as run:
            self.assertEqual(stream_cutter.decode_pcm('test.mp4',0,'ffmpeg',120,150),b'data')
        cmd=run.call_args.args[0]
        self.assertEqual(cmd[cmd.index('-ss')+1],'120.000000')
        self.assertEqual(cmd[cmd.index('-t')+1],'30.000000')
    def test_multisegment_render_limited_at_input(self):
        with tempfile.TemporaryDirectory() as d:
            args=stream_cutter.parse_args([]);stream_cutter.apply_speed(args,'turbo');args.vcodec='libx264';args.blur_vf=None
            cmd=stream_cutter.chunk_cmd('ffmpeg','video.mp4',[(100,102),(105,108)],'out.mkv',args,1,None,1,[],str(Path(d)/'filter.txt'))
            self.assertLess(cmd.index('-t'),cmd.index('-i'))
            self.assertEqual(cmd[cmd.index('-t')+1],'8.000')
    def test_no_silence_skips_audio_decode(self):
        info={'format':{'duration':'100'},'streams':[{'codec_type':'audio'},{'codec_type':'video'}]}
        with patch.object(stream_cutter,'require_tools',return_value=('ffmpeg','ffprobe')), patch.object(stream_cutter,'pick_encoder',return_value='libx264'), patch.object(stream_cutter,'probe',return_value=info), patch.object(stream_cutter,'analyse',side_effect=AssertionError('must skip')), patch.object(stream_cutter,'render_file') as render, patch.object(pipeline,'media_duration',return_value=10):
            messages=[]
            keeps,duration=pipeline.render_clip('test.mp4','out.mp4',{'start':30,'end':40},0,{'enabled':False},messages.append)
        self.assertEqual(keeps,[(30,40)]); self.assertEqual(duration,10)
        self.assertTrue(any('выключено' in m for m in messages)); render.assert_called_once()
    def test_wait_reports_elapsed_and_stops(self):
        import time
        logs=[]
        with pipeline.report_wait(logs.append,'Операция',interval=.02): time.sleep(.07)
        count=len(logs); time.sleep(.04)
        self.assertGreaterEqual(count,1);self.assertEqual(count,len(logs))
        self.assertIn('ожидание',logs[0]); self.assertNotIn('%',logs[0])

    def test_background_commands_use_windows_no_console_flag(self):
        with patch.object(stream_cutter.subprocess,'CREATE_NO_WINDOW',0x08000000,create=True), patch.object(stream_cutter.subprocess,'run') as run:
            stream_cutter.run(['ffprobe'])
            self.assertEqual(run.call_args.kwargs['creationflags'],0x08000000)
            stream_cutter.run_bytes(['ffmpeg'])
            self.assertEqual(run.call_args.kwargs['creationflags'],0x08000000)
    def test_vod_uses_installer_cli_location_automatically(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); observed=[]
            def download(vod,quality,out,**kwargs):
                observed.append(kwargs['cli']); raise RuntimeError('download stub')
            engine=pipeline.Pipeline(root,None,download=download)
            with self.assertRaisesRegex(RuntimeError,'download stub'):
                engine.run({'vod_id':'123','streamer':'abc'}, {})
            self.assertEqual(Path(observed[0]),root/'desktop'/'tools'/'TwitchDownloaderCLI'/'TwitchDownloaderCLI.exe')

    def test_expected_downloader_path_and_helpful_missing_error(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); engine=pipeline.Pipeline(root,lambda *a:[])
            with self.assertRaisesRegex(pipeline.PipelineError,'INSTALL.bat'):
                engine.run({'vod_id':'123','streamer':'abc'}, {})

class MeasuredTimelineTests(unittest.TestCase):
    def test_rejects_unexpected_part_length(self):
        info={'format':{'duration':'5','start_time':'0'},'streams':[{'codec_type':'video','start_time':'0','r_frame_rate':'25/1'}]}
        with self.assertRaises(stream_cutter.InvalidRenderTimeline):
            stream_cutter.measured_timeline([[(0,1)]],[info],info)
    def test_rejects_unexpected_final_length(self):
        part={'format':{'duration':'1','start_time':'0'},'streams':[{'codec_type':'video','start_time':'0','r_frame_rate':'25/1'}]}
        bad={'format':{'duration':'9','start_time':'0'},'streams':part['streams']}
        with self.assertRaises(stream_cutter.InvalidRenderTimeline):
            stream_cutter.measured_timeline([[(0,1)]],[part],bad)
    def test_invalid_timeline_is_not_auto_retried(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);settings=Settings(root/'config.json');settings.save(settings.load())
            class Engine:
                def __init__(self,*args):pass
                def run(self,job,cfg,progress):
                    progress('Рендер','проверка')
                    raise stream_cutter.InvalidRenderTimeline('плохая часть')
            svc=Service(root,settings,None,pipeline_factory=Engine);svc.store.add('1','x');svc.worker_once()
            self.assertEqual(svc.store.list_jobs()[0]['state'],'failed')

class ServiceLogTests(unittest.TestCase):
    def test_render_progress_visible_and_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); settings=Settings(root/'config.json'); cfg=settings.load()
            cfg['twitch']['client_secret']='SECRET';settings.save(cfg)
            class Engine:
                def __init__(self,*args):pass
                def run(self,job,cfg,progress):
                    progress('Нарезка','1/2');progress('Рендер','Рендер: 25% SECRET')
                    return {'outputs':[],'folder':'here'}
            svc=Service(root,settings,None,pipeline_factory=Engine);svc.store.add('123','x');svc.worker_once()
            log='\n'.join(text for _,text in svc.logs())
            self.assertIn('1/2',log);self.assertIn('Рендер: 25%',log);self.assertNotIn('SECRET',log)

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg unavailable')
class RenderIntegrationTests(unittest.TestCase):
    def test_nonzero_range_silence_and_transcript_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); src=root/'input.mp4'; (root/'_work').mkdir()
            expr=r'aevalsrc=if(between(t\,13\,17)\,0\,0.5*sin(2*PI*440*t)):s=16000:d=30'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i',expr,'-t','30','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            logs=[];out=root/'clip.mp4'
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keep,duration=pipeline.render_clip(src,out,{'start':10,'end':20},0,{'enabled':True,'min_cut':1.0,'max_gap':1.0},logs.append)
            self.assertGreaterEqual(keep[0][0],10); self.assertLessEqual(keep[-1][1],20)
            self.assertGreater(len(keep),1);self.assertLess(duration,9)
            self.assertTrue(any('Анализ тишины' in s for s in logs));self.assertTrue(any('Склейка' in s for s in logs))
            mapped,planned=full_cycle.remap_segments([{'start':11,'end':12,'text':'a'},{'start':18,'end':19,'text':'b'}],0,keep)
            self.assertAlmostEqual(mapped[0]['start'],1,delta=.2)
            self.assertLess(mapped[1]['start'],7)
            self.assertAlmostEqual(duration,planned,delta=.5)
    def test_27_parts_measured_timeline_tracks_audio_not_ideal_sum(self):
        import array
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'input.mp4';out=root/'output.mp4'
            expr=r'aevalsrc=if(between(mod(t\,4)\,0.70\,0.90)\,0.7*sin(2*PI*1000*t)\,0):s=48000:d=110'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i',expr,'-t','110','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=60)
            info=stream_cutter.probe(str(src),'ffprobe');keeps=[(i*4+.13,i*4+1.47) for i in range(27)]
            args=stream_cutter.parse_args([]);stream_cutter.apply_speed(args,'turbo')
            args.vcodec='libx264';args.out=str(out);args.jobs=2;args.scale_height=0;args.blur_regions=[];args.fast=False;args.map_timeline=True
            res={'src':str(src),'info':info,'streams':stream_cutter.audio_streams(info),'total':110.,'keep':keeps}
            stream_cutter.render_file(res,args,'ffmpeg','ffprobe')
            actual=pipeline.media_duration(out);ideal=sum(b-a for a,b in keeps)
            self.assertGreater(abs(actual-ideal),.5) # reproduced original failure
            signals=[{'start':i*4+.70,'end':i*4+.90,'text':str(i)} for i in range(27)]
            mapped,planned=full_cycle.remap_segments(signals,0,keeps,rendered_timeline=res['render_timeline'])
            self.assertLess(abs(actual-planned),.25)
            raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(out),'-map','0:a:0','-af','aresample=async=1:first_pts=0','-ac','1','-ar','4000','-f','s16le','-']);samples=array.array('h');samples.frombytes(raw)
            for row in mapped:
                expected=row['start'];lo=max(0,int((expected-.15)*4000));hi=min(len(samples),int((expected+.30)*4000))
                first=next((j for j in range(lo,hi) if abs(samples[j])>4000),None)
                self.assertIsNotNone(first, row)
                self.assertLess(abs(first/4000-expected),.15,(row,first/4000))

    def test_pipeline_completes_long_clip_with_many_cuts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'test_input';folder.mkdir();src=folder/'demo.mp4'
            expr=r'aevalsrc=if(between(mod(t\,4)\,0.70\,1.50)\,0.7*sin(2*PI*1000*t)\,0):s=48000:d=110'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i',expr,'-t','110','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=60)
            settings=Settings(root/'config.json');cfg=settings.load();cfg['pipeline']['min_reaction_minutes']=0;cfg['pipeline']['remove_donations']=False
            cfg['pipeline']['silence'].update(enabled=True,min_cut=1.0,max_gap=1.0);settings.save(cfg)
            def speech(src,cache,cfg,progress,duration):
                return ([{'start':i*4+.8,'end':i*4+1.2,'text':f'phrase{i}'} for i in range(27)],duration)
            finder=lambda *a:[{'start':0,'end':108,'title':'Проверка'}]
            service=Service(root,settings,None,pipeline_factory=lambda root,_:pipeline.Pipeline(root,finder,speech=speech))
            service.queue_test_inputs()
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'): service.worker_once()
            job=service.store.list_jobs()[0];self.assertEqual(job['state'],'done',job['detail'])
            meta=next((root/'recordings').rglob('*.pipeline.json'))
            import json
            data=json.loads(meta.read_text(encoding='utf8'))
            self.assertGreater(len(data['render_timeline']),20)
            text=(meta.parent/'Готовые без тишины'/meta.name.replace('.pipeline.json','.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('phrase0',text); self.assertIn('phrase26',text)
            report=next((meta.parent/'Готовые без тишины').glob('* — удалённая тишина.txt')).read_text(encoding='utf-8-sig')
            self.assertIn('Всего удалено:',report); self.assertIn('исходного тестового видео',report)
            self.assertTrue(any('Проверено частей' in m for _,m in service.logs()))

    def test_nonzero_vod_source_offset_without_silence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'input.mp4';(root/'_work').mkdir()
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-f','lavfi','-i','sine=duration=12','-t','12','-c:v','libx264','-c:a','aac','-y',str(src)],check=True,timeout=30)
            with patch.object(stream_cutter,'pick_encoder',return_value='libx264'):
                keeps,duration=pipeline.render_clip(src,root/'clip.mp4',{'start':101.2,'end':106.2},100,{'enabled':False})
            self.assertAlmostEqual(keeps[0][0],1.2);self.assertAlmostEqual(duration,5,delta=.5)
            rows,planned=full_cycle.remap_segments([{'start':102.2,'end':103.2,'text':'word'}],100,keeps)
            self.assertAlmostEqual(rows[0]['start'],1,delta=.01)

class RenderProgressTests(unittest.TestCase):
    def test_running_encoder_reports_progress(self):
        import time
        messages=[]; tracker=stream_cutter.Progress(4,1)
        def fake_exec(cmd,idx,prog,log):
            prog.set(idx,2); time.sleep(10.2); return 0
        with tempfile.TemporaryDirectory() as d, patch.object(stream_cutter,'_exec',side_effect=fake_exec):
            result=stream_cutter.run_jobs('ffmpeg',[(0,4)],lambda *args:['fake'],tracker,d,None,1,messages.append)
        self.assertEqual(len(result),1)
        self.assertTrue(any('50.0%' in message for message in messages),messages)

if __name__=='__main__': unittest.main()
