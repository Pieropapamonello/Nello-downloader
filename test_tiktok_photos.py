import json
import unittest
from unittest.mock import Mock, patch
from smd_tiktok import TikTokMixin

class TikTokPhotoTests(unittest.IsolatedAsyncioTestCase):
    def downloader(self):
        dl = TikTokMixin()
        dl.debug = False
        dl.tiktok_cookies = 'unused'
        dl.proxy_dict = None
        dl._load_netscape_cookies = lambda path: {}
        return dl

    def test_video_cover_logo_and_avatar_are_not_slides(self):
        page = '<meta property="og:image" content="https://tiktokcdn.com/cover.jpg">'
        data = {'itemInfo': {'itemStruct': {'id': '123', 'video': {
            'cover': 'https://tiktokcdn.com/cover.jpg'},
            'author': {'avatar': 'https://tiktokcdn.com/avatar.jpg'}}}}
        page += '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data) + '</script>'
        self.assertEqual(self.downloader()._extract_tiktok_photo_urls_from_html(page), [])

    def test_real_photo_slides_remain_supported(self):
        data = {'itemInfo': {'itemStruct': {'id': '123', 'imagePost': {'images': [
            {'imageURL': {'urlList': ['https://tiktokcdn.com/slide.jpg']}}]}}}}
        page = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data) + '</script>'
        self.assertEqual(self.downloader()._extract_tiktok_photo_urls_from_html(page),
                         ['https://tiktokcdn.com/slide.jpg'])

    async def test_short_video_link_never_downloads_page_images(self):
        response = Mock(url='https://www.tiktok.com/@user/video/123', text='cover and logo', status_code=200)
        with patch('smd_tiktok.requests.get', return_value=response) as get:
            self.assertEqual(await self.downloader()._tiktok_photo_fallback('https://vm.tiktok.com/example/'), [])
            self.assertEqual(get.call_count, 1)
