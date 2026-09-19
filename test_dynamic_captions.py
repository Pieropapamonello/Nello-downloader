import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dynamic_captions import chunks, text_band, make_ass


class DynamicTests(unittest.TestCase):
    def test_chunks_keep_words_and_timing_without_overlap(self):
        text = 'Una frase tradotta con tutte le parole al posto giusto'
        result = list(chunks([(1200, 6800, text)]))
        self.assertEqual(' '.join(r[2] for r in result), text.upper())
        self.assertEqual((result[0][0], result[-1][1]), (1200, 6800))
        self.assertTrue(all(len(r[2].split()) <= 3 for r in result))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(result, result[1:])))

    def test_large_uppercase_band_only(self):
        tsv = 'block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv += '1\t1\t1\t100\t430\t160\t25\t90\tACTORS\n'
        tsv += '2\t1\t1\t5\t5\t60\t10\t95\tLOGO\n'
        tsv += '3\t1\t1\t100\t350\t60\t8\t95\tSMALL\n'
        self.assertEqual(text_band(tsv, 640), [430 / 640])

    def test_ass_is_yellow_bold_uppercase_and_escapes_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / 'italian.srt'
            srt.write_text('1\n00:00:01,000 --> 00:00:03,000\nCiao mondo!\n', encoding='utf-8')
            with patch('dynamic_captions.caption_y', return_value=.65):
                ass = make_ass(srt, 'video.mp4', 3, 360, 640).read_text(encoding='utf-8')
            self.assertIn('&H0000FFFF', ass)
            self.assertIn('CIAO MONDO!', ass)
            self.assertIn('\\pos(180,416)', ass)
            self.assertIn('\\t(0,90,', ass)
