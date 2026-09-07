import asyncio
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer

from downloader_service import build_app
from remote_downloader import remote_download

TOKEN = 'test-only-' * 4


class FakeDownloader:
    calls = 0
    def __init__(self):
        self.base_opts = {'format': 'best'}

    async def download_video(self, url):
        type(self).calls += 1
        path = Path(self.temp_dir) / 'video.mp4'
        path.write_bytes(b'test video payload')
        return {'success': True, 'type': 'video', 'file_path': str(path), 'title': 'Test'}


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeDownloader.calls = 0
        self.client = TestClient(TestServer(build_app(TOKEN, FakeDownloader)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + TOKEN}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health_public_jobs_private(self):
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        self.assertEqual((await self.client.post('/jobs', json={})).status, 401)
        bad = {'id': str(uuid.uuid4()), 'url': 'http://127.0.0.1/private'}
        self.assertEqual((await self.client.post('/jobs', json=bad, headers=self.headers)).status, 400)

    async def test_submit_is_idempotent_and_files_require_auth(self):
        ident = str(uuid.uuid4())
        body = {'id': ident, 'url': 'https://youtube.com/shorts/Pq2ArYdUzQo'}
        for _ in range(2):
            self.assertEqual((await self.client.post('/jobs', json=body, headers=self.headers)).status, 202)
        for _ in range(50):
            result = await (await self.client.get('/jobs/' + ident, headers=self.headers)).json()
            if result['state'] == 'done':
                break
            await asyncio.sleep(.01)
        self.assertEqual(FakeDownloader.calls, 1)
        self.assertNotIn('file_path', result['result'])
        self.assertEqual((await self.client.get(f'/jobs/{ident}/files/0')).status, 401)
        self.assertEqual((await self.client.get(f'/jobs/{ident}/files/-1', headers=self.headers)).status, 404)
        await self.client.delete('/jobs/' + ident, headers=self.headers)
        self.assertEqual((await self.client.get('/jobs/' + ident, headers=self.headers)).status, 404)

    async def test_client_transfers_file_and_deletes_remote_job(self):
        with patch.dict(os.environ, {'DOWNLOADER_URL': str(self.client.make_url('')).rstrip('/'),
                                     'DOWNLOADER_TOKEN': TOKEN}):
            result = await remote_download('https://youtu.be/Pq2ArYdUzQo')
        self.assertTrue(result['success'], result)
        path = Path(result['file_path'])
        try:
            self.assertEqual(path.read_bytes(), b'test video payload')
        finally:
            path.unlink()


if __name__ == '__main__':
    unittest.main()
