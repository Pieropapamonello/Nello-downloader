import logging
import asyncio
import uuid
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from cookie_health import (inspect_content, validate_upload, AuthDiagnostics, install_live, read_content)
from downloader_service import build_app


def cookie(expiry=4102444800, domain='.instagram.com', name='sessionid'):
    return f'# Netscape HTTP Cookie File\n#HttpOnly_{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\tSECRET_VALUE\n'


class HealthTests(unittest.TestCase):
    def test_expiry_and_session_cookie(self):
        self.assertEqual(inspect_content(cookie(1), 'instagram')['state'], 'expired')
        self.assertEqual(inspect_content(cookie(0), 'instagram')['state'], 'present')
        self.assertEqual(inspect_content(cookie(), 'instagram')['state'], 'present')
        self.assertEqual(inspect_content(cookie(name='csrftoken'), 'instagram')['state'], 'missing_session')
        self.assertNotIn('SECRET_VALUE', str(inspect_content(cookie(), 'instagram')))

    def test_validation_no_wrong_domain_expired_or_missing_auth(self):
        for content in (cookie(1), cookie(domain='.evil.com'), cookie(name='csrftoken'), '{}'):
            with self.assertRaises(ValueError):
                validate_upload(content, 'instagram')
        self.assertIn('#HttpOnly_', validate_upload(cookie(), 'instagram'))

    def test_diagnostics_not_generic_failures(self):
        diagnostics = AuthDiagnostics()
        for message in ('Failed to parse JSON', 'HTTP 403 Forbidden', 'memory limit reached', 'timed out'):
            diagnostics.emit(logging.LogRecord('test', 40, '', 0, message, (), None))
        self.assertIsNone(diagnostics.issue)
        diagnostics.emit(logging.LogRecord('test', 40, '', 0, 'redirected to the login page', (), None))
        self.assertEqual(diagnostics.issue, 'login_required')

    def test_override_survives_new_reader(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'COOKIE_UPDATE_DIR': directory}):
            install_live('instagram', cookie())
            self.assertEqual(read_content('instagram'), cookie())


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'COOKIE_UPDATE_DIR': self.tmp.name,
                                          'COOKIE_RENDER_API_KEY': 'fake', 'RENDER_SERVICE_ID': 'srv-test'})
        self.env.start()
        self.client = TestClient(TestServer(build_app('t' * 32)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + 't' * 32}

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    async def test_auth_and_invalid_upload(self):
        self.assertEqual((await self.client.get('/admin/cookies')).status, 401)
        self.assertEqual((await self.client.put('/admin/cookies/instagram', json={})).status, 401)
        r = await self.client.put('/admin/cookies/instagram', headers=self.headers, json={'content': cookie(1)})
        self.assertEqual(r.status, 400)
        self.assertFalse((Path(self.tmp.name) / 'instagram_cookies.txt').exists())

    async def test_persistence_failure_does_not_replace_live_cookie(self):
        response = MagicMock(status=403)
        session = MagicMock()
        session.put.return_value.__aenter__ = AsyncMock(return_value=response)
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        with patch('downloader_service.ClientSession', factory):
            r = await self.client.put('/admin/cookies/instagram', headers=self.headers, json={'content': cookie()})
        self.assertEqual(r.status, 502)
        self.assertFalse((Path(self.tmp.name) / 'instagram_cookies.txt').exists())

    async def test_persist_and_apply_only_selected_platform(self):
        response = MagicMock(status=201)
        session = MagicMock()
        session.put.return_value.__aenter__ = AsyncMock(return_value=response)
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        with patch('downloader_service.ClientSession', factory):
            r = await self.client.put('/admin/cookies/instagram', headers=self.headers, json={'content': cookie()})
        self.assertEqual(r.status, 200)
        self.assertIn('/srv-test/secret-files/INSTAGRAM_COOKIES', session.put.call_args.args[0])
        r = await self.client.get('/admin/cookies', headers=self.headers)
        data = await r.json()
        self.assertEqual(data['platforms']['instagram']['state'], 'present')
        self.assertNotIn('SECRET_VALUE', str(data))

    async def test_failed_whatsapp_job_exposes_auth_reason_until_success(self):
        class FakeDownloader:
            base_opts = {}
            async def download_video(self, url):
                return {'success': False, 'auth_issue': 'login_required'}
        await self.client.close()
        self.client = TestClient(TestServer(build_app('t' * 32, FakeDownloader)))
        await self.client.start_server()
        ident = str(uuid.uuid4())
        await self.client.post('/jobs', headers=self.headers,
                               json={'id': ident, 'url': 'https://instagram.com/reel/example', 'target': 'whatsapp'})
        for _ in range(50):
            job = await (await self.client.get('/jobs/' + ident, headers=self.headers)).json()
            if job['state'] == 'done':
                break
            await asyncio.sleep(.01)
        data = await (await self.client.get('/admin/cookies', headers=self.headers)).json()
        self.assertEqual(data['platforms']['instagram']['issue'], 'login_required')
        install_live('instagram', cookie())
        data = await (await self.client.get('/admin/cookies', headers=self.headers)).json()
        self.assertNotIn('issue', data['platforms']['instagram'])


if __name__ == '__main__':
    unittest.main()
