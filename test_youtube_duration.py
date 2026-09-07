import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from youtube_duration import video_id, parse_duration, youtube_duration


class DurationTests(unittest.TestCase):
    def test_duration_uses_configured_youtube_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            cookiefile = Path(directory) / 'cookies.txt'
            cookiefile.write_text('# Netscape HTTP Cookie File\n'
                                  '.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest-only\n')
            page = 'var ytInitialPlayerResponse = ' + json.dumps({
                'videoDetails': {'videoId': 'Pq2ArYdUzQo', 'lengthSeconds': '72'}})
            with patch('youtube_duration.requests.get') as get:
                get.return_value.__enter__.return_value.iter_content.return_value = [page.encode()]
                self.assertEqual(youtube_duration('https://youtu.be/Pq2ArYdUzQo', str(cookiefile)), 72)
                cookies = list(get.call_args.kwargs['cookies'])
                self.assertEqual([(c.domain, c.name) for c in cookies], [('.youtube.com', 'SID')])

    def test_url_variants(self):
        for url in ('https://youtu.be/Pq2ArYdUzQo',
                    'https://www.youtube.com/watch?v=Pq2ArYdUzQo&t=3',
                    'https://www.youtube.com/shorts/Pq2ArYdUzQo'):
            self.assertEqual(video_id(url), 'Pq2ArYdUzQo')
        self.assertIsNone(video_id('https://example.org/watch?v=Pq2ArYdUzQo'))

    def test_reads_only_target_video(self):
        def player(ident, duration):
            return 'var ytInitialPlayerResponse = ' + json.dumps({
                'videoDetails': {'videoId': ident, 'lengthSeconds': duration}}) + ';'
        page = player('OtherVideo1', '20') + player('Pq2ArYdUzQo', '1200')
        self.assertEqual(parse_duration(page, 'Pq2ArYdUzQo'), 1200)
        self.assertIsNone(parse_duration(page, 'NoMatching1'))

    def test_consent_page_and_invalid_duration(self):
        self.assertIsNone(parse_duration('<html>Consent required</html>', 'Pq2ArYdUzQo'))
        for duration in ('NaN', 'Infinity', '-1', '0', 'unknown'):
            page = 'ytInitialPlayerResponse = ' + json.dumps({
                'videoDetails': {'videoId': 'Pq2ArYdUzQo', 'lengthSeconds': duration}})
            self.assertIsNone(parse_duration(page, 'Pq2ArYdUzQo'))


if __name__ == '__main__':
    unittest.main()
