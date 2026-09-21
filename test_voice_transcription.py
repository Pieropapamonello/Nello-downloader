import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from downloader_service import build_app
from voice_transcription import (italian_detection, transcript_text, run_voice_job, transcribe,
                                 format_transcript, audio_chunks)


class VoiceTests(unittest.TestCase):
    def test_proofreading_keeps_facts_and_adds_readable_paragraphs(self):
        text = "  ciao ,perchè non vieni?qual'è il problema? Un pò di tempo. "
        self.assertEqual(format_transcript(text), "Ciao, perché non vieni? Qual è il problema? Un po’ di tempo.")
        facts = "Alle 9.30 costa 19,90 euro, non 90 euro. Il sito è https://esempio.it/a e si chiama Dott. Rossi."
        self.assertEqual(format_transcript(facts), facts)
        long = 'Questa è una frase completa con informazioni da conservare. ' * 12
        formatted = format_transcript(long)
        self.assertIn('\n\n', formatted)
        self.assertEqual(formatted.replace('\n\n', ' '), long.strip())
        self.assertEqual(format_transcript(''), '')

    def test_chunk_boundaries_keep_every_sample_and_prefer_a_pause(self):
        import io
        import wave
        frames = b'\x10\x27' * (25 * 16000) + b'\x00\x00' * 16000 + b'\x20\x27' * (40 * 16000)
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(frames)
        data.seek(0)
        with wave.open(data, 'rb') as wav:
            chunks = list(audio_chunks(wav))
        self.assertEqual(b''.join(chunks), frames)
        self.assertTrue(all(len(chunk) <= 30 * 16000 * 2 for chunk in chunks))
        self.assertAlmostEqual(len(chunks[0]) / 32000, 25.5, places=1)

    def test_only_confident_italian(self):
        self.assertTrue(italian_detection('auto-detected language: it (p = 0.96)'))
        for text in ('auto-detected language: en (p = 0.99)',
                     'auto-detected language: es (p = 0.99)',
                     'auto-detected language: it (p = 0.50)', ''):
            self.assertFalse(italian_detection(text))
        self.assertTrue(italian_detection('auto-detected language: it (p = 0.70)', seconds=3))
        self.assertFalse(italian_detection('auto-detected language: en (p = 0.99)', seconds=3))
        self.assertTrue(italian_detection('auto-detected language: it (p = 0.45)', seconds=5))
        self.assertFalse(italian_detection('auto-detected language: it (p = 0.30)', seconds=3))

    def test_silence_and_non_italian_never_produce_text(self):
        data = {'result': {'language': 'it'}, 'transcription': [
            {'offsets': {'from': 0, 'to': 1000}, 'text': ' Ciao ragazzi. '},
            {'offsets': {'from': 3000, 'to': 4000}, 'text': 'invented silence'}]}
        self.assertEqual(transcript_text(data, [(0, 1000)]), 'Ciao ragazzi.')
        self.assertEqual(transcript_text(data, []), '')
        data['result']['language'] = 'en'
        self.assertEqual(transcript_text(data, [(0, 1000)]), '')

    def test_memory_pressure_does_not_start_process(self):
        with patch('voice_transcription.memory_pressure', return_value=True), patch('voice_transcription.subprocess.Popen') as proc:
            self.assertFalse(run_voice_job('unused')['success'])
            proc.assert_not_called()

    def test_language_detection_checks_later_audio_not_just_first_window(self):
        import wave
        from types import SimpleNamespace
        samples = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.audio'
            def fake_run(command, **kwargs):
                if command[0] == 'ffmpeg':
                    with wave.open(command[-1], 'wb') as out:
                        out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                        out.writeframes(b'\x10\x27' * 30 * 16000 + b'\x20\x27' * 5 * 16000)
                    return SimpleNamespace()
                if '-dl' not in command:
                    import json
                    with wave.open(command[command.index('-f') + 1], 'rb') as wav:
                        self.assertLessEqual(wav.getnframes(), 30 * 16000)
                    Path(command[command.index('-of') + 1] + '.json').write_text(json.dumps({
                        'result': {'language': 'it'}, 'transcription': [
                            {'offsets': {'from': 0, 'to': 1000}, 'text': 'Ciao ragazzi.'}]}))
                    return SimpleNamespace(stderr='VAD segment 0: start = 0.00, end = 1.00')
                with wave.open(command[command.index('-f') + 1], 'rb') as wav:
                    samples.append(wav.readframes(wav.getnframes())[-2:])
                lang = 'it' if len(samples) == 1 else 'en'
                return SimpleNamespace(stderr=f'auto-detected language: {lang} (p = 0.99)')
            with patch('voice_transcription.Path.is_file', return_value=True), \
                 patch('voice_transcription.subprocess.check_output', return_value=b'{"format":{"duration":"35"},"streams":[{"codec_type":"audio"}]}'), \
                 patch('voice_transcription.subprocess.run', side_effect=fake_run):
                self.assertEqual(transcribe(str(source))['skipped'], 'not_italian')
            self.assertEqual(samples, [b'\x10\x27', b'\x20\x27'])


class VoiceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_upload_serial_job_and_audio_cleanup(self):
        token = 'test-only-' * 4
        headers = {'Authorization': 'Bearer ' + token}
        paths = []
        def fake(source):
            paths.append(Path(source))
            self.assertEqual(Path(source).read_bytes(), b'voice bytes')
            return {'success': True, 'language': 'it', 'text': 'Ciao ragazzi.'}
        async with TestClient(TestServer(build_app(token))) as client:
            ident = str(uuid.uuid4())
            self.assertEqual((await client.post('/voice-jobs/' + ident, data=b'voice bytes')).status, 401)
            self.assertEqual((await client.post('/subtitle-jobs/' + ident, data=b'video bytes')).status, 401)
            self.assertEqual((await client.post('/subtitle-jobs/' + ident, data=b'not an MP4', headers=headers)).status, 400)
            with patch('downloader_service.run_voice_job', side_effect=fake):
                self.assertEqual((await client.post('/voice-jobs/' + ident, data=b'voice bytes', headers=headers)).status, 202)
                for _ in range(100):
                    result = await (await client.get('/jobs/' + ident, headers=headers)).json()
                    if result['state'] == 'done':
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(result['result']['text'], 'Ciao ragazzi.')
                self.assertFalse(paths[0].exists())
                self.assertEqual((await client.post('/voice-jobs/' + ident, data=b'voice bytes', headers=headers)).status, 202)
                self.assertEqual(len(paths), 1)
            await client.delete('/jobs/' + ident, headers=headers)
            self.assertEqual((await client.get('/jobs/' + ident, headers=headers)).status, 404)

    async def test_empty_and_oversized_upload_rejected(self):
        token = 'test-only-' * 4
        async with TestClient(TestServer(build_app(token))) as client:
            with patch('downloader_service.MAX_VOICE_BYTES', 4):
                for data, expected in ((b'', 400), (b'12345', 413)):
                    response = await client.post('/voice-jobs/' + str(uuid.uuid4()), data=data,
                                                 headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status, expected)


if __name__ == '__main__':
    unittest.main()
