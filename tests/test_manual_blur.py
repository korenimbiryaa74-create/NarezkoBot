import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import manual_blur
import banner_blur
import pipeline

class ManualBlurTests(unittest.TestCase):
    def test_profiles_are_isolated_case_insensitive_and_explicit_none(self):
        with tempfile.TemporaryDirectory() as d:
            manual_blur.save_profile(d,'NAME',[[10,20,350,450],[600,100,900,300]])
            manual_blur.save_profile(d,'other',[])
            self.assertEqual(manual_blur.load_profile(d,'name')['boxes'],[[10,20,350,450],[600,100,900,300]])
            self.assertEqual(manual_blur.load_profile(d,'OTHER')['mode'],'none')
            with self.assertRaises(banner_blur.BannerSetupError):manual_blur.load_profile(d,'missing')
            with self.assertRaises(ValueError):manual_blur.save_profile(d,'../a',[])
    def test_corrupt_or_invalid_data_never_interpreted_as_no_blur(self):
        with tempfile.TemporaryDirectory() as d:
            path=manual_blur.profile_path(d,'test');path.parent.mkdir(parents=True);path.write_text('{')
            with self.assertRaises(banner_blur.BannerSetupError):manual_blur.load_profile(d,'test')
            self.assertEqual(path.read_text(),'{')
            for box in ([[True,0,2,2]],[[0,0,1001,1000]],[[0,0,1,1]],[[10,0,9,10]]):
                with self.assertRaises(banner_blur.BannerSetupError):manual_blur.save_profile(d,'test',box)
    def test_missing_profile_or_test_nick_fails_before_processing(self):
        with tempfile.TemporaryDirectory() as d:
            worker=pipeline.Pipeline(d,lambda *a:self.fail('finder used'),speech=lambda *a:self.fail('speech used'))
            cfg={'banner_blur':{'enabled':True}}
            with self.assertRaisesRegex(banner_blur.BannerSetupError,'укажите ник'):worker.run({'vod_id':'test-'+'a'*32,'streamer':'test','local_name':'x.mp4'},cfg)
            with self.assertRaisesRegex(banner_blur.BannerSetupError,'не настроен'):worker.run({'vod_id':'123','streamer':'someone'},cfg)
    def test_display_box_scales_reversed_drag_and_clamps(self):
        self.assertEqual(manual_blur.display_box(500,200,0,0,1000,400),[0,0,500,500])
        self.assertEqual(manual_blur.display_box(-20,50,1500,500,1000,400),[0,125,1000,1000])
        with self.assertRaises(ValueError):manual_blur.display_box(0,0,1,1,0,1)

    def test_no_blur_mode_is_distinct_from_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            manual_blur.save_profile(d,'test',[])
            self.assertEqual(manual_blur.load_profile(d,'test'),{'version':1,'mode':'none','boxes':[]})
            with self.assertRaises(banner_blur.BannerSetupError):manual_blur.load_profile(d,'unset')

    def test_public_latest_vod_needs_only_login_and_uses_newest_archive(self):
        session=Mock();response=Mock();session.post.return_value=response
        response.json.return_value=[{'data':{'user':{'videos':{'edges':[
            {'node':{'id':'123','lengthSeconds':7200}},
            {'node':{'id':'122','lengthSeconds':1800}}]}}}}]
        with patch('requests.Session',return_value=session):
            self.assertEqual(manual_blur.latest_public_vod('NAME','socks5://localhost:1234'),('123',7200))
        args,kwargs=session.post.call_args
        self.assertEqual(args[0],'https://gql.twitch.tv/gql')
        self.assertEqual(kwargs['json'][0]['variables']['channelOwnerLogin'],'name')
        self.assertEqual(kwargs['json'][0]['variables']['broadcastType'],'ARCHIVE')
        self.assertEqual(kwargs['json'][0]['variables']['videoSort'],'TIME')
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies['https'],'socks5://localhost:1234')
        session.close.assert_called_once()

    def test_public_latest_vod_errors_are_distinct_and_do_not_leak_proxy(self):
        session=Mock();response=Mock();session.post.return_value=response
        examples=[([{'data':{'user':None}}],'не найден'),
                  ([{'data':{'user':{'videos':{'edges':[]}}}}],'нет доступной'),
                  ([{'errors':[{'message':'bad query'}]}],'не вернул'),
                  ([{'data':{'user':{'videos':{'edges':[{'node':{'id':'12','lengthSeconds':0}}]}}}}],'некорректные'),
                  ([{'data':{'user':{'videos':{'edges':[{'node':{'id':'abc','lengthSeconds':3}}]}}}}],'некорректные')]
        with patch('requests.Session',return_value=session):
            for payload,message in examples:
                response.json.return_value=payload
                with self.assertRaisesRegex(banner_blur.BannerSetupError,message):manual_blur.latest_public_vod('name')
            import requests
            session.post.side_effect=requests.ConnectionError('password-in-proxy')
            with self.assertRaises(banner_blur.BannerSetupError) as err:manual_blur.latest_public_vod('name')
            self.assertNotIn('password-in-proxy',str(err.exception))
        self.assertEqual(session.close.call_count,len(examples)+1)

    def test_latest_frame_uses_short_excerpt_without_twitch_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            cli=Path(d)/'cli.exe';cli.touch()
            def run(cmd,**kwargs):
                self.assertEqual(cmd[cmd.index('--id')+1],'123')
                self.assertEqual(cmd[cmd.index('-b')+1],'00:59:58')
                self.assertEqual(cmd[cmd.index('-e')+1],'01:00:06')
                self.assertEqual(kwargs['timeout'],180)
                Path(cmd[cmd.index('-o')+1]).touch();return Mock(returncode=0)
            with patch('manual_blur.latest_public_vod',return_value=('123',7200)) as lookup,patch('manual_blur.subprocess.run',side_effect=run),patch('manual_blur.full_frame_at',return_value='frame'):
                self.assertEqual(manual_blur.latest_frame('Name','',str(cli),'ffmpeg'),('frame','123',3598))
            lookup.assert_called_once_with('name','')

if __name__=='__main__':unittest.main()
