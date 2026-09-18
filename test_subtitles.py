import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from subtitles import (caption_metadata, tiktok_source_language, parse_captions,
                       translate_cues, build_subtitles, prepare_subtitles)
from wa_media import prepare_video

VTT = 'WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nCiao mondo!\n\n00:00:01.500 --> 00:00:03.000\nQuesti sono i sottotitoli italiani.\n'
TRACK = {'ext': 'vtt', 'data': VTT}


class CaptionTests(unittest.TestCase):
    def test_english_requires_source_language_not_title_or_translation(self):
        base = {'subtitles': {'en': [TRACK], 'it': [TRACK]}, 'duration': 3,
                'title': 'This is an English title'}
        for lang in ('', 'it', 'fr'):
            self.assertEqual(caption_metadata(dict(base, language=lang)), {})
        self.assertEqual(caption_metadata(dict(base, language='en-US'))['language'], 'en')
        info = dict(base, automatic_captions={'en-orig': [TRACK]})
        self.assertEqual(caption_metadata(info)['language'], 'en')
        info['requested_formats'] = [{'acodec': 'aac', 'language': 'it'}]
        self.assertEqual(caption_metadata(info), {})

    def test_tiktok_automatic_translation_is_not_spoken_language(self):
        data = {'video': {'subtitleInfos': [
            {'Source': 'ASR', 'LanguageCodeName': 'ita-IT'},
            {'Source': 'MT', 'LanguageCodeName': 'eng-US'}]}}
        self.assertEqual(tiktok_source_language(data), 'it')
        data['video']['subtitleInfos'][0]['LanguageCodeName'] = 'eng-US'
        self.assertEqual(tiktok_source_language(data), 'en')
        data['video']['subtitleInfos'] = [{'Source': 'MT', 'LanguageCodeName': 'eng-US'}]
        self.assertIsNone(tiktok_source_language(data))

    def test_tiktok_custom_extractor_replaces_default(self):
        import yt_dlp
        from tiktok_captions import TikTokCaptionsIE
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            ydl.add_info_extractor(TikTokCaptionsIE())
            self.assertIsInstance(ydl.get_info_extractor('TikTok'), TikTokCaptionsIE)

    def test_vtt_and_srt_timing_and_text(self):
        self.assertEqual(parse_captions(VTT, 'vtt')[0], (0, 1500, 'Ciao mondo!'))
        srt = '1\n00:00:01,000 --> 00:00:02,000\n<b>Hello</b> &amp; world\nsecond line\n\n'
        self.assertEqual(parse_captions(srt, 'srt'), [(1000, 2000, 'Hello & world second line')])

    def test_json3_and_rolling_caption_deduplication(self):
        data = {'events': [
            {'tStartMs': 0, 'dDurationMs': 1500, 'segs': [{'utf8': 'Hello'}]},
            {'tStartMs': 1000, 'dDurationMs': 1000, 'segs': [{'utf8': 'Hello'}]},
            {'tStartMs': 1900, 'dDurationMs': 1000, 'segs': [{'utf8': 'World'}]}]}
        self.assertEqual(parse_captions(json.dumps(data), 'json3'),
                         [(0, 1900, 'Hello'), (1900, 2900, 'World')])

    def test_translation_preserves_cue_alignment(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.json.return_value = {'responseStatus': 200, 'responseData': {'translatedText': 'Ciao\nMondo'}}
        cues = [(0, 1000, 'Hello'), (1000, 2000, 'World')]
        self.assertEqual(translate_cues(cues, session), [(0, 1000, 'Ciao'), (1000, 2000, 'Mondo')])
        response.json.return_value['responseData']['translatedText'] = 'Ciao mondo'
        with self.assertRaises(ValueError):
            translate_cues(cues, session)
        response.json.return_value['quotaFinished'] = True
        with self.assertRaises(ValueError):
            translate_cues(cues, session)

    def test_native_italian_track_preferred_without_translation_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch('requests.Session') as session:
            path = Path(directory) / 'italian.srt'
            self.assertTrue(build_subtitles({'language': 'en', 'duration': 3,
                                            'tracks': {'it': [TRACK], 'en': [TRACK]}}, path))
            self.assertIn('Ciao mondo!', path.read_text(encoding='utf-8'))
            session.return_value.__enter__.return_value.get.assert_not_called()

    def test_free_provider_fallback_preserves_cues(self):
        session = MagicMock()
        failed = MagicMock()
        failed.__enter__.return_value.raise_for_status.side_effect = RuntimeError('provider unavailable')
        success = MagicMock()
        success.__enter__.return_value.json.return_value = [[['Ciao\n', 'Hello'], ['Mondo', 'World']]]
        session.get.side_effect = [failed, success]
        self.assertEqual(translate_cues([(0, 1000, 'Hello'), (1000, 2000, 'World')], session),
                         [(0, 1000, 'Ciao'), (1000, 2000, 'Mondo')])
        self.assertEqual(session.get.call_count, 2)

    def test_italian_unknown_and_long_videos_not_processed(self):
        with tempfile.TemporaryDirectory() as directory, patch('requests.Session') as session:
            for lang, duration in (('it', 3), ('', 3), ('en', 181), ('en', 0)):
                self.assertFalse(build_subtitles({'language': lang, 'duration': duration,
                                                 'tracks': {'it': [TRACK]}}, Path(directory) / 'italian.srt'))
            session.assert_not_called()

    def test_memory_pressure_skips_optional_worker(self):
        with patch('subtitles.memory_pressure', return_value=True), patch('subtitles.subprocess.Popen') as popen:
            self.assertIsNone(prepare_subtitles({'language': 'en'}, '.'))
            popen.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_real_burn_keeps_audio_and_changes_video_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=3',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
            sub = prepare_subtitles({'language': 'en', 'duration': 3, 'tracks': {'it': [TRACK]}}, directory)
            self.assertIsNotNone(sub)
            output = prepare_video(str(source), subtitle_path=sub)
            self.assertTrue(source.exists())
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', output]))
            self.assertEqual({s['codec_type'] for s in probe['streams']}, {'audio', 'video'})
            pixels = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.5', '-i', output,
                                             '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
            self.assertGreater(max(pixels), 200)  # White subtitle glyphs over black frame.
            subprocess.run(['ffmpeg', '-v', 'error', '-i', output, '-f', 'null', '-'], check=True)


if __name__ == '__main__':
    unittest.main()
