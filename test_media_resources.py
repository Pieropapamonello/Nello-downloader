import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
import json
import subprocess

from media_resources import limited_media
from social_downloader import SocialMediaDownloader


class ResourceTests(unittest.TestCase):
    def test_threads_share_one_slot_and_release_after_failure(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        @limited_media
        def work(fail):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.02)
                if fail:
                    raise ValueError('expected')
            finally:
                with guard:
                    active -= 1

        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(work, fail) for fail in (True, False, False)]
            with self.assertRaises(ValueError):
                futures[0].result()
            for future in futures[1:]:
                future.result()
        self.assertEqual(peak, 1)


class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    def downloader(self):
        dl = SocialMediaDownloader.__new__(SocialMediaDownloader)
        dl.max_retries = 1
        dl.retry_delay = 0
        dl.youtube_max_duration = 180
        dl.get_ydl_opts = MagicMock(return_value={})
        return dl

    async def test_reuses_extracted_metadata(self):
        dl = self.downloader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'test')
            with patch('social_downloader.yt_dlp.YoutubeDL') as factory:
                ydl = factory.return_value.__enter__.return_value
                ydl.prepare_filename.return_value = str(path)
                info = {'id': 'example', 'duration': 72}
                self.assertEqual(await dl.download_with_ytdlp('https://facebook.com/reel/123', info=info), str(path))
                ydl.process_ie_result.assert_called_once_with(info, download=True)
                ydl.extract_info.assert_not_called()
                ydl.download.assert_not_called()

    async def test_extraction_failure_is_not_reported_as_long_video(self):
        dl = self.downloader()
        async def fail(*args):
            raise RuntimeError('solver failed')
        dl.extract_info = fail
        with patch('social_downloader.youtube_duration', return_value=72):
            result = await dl.download_video('https://www.youtube.com/shorts/example')
        self.assertFalse(result['success'])
        self.assertIn('error', result)
        self.assertNotIn('skip_long', result)

    async def test_long_video_still_skipped_before_download(self):
        dl = self.downloader()
        dl.extract_info = AsyncMock()
        with patch('social_downloader.youtube_duration', return_value=181):
            result = await dl.download_video('https://www.youtube.com/shorts/example')
        self.assertTrue(result['skip_long'])
        dl.extract_info.assert_not_awaited()

    async def test_unknown_duration_does_not_start_extraction(self):
        dl = self.downloader()
        dl.extract_info = AsyncMock()
        with patch('social_downloader.youtube_duration', return_value=None):
            result = await dl.download_video('https://www.youtube.com/shorts/example')
        self.assertIn('verificare la durata', result['error'])
        dl.extract_info.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
