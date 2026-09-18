import json
import unittest
from unittest.mock import AsyncMock, patch

from facebook_post import resolve_page, is_post_url, post_id
from facebook_photo import extract_photo
from social_downloader import SocialMediaDownloader


def page(data, canonical=''):
    return f'<link rel="canonical" href="{canonical}"><script type="application/json">{json.dumps(data)}</script>'


class FacebookPostTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_photo_ignores_recommended_video_and_avatar(self):
        data = [
            {'post_id': '999', 'attachments': [{'media': {'__typename': 'Video', 'id': '888'}}]},
            {'post_id': '123', 'message': {'text': 'Descrizione completa #tag'},
             'attachments': [{'media': {'__typename': 'Photo', 'id': '456'}, 'styles':
                              {'attachment': {'media': {'__typename': 'Photo', 'id': '456',
                                                       'photo_image': {'uri': 'https://x.fbcdn.net/photo.jpg',
                                                                       'width': 512, 'height': 640}}}}}]},
            {'id': '777', 'image': {'uri': 'https://x.fbcdn.net/avatar.jpg', 'width': 1080, 'height': 1080}}]
        source = page(data, 'https://www.facebook.com/name/posts/title/123/')
        target = resolve_page(source, 'https://www.facebook.com/share/abc/')
        self.assertEqual(target['kind'], 'Photo')
        self.assertEqual(target['id'], '456')
        self.assertEqual(target['description'], 'Descrizione completa #tag')
        self.assertEqual(extract_photo(source, target['id'])['image'], 'https://x.fbcdn.net/photo.jpg')

    def test_missing_identity_does_not_pick_first_story(self):
        data = {'post_id': '999', 'attachments': [{'media': {'__typename': 'Video', 'id': '888'}}]}
        self.assertIsNone(resolve_page(page(data), 'https://www.facebook.com/share/abc/'))
        self.assertIsNone(resolve_page(page(data), 'https://www.facebook.com/name/posts/123/'))

    def test_multiple_attachments_are_not_guessed(self):
        data = {'post_id': '123', 'attachments': [{'media': {'__typename': 'Photo', 'id': '1'}},
                                                  {'media': {'__typename': 'Photo', 'id': '2'}}]}
        self.assertIsNone(resolve_page(page(data), 'https://www.facebook.com/name/posts/123/'))

    def test_external_canonical_rejected(self):
        self.assertIsNone(resolve_page(page({}, 'https://notfacebook.com/reel/123'),
                                       'https://www.facebook.com/share/abc'))

    def test_post_routes(self):
        self.assertTrue(is_post_url('https://www.facebook.com/share/14suwq5RADU/'))
        self.assertTrue(is_post_url('https://www.facebook.com/name/posts/123/'))
        self.assertFalse(is_post_url('https://www.facebook.com/reel/123/'))
        self.assertFalse(is_post_url('https://notfacebook.com/share/abc/'))
        self.assertEqual(post_id('https://www.facebook.com/name/posts/title/123/'), '123')

    async def test_unresolved_share_never_uses_generic_video_extractor(self):
        dl = SocialMediaDownloader.__new__(SocialMediaDownloader)
        dl.extract_info = AsyncMock()
        with patch('facebook_post.download_post', AsyncMock(return_value={'success': False})) as fetch:
            result = await dl.download_video('https://www.facebook.com/share/14suwq5RADU/')
        self.assertFalse(result['success'])
        fetch.assert_awaited_once()
        dl.extract_info.assert_not_awaited()
