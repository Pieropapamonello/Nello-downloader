"""Cookie metadata and bounded authentication diagnostics; never expose cookie values."""
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

PLATFORMS = {
    'instagram': (('instagram.com',), ('sessionid',)),
    'facebook': (('facebook.com',), ('xs',)),
    'tiktok': (('tiktok.com',), ('sessionid', 'sessionid_ss', 'sid_tt')),
    'youtube': (('youtube.com', 'google.com'), ('SID', '__Secure-3PSID', '__Secure-1PSID')),
}
MAX_COOKIE_BYTES = 512 * 1024


def live_path(platform):
    return Path(os.getenv('COOKIE_UPDATE_DIR', '/tmp/nello_cookies')) / (platform + '_cookies.txt')


def read_content(platform):
    # The downloader uses the same managed override and Render secret precedence.
    for path in (live_path(platform), Path('/etc/secrets') / (platform.upper() + '_COOKIES'),
                 Path('/etc/secrets') / (platform + '_cookies.txt')):
        if path.is_file():
            return path.read_text(encoding='utf-8')
    value = os.getenv(platform.upper() + '_COOKIES', '')
    if value:
        return value
    path = Path(__file__).parent / (platform + '_cookies.txt')
    return path.read_text(encoding='utf-8') if path.is_file() else ''


def rows(content, platform):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_COOKIE_BYTES:
        raise ValueError('File troppo grande (massimo 512 KB).')
    result = []
    for line in content.lstrip('\ufeff').splitlines():
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) != 7:
            raise ValueError('Serve un file cookie Netscape, non JSON.')
        domain = parts[0].lstrip('.').lower()
        if not any(domain == d or domain.endswith('.' + d) for d in PLATFORMS[platform][0]):
            raise ValueError('Esporta soltanto i cookie della piattaforma selezionata.')
        try:
            expiry = int(parts[4] or '0')
        except ValueError:
            raise ValueError('Scadenza cookie non valida.') from None
        if expiry < 0 or parts[1] not in ('TRUE', 'FALSE') or parts[3] not in ('TRUE', 'FALSE'):
            raise ValueError('Formato cookie non valido.')
        result.append((parts[5], expiry, bool(parts[6])))
    return result


def inspect_content(content, platform, now=None):
    now = time.time() if now is None else now
    version = hashlib.sha256(content.encode()).hexdigest()[:16]
    if not content.strip():
        return {'state': 'missing', 'version': version}
    try:
        data = rows(content, platform)
    except ValueError:
        return {'state': 'invalid', 'version': version}
    auth = [expiry for name, expiry, present in data if present and name in PLATFORMS[platform][1]]
    state = ('missing_session' if not auth else
             'expired' if all(expiry > 0 and expiry <= now for expiry in auth) else 'present')
    return {'state': state, 'version': version}


def validate_upload(content, platform):
    rows(content, platform)
    if inspect_content(content, platform)['state'] != 'present':
        raise ValueError('Manca una sessione attiva: accedi al sito ed esporta nuovamente i cookie.')
    return '# Netscape HTTP Cookie File\n' + '\n'.join(
        line for line in content.lstrip('\ufeff').splitlines()
        if line and (not line.startswith('#') or line.startswith('#HttpOnly_'))) + '\n'


def install_live(platform, content):
    path = live_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def platform_for_url(url):
    host = (urlsplit(url).hostname or '').lower()
    for platform, (domains, _) in PLATFORMS.items():
        if any(host == d or host.endswith('.' + d) for d in domains):
            return platform
    return {'youtu.be': 'youtube', 'fb.watch': 'facebook'}.get(host)


ISSUE_PRIORITY = {'login_required': 1, 'session_rejected': 2, 'access_denied': 3,
                  'rate_limited': 4, 'access_check': 5, 'account_restricted': 6}


def classify_access(text):
    text = str(text).lower()
    for issue in ISSUE_PRIORITY:
        if text.startswith('instagram access diagnostic: ' + issue + ' '):
            return issue
    patterns = {
        'account_restricted': ('account_restricted', 'feedback_required', 'account is suspended',
                               'account has been suspended', 'account is disabled',
                               'account has been disabled', 'account is temporarily locked',
                               'account has been locked', 'account has been restricted',
                               'added a restriction to your account', 'cannot create new sessions'),
        'access_check': ('challenge_required', 'checkpoint_required', 'checkpoint_challenge_required',
                         'confirm your identity', 'verify your identity', 'confirm you?re not a bot',
                         "confirm you're not a bot", 'consent_required'),
        'rate_limited': ('rate_limited', 'too many requests', 'please wait a few minutes before you try again'),
        'access_denied': ('access_denied',),
        'session_rejected': ('cookies are no longer valid', 'cookies have expired', 'session has expired',
                             'invalid session', 'login_required'),
        'login_required': ('redirected to the login page', 'login required', 'log in to access',
                           'not logged in', 'cookies scaduti', 'cookie scaduti'),
    }
    for issue, phrases in patterns.items():
        if any(phrase in text for phrase in phrases):
            return issue
    return None


def response_access_issue(status, url, payload=None):
    # Inspect only known response fields and redirect paths; do not log bodies or URLs.
    payload = payload if isinstance(payload, dict) else {}
    text = ' '.join(str(payload.get(key, ''))[:1000] for key in ('message', 'error_type', 'error_title', 'feedback_message'))
    issue = classify_access(text)
    if issue:
        return issue
    path = urlsplit(url).path.lower()
    if payload.get('challenge') or payload.get('checkpoint_url') or '/challenge/' in path or '/checkpoint/' in path:
        return 'access_check'
    if status == 429:
        return 'rate_limited'
    if status == 401 or '/accounts/login' in path:
        return 'session_rejected'
    if status == 403:
        return 'access_denied'
    return None


class AuthDiagnostics(logging.Handler):
    """Keep the most specific reason code, with no response or cookie content."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.issue = None

    def emit(self, record):
        issue = classify_access(record.getMessage())
        if issue and ISSUE_PRIORITY[issue] > ISSUE_PRIORITY.get(self.issue, 0):
            self.issue = issue
