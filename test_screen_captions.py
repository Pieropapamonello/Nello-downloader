import unittest
from unittest.mock import patch
from screen_captions import verified_band, tsv_lines, text_language


class ScreenTests(unittest.TestCase):
    def test_real_language_detection_and_short_uncertain_rejection(self):
        self.assertEqual(text_language('Bonjour tout le monde. Voici une nouvelle histoire pour vous.'), 'fr')
        self.assertEqual(text_language('Questi sono i sottotitoli italiani del video di oggi.'), 'it')
        self.assertIsNone(text_language('Logo'))

    def test_static_logo_never_authorizes_mask(self):
        samples = [(i, 20, 20, 100, 40, 'BRAND NAME') for i in range(3)]
        self.assertIsNone(verified_band(samples, 640))

    def test_bottom_captions_and_already_italian(self):
        samples = [(0, 5, 610, 460, 634, 'Bonjour tout le monde'),
                   (1, 5, 610, 460, 634, 'Voici une nouvelle histoire')]
        with patch('screen_captions.text_language', return_value='fr'):
            self.assertEqual(verified_band(samples, 640), ('fr', 602, 640))
        with patch('screen_captions.text_language', return_value='it'):
            self.assertEqual(verified_band(samples, 640)[0], 'it')

    def test_unicode_ocr_keeps_accents_and_ignores_low_confidence(self):
        tsv = 'left\ttop\twidth\theight\tconf\ttext\n0\t8\t50\t20\t95\tFran\u00e7ais\n60\t8\t30\t20\t10\tnoise\n'
        self.assertEqual(tsv_lines(tsv, 128)[0][-1], 'Fran\u00e7ais')
