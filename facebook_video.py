"""Read progressive video URLs only from the requested reel's JSON nodes."""
import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit
from facebook_photo import Scripts
from smd_facebook import facebook_video_id

log = logging.getLogger(__name__)


def extract_video(page, ident):
    scripts = Scripts()
    scripts.feed(page)
    candidates = {}
    duration = None
    for block in scripts.blocks:
        try:
            stack = [json.loads(block)]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                if str(node.get('id')) != ident:
                    continue
                if node.get('length_in_second'):
                    duration = node['length_in_second']
                for data in (node, node.get('videoDeliveryLegacyFields') or {}):
                    if not isinstance(data, dict):
                        continue
                    for quality in ('sd', 'hd'):
                        uri = data.get('browser_native_' + quality + '_url')
                        if (isinstance(uri, str) and uri.startswith('https://')
                                and (urlsplit(uri).hostname or '').endswith('.fbcdn.net')):
                            candidates[quality] = uri
    uri = candidates.get('sd') or candidates.get('hd')
    return {'url': uri, 'duration': duration} if uri else None


async def download_video(dl, url):
    ident = facebook_video_id(url)
    if not ident:
        return None

    def run():
        from curl_cffi import requests
        cookies = dl._load_netscape_cookies(getattr(dl, 'facebook_cookies', None))
        for jar in ([None, cookies] if cookies else [None]):
            path = Path(dl.temp_dir) / ('facebook_' + ident + '.mp4')
            try:
                page = requests.get('https://www.facebook.com/reel/' + ident,
                                    impersonate='chrome99', cookies=jar, timeout=20,
                                    proxies=getattr(dl, 'proxy_dict', None))
                if page.status_code != 200 or len(page.content) > 8 * 1024 * 1024:
                    continue
                media = extract_video(page.text, ident)
                if not media:
                    continue
                response = requests.get(media['url'], impersonate='chrome99', stream=True, timeout=60,
                                        proxies=getattr(dl, 'proxy_dict', None))
                try:
                    if response.status_code != 200:
                        continue
                    total = 0
                    with path.open('wb') as output:
                        for chunk in response.iter_content(256 * 1024):
                            if not chunk:
                                continue
                            if total == 0 and b'ftyp' not in chunk[:32]:
                                raise ValueError('not MP4')
                            total += len(chunk)
                            if total > 100 * 1024 * 1024:
                                raise ValueError('video too large')
                            output.write(chunk)
                    if total > 1024:
                        log.info('Facebook exact reel fallback succeeded: id=%s bytes=%s', ident, total)
                        return {'success': True, 'type': 'video', 'file_path': str(path),
                                'platform': 'facebook', 'url': url, 'title': '',
                                'duration': media['duration'], 'uploader': ''}
                finally:
                    response.close()
            except Exception as exc:
                log.warning('Facebook exact reel fallback: %s', type(exc).__name__)
            path.unlink(missing_ok=True)
        return None

    return await asyncio.to_thread(run)
