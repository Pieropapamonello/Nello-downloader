import json
import unittest
from unittest.mock import AsyncMock, patch
from facebook_photo import extract_photo, photo_id
from social_downloader import SocialMediaDownloader


def page(data):
    return '<script type="application/json">' + json.dumps(data) + '</script>'


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_id_and_description_across_fragments(self):
        data = [{'id': '111', 'image': {'uri': 'https://x.fbcdn.net/avatar.jpg', 'width': 2000, 'height': 2000}},
                {'id': '123', 'image': {'uri': 'https://x.fbcdn.net/photo.jpg', 'width': 1080, 'height': 1350}},
                {'id': '123', 'message': {'text': 'Descrizione del post'}}]
        result = extract_photo(page(data), '123')
        self.assertEqual(result['image'], 'https://x.fbcdn.net/photo.jpg')
        self.assertEqual(result['description'], 'Descrizione del post')

    def test_no_avatar_og_image_or_unrelated_picture(self):
        source = '<meta property="og:image" content="https://x.fbcdn.net/avatar.jpg">'
        source += page({'id': '999', 'image': {'uri': 'https://x.fbcdn.net/unrelated.jpg', 'width': 1080, 'height': 1350}})
        self.assertIsNone(extract_photo(source, '123'))
        self.assertIsNone(extract_photo(page({'id': '123', 'owner': {'profile_picture': {'uri': 'https://x.fbcdn.net/avatar.jpg'}}}), '123'))

    def test_url_validation(self):
        self.assertEqual(photo_id('https://www.facebook.com/photo/?fbid=123&set=a.12'), '123')
        self.assertEqual(photo_id('https://www.facebook.com/photo.php?fbid=123'), '123')
        self.assertIsNone(photo_id('https://evil.com/photo/?fbid=123'))

    async def test_failure_never_falls_back_to_generic_scraper(self):
        dl = SocialMediaDownloader.__new__(SocialMediaDownloader)
        dl.download_with_cobalt = AsyncMock()
        with patch('facebook_photo.download_photo', AsyncMock(return_value={'success': False})) as fetch:
            result = await dl.download_video('https://www.facebook.com/photo/?fbid=123')
        self.assertFalse(result['success'])
        fetch.assert_awaited_once()
        dl.download_with_cobalt.assert_not_awaited()
