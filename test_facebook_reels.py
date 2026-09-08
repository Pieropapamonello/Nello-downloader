import unittest
from unittest.mock import AsyncMock, patch
from social_downloader import SocialMediaDownloader
from smd_facebook import FacebookMixin, is_facebook_video_url, facebook_video_id

class FacebookReelTests(unittest.IsolatedAsyncioTestCase):
    def test_video_routes_and_photo_routes(self):
        for url in ['https://www.facebook.com/reel/586205726927663',
                    'https://www.facebook.com/watch/?v=123',
                    'https://www.facebook.com/user/videos/123/',
                    'https://www.facebook.com/share/r/abc/', 'https://fb.watch/abc/']:
            self.assertTrue(is_facebook_video_url(url), url)
        self.assertFalse(is_facebook_video_url('https://www.facebook.com/photo/?fbid=123'))
        self.assertEqual(facebook_video_id('https://www.facebook.com/reel/123/'), '123')

    async def test_reel_never_scrapes_preview_or_recommendations(self):
        with patch('smd_facebook.requests.get') as request:
            self.assertIsNone(await FacebookMixin()._facebook_fallback(
                'https://www.facebook.com/reel/123'))
            request.assert_not_called()

    async def test_failed_reel_does_not_use_photo_fallbacks(self):
        dl = SocialMediaDownloader.__new__(SocialMediaDownloader)
        dl.max_retries = 1
        dl.retry_delay = 0
        dl.extract_info = AsyncMock(return_value=None)
        dl.download_with_cobalt = AsyncMock()
        dl._facebook_fallback = AsyncMock()
        result = await dl.download_video('https://www.facebook.com/reel/123')
        self.assertFalse(result['success'])
        dl.download_with_cobalt.assert_not_awaited()
        dl._facebook_fallback.assert_not_awaited()

    async def test_unrelated_video_id_is_rejected(self):
        dl = SocialMediaDownloader.__new__(SocialMediaDownloader)
        dl.max_retries = 1
        dl.retry_delay = 0
        dl.extract_info = AsyncMock(return_value={'id': '999', 'title': 'Suggested video'})
        dl.download_with_ytdlp = AsyncMock()
        result = await dl.download_video('https://www.facebook.com/reel/123')
        self.assertFalse(result['success'])
        dl.download_with_ytdlp.assert_not_awaited()
