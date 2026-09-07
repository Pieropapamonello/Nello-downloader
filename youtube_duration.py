"""Read duration from the watch page without loading players or media formats."""
import json
import http.cookiejar
import logging
import os
import re
from urllib.parse import urlsplit, parse_qs

import requests

logger = logging.getLogger(__name__)


def video_id(url):
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host in ('youtu.be', 'www.youtu.be'):
        candidate = parts.path.strip('/').split('/')[0]
    elif host == 'youtube.com' or host.endswith('.youtube.com'):
        segments = parts.path.strip('/').split('/')
        candidate = (segments[1] if len(segments) >= 2 and segments[0] in ('shorts', 'embed', 'live')
                     else parse_qs(parts.query).get('v', [''])[0])
    else:
        return None
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate) else None


def parse_duration(page, expected_id):
    for match in re.finditer(r'(?:ytInitialPlayerResponse\s*=|"ytInitialPlayerResponse"\s*:)\s*', page):
        try:
            data, _ = json.JSONDecoder().raw_decode(page[match.end():])
            details = data.get('videoDetails', {})
            if details.get('videoId') != expected_id or details.get('isLiveContent'):
                continue
            duration = float(details['lengthSeconds'])
            if 0 < duration < float('inf'):
                return duration
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def youtube_duration(url, cookiefile=None, proxies=None):
    ident = video_id(url)
    if not ident:
        return None
    cookies = None
    if cookiefile and os.path.exists(cookiefile):
        cookies = http.cookiejar.MozillaCookieJar(cookiefile)
        try:
            cookies.load(ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError):
            logger.warning('YouTube duration: invalid cookie file')
            cookies = None
    try:
        with requests.get('https://www.youtube.com/watch', params={'v': ident},
                          headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US'},
                          cookies=cookies, proxies=proxies,
                          stream=True, timeout=(5, 10)) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) >= 2 * 1024 * 1024:
                    break
            duration = parse_duration(content.decode('utf-8', errors='replace'), ident)
            logger.info('YouTube duration preflight: id=%s duration=%s authenticated_cookies=%s',
                        ident, duration, bool(cookies))
            return duration
    except requests.RequestException as exc:
        logger.warning('YouTube duration request failed: %s', type(exc).__name__)
        return None
