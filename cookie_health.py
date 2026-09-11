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


class AuthDiagnostics(logging.Handler):
    """Keep a reason code only. Anonymous attempt errors matter only if the job fails."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.issue = None

    def emit(self, record):
        text = record.getMessage().lower()
        if any(s in text for s in ('cookies are no longer valid', 'cookies have expired',
                                   'session has expired', 'invalid session', 'login_required')):
            self.issue = 'session_rejected'
        elif self.issue != 'session_rejected':
            if any(s in text for s in ('confirm you’re not a bot', "confirm you're not a bot",
                                       'challenge_required', 'checkpoint_required')):
                self.issue = 'access_check'
            elif self.issue is None and any(s in text for s in (
                    'redirected to the login page', 'login required', 'log in to access',
                    'not logged in', 'cookies scaduti', 'cookie scaduti')):
                self.issue = 'login_required'
