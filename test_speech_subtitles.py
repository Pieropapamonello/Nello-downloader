import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from speech_subtitles import (english_detection, transcript_cues, readable_cues, SkipSpeech,
                              prepare_spoken_subtitles, speech_windows, respect_pauses, foreign_language)


class SpeechTests(unittest.TestCase):
    def test_foreign_speech_supported_italian_and_uncertain_skipped(self):
        for code in ('en', 'fr', 'es', 'de', 'ja', 'ar'):
            self.assertEqual(foreign_language(f'auto-detected language: {code} (p = 0.99)'), code)
        self.assertIsNone(foreign_language('auto-detected language: it (p = 0.99)'))
        self.assertIsNone(foreign_language('auto-detected language: fr (p = 0.30)'))
        data = {'result': {'language': 'fr'}, 'transcription': [
            {'offsets': {'from': 0, 'to': 1000}, 'text': 'Hello everyone.'}]}
        self.assertEqual(transcript_cues(data, 1, 'fr'), [(0, 1000, 'Hello everyone.')])
    def test_long_pause_is_not_filled_or_merged(self):
        windows = speech_windows('VAD segment 0: start = 0.10, end = 6.39\nVAD segment 1: start = 14.41, end = 18.00')
        data = {'result': {'language': 'en'}, 'transcription': [
            {'offsets': {'from': 5780, 'to': 14450}, 'text': 'country.'},
            {'offsets': {'from': 14450, 'to': 15420}, 'text': 'Think of it.'}]}
        cues = transcript_cues(respect_pauses(data, windows), 42)
        self.assertEqual(cues[0], (5780, 6390, 'country.'))
        self.assertFalse(any(s <= 7000 < e for s, e, _ in cues))
        self.assertEqual(cues[1][0], 14450)

    def test_only_confident_english_is_processed(self):
        self.assertTrue(english_detection('auto-detected language: en (p = 0.999528)'))
        for output in ('auto-detected language: it (p = 0.998)',
                       'auto-detected language: en (p = 0.45)', '', 'English video'):
            self.assertFalse(english_detection(output))

    def test_timestamps_and_silent_segments(self):
        result = {'result': {'language': 'en'}, 'transcription': [
            {'offsets': {'from': 0, 'to': 2000}, 'text': ' Hello there. '},
            {'offsets': {'from': 2000, 'to': 3000}, 'text': '[Music]'},
            {'offsets': {'from': 3000, 'to': 6000}, 'text': 'Welcome back!'}]}
        self.assertEqual(transcript_cues(result, 5), [(0, 2000, 'Hello there.'), (3000, 5000, 'Welcome back!')])
        result['result']['language'] = 'it'
        with self.assertRaises(SkipSpeech):
            transcript_cues(result, 5)

    def test_empty_or_repeated_transcripts_rejected(self):
        with self.assertRaises(SkipSpeech):
            transcript_cues({'result': {'language': 'en'}, 'transcription': []}, 5)
        data = {'result': {'language': 'en'}, 'transcription': [
            {'offsets': {'from': i * 1000, 'to': (i + 1) * 1000}, 'text': 'Thank you.'} for i in range(10)]}
        with self.assertRaises(SkipSpeech):
            transcript_cues(data, 10)

    def test_translation_phrases_reflow_without_losing_text_or_duration(self):
        text = 'Una frase italiana abbastanza lunga per verificare che venga divisa in sottotitoli leggibili senza perdere parole.'
        parts = readable_cues([(1000, 7000, text)])
        self.assertEqual(' '.join(p[2] for p in parts), text)
        self.assertEqual(parts[0][0], 1000)
        self.assertEqual(parts[-1][1], 7000)
        self.assertTrue(all(a[1] == b[0] for a, b in zip(parts, parts[1:])))

    def test_memory_pressure_does_not_start_model(self):
        with patch('speech_subtitles.memory_pressure', return_value=True), patch('speech_subtitles.subprocess.Popen') as popen:
            self.assertIsNone(prepare_spoken_subtitles('source.mp4', '.'))
            popen.assert_not_called()

    def test_timeout_kills_optional_worker_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            source.write_bytes(b'original')
            proc = MagicMock()
            proc.poll.return_value = None
            with patch('speech_subtitles.memory_pressure', return_value=False), \
                 patch('speech_subtitles.subprocess.Popen', return_value=proc), \
                 patch('speech_subtitles.stop_job') as stop:
                self.assertIsNone(prepare_spoken_subtitles(str(source), directory, timeout=0))
                stop.assert_called_once_with(proc)
            self.assertEqual(source.read_bytes(), b'original')
