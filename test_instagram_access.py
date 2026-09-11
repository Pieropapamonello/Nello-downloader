import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from cookie_health import AuthDiagnostics, response_access_issue, install_live
from social_downloader import SocialMediaDownloader
from smd_instagram import InstagramMixin

VALID = '# Netscape HTTP Cookie File\n#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\ttest-session\n'


class InstagramAccessTests(unittest.TestCase):
    def test_httponly_and_expired_cookie_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text(VALID + '.instagram.com\tTRUE\t/\tTRUE\t1\told\texpired\n')
            result = SocialMediaDownloader._load_netscape_cookies(None, str(path))
            self.assertEqual(result, {'sessionid': 'test-session'})

    def test_fallback_uses_original_after_extractor_rewrites_jar(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'COOKIE_UPDATE_DIR': directory}):
            install_live('instagram', VALID)
            jar = Path(directory) / 'managed_instagram_cookies.txt'
            jar.write_text('# Netscape HTTP Cookie File\n')
            result = SocialMediaDownloader._load_netscape_cookies(None, str(jar))
            self.assertEqual(result, {'sessionid': 'test-session'})

    def test_structured_restrictions_not_confused_with_expiry(self):
        cases = [(400, {'message': 'feedback_required'}, 'account_restricted'),
                 (403, {'message': 'challenge_required'}, 'access_check'),
                 (200, {'checkpoint_url': '/checkpoint/'}, 'access_check'),
                 (429, {}, 'rate_limited'), (401, {}, 'session_rejected'),
                 (403, {}, 'access_denied'), (200, {}, None)]
        for status, payload, expected in cases:
            self.assertEqual(response_access_issue(status, 'https://instagram.com/api/', payload), expected)
        self.assertEqual(response_access_issue(200, 'https://instagram.com/accounts/login/'), 'session_rejected')

    def test_specific_account_alert_overrides_generic_login(self):
        capture = AuthDiagnostics()
        for message in ('login_required', 'Instagram access diagnostic: access_check (HTTP 403)',
                        'Instagram access diagnostic: account_restricted (HTTP 400)', 'login_required'):
            capture.emit(logging.LogRecord('test', 40, '', 0, message, (), None))
        self.assertEqual(capture.issue, 'account_restricted')

    def test_api_stops_on_checkpoint_without_exposing_body(self):
        dl = Mock(spec=InstagramMixin)
        dl.instagram_cookies = 'unused'
        dl.proxy_dict = None
        dl._load_netscape_cookies = Mock(return_value={'sessionid': 'test-session'})
        response = Mock(status_code=200, url='https://instagram.com/challenge/')
        response.json.side_effect = ValueError()
        with patch('smd_instagram.requests.get', return_value=response) as get:
            result = InstagramMixin._instagram_api_fallback_sync(dl, 'https://instagram.com/reel/ABC/')
        self.assertEqual(result, [])
        self.assertEqual(dl._instagram_access_issue, 'access_check')
        get.assert_called_once()


if __name__ == '__main__':
    unittest.main()
