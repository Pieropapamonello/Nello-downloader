import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from dynamic_captions import chunks, make_ass
from burned_captions import changing_band, matches_speech, ocr


class DynamicTests(unittest.TestCase):
    def test_ocr_limits_threads_and_rejects_uncertain_words(self):
        from PIL import Image
        from types import SimpleNamespace
        tsv = 'top\tconf\ttext\n16\t95\tWE\n16\t91\tPLAYED\n112\t20\tNOISE\n'
        with tempfile.TemporaryDirectory() as directory:
            with patch('burned_captions.subprocess.run', return_value=SimpleNamespace(stdout=tsv)) as run:
                result = ocr([Image.new('L', (100, 30)), Image.new('L', (100, 30))],
                             Path(directory) / 'ocr.png', timeout=12, confidence=55)
        self.assertEqual(result, ['WE PLAYED', ''])
        self.assertEqual(run.call_args.kwargs['env']['OMP_THREAD_LIMIT'], '1')
        self.assertEqual(run.call_args.kwargs['timeout'], 12)

    def test_chunks_keep_words_and_timing_without_overlap(self):
        text = 'Una frase tradotta con tutte le parole al posto giusto'
        result = list(chunks([(1200, 6800, text)]))
        self.assertEqual(' '.join(r[2] for r in result), text.upper())
        self.assertEqual((result[0][0], result[-1][1]), (1200, 6800))
        self.assertTrue(all(len(r[2].split()) <= 3 for r in result))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(result, result[1:])))

    def test_static_signs_never_authorize_a_mask(self):
        self.assertIsNone(changing_band([(0, 50, 70, 'NEVADA'), (1, 50, 70, 'NEVADA')]))
        self.assertFalse(matches_speech('NO TAX ON TIPS', {'millions', 'country', 'people'}))
        self.assertEqual(changing_band([(0, 430, 460, 'WE PLAYED'), (1, 432, 461, 'PROLOGUE')]), (430, 461))

    def test_ass_is_yellow_bold_uppercase_and_escapes_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / 'italian.srt'
            srt.write_text('1\n00:00:01,000 --> 00:00:03,000\nCiao mondo!\n', encoding='utf-8')
            ass = make_ass(srt, 'video.mp4', 3, 640, 360).read_text(encoding='utf-8')
            self.assertIn('&H0000FFFF', ass)
            self.assertIn('CIAO MONDO!', ass)
            self.assertIn('\\pos(360,360)', ass)
            self.assertIn('Luckiest Guy,42', ass)
            self.assertNotIn('Dialogue: 0', ass)

    def test_screen_phrase_not_split_or_borrowed_and_english_is_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / 'italian.srt'
            srt.write_text('1\n00:00:08,000 --> 00:00:08,750\nAbbiamo giocato\n\n2\n00:00:08,750 --> 00:00:09,500\nIl prologo\n', encoding='utf-8')
            srt.with_name('caption_layout.json').write_text(json.dumps({'source': 'burned', 'box': [0, .67, 1, .72]}))
            ass = make_ass(srt, 'video.mp4', 10, 360, 640).read_text(encoding='utf-8')
            self.assertIn('ABBIAMO GIOCATO\n', ass)
            self.assertNotIn('ABBIAMO GIOCATO IL', ass)
            self.assertIn('Dialogue: 0,0:00:00.00,0:00:10.00,Mask', ass)
            self.assertIn('\\alpha&H00&', ass)
