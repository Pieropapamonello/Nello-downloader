"""Extract only the requested Facebook Photo node, never arbitrary page images."""
import asyncio
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

log = logging.getLogger(__name__)


def photo_id(url):
    parsed = urlsplit(url)
    if (parsed.hostname or '').lower() not in ('facebook.com', 'www.facebook.com', 'm.facebook.com'):
        return None
    if parsed.path.rstrip('/') not in ('/photo', '/photo.php'):
        return None
    ident = parse_qs(parsed.query).get('fbid', [''])[0]
    return ident if ident.isdigit() else None


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('type') == 'application/json'
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.blocks.append(''.join(self.parts))
            self.parts = []
            self.active = False


def extract_photo(page, ident):
    parser = Scripts()
    parser.feed(page)
    image, area, description, owner = None, 0, '', ''
    for block in parser.blocks:
        try:
            stack = [json.loads(block)]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
                if str(node.get('id')) != ident:
                    continue
                candidate = node.get('image') or {}
                if isinstance(candidate, dict):
                    uri = candidate.get('uri', '')
                    host = (urlsplit(uri).hostname or '').lower()
                    size = int(candidate.get('width') or 0) * int(candidate.get('height') or 0)
                    if (uri.startswith('https://') and host.endswith('.fbcdn.net') and size > area
                            and '/rsrc.php' not in uri and 'silhouette' not in uri):
                        image, area = uri, size
                story = node.get('creation_story') or {}
                message = node.get('message') or story.get('message') or {}
                if isinstance(message, dict) and isinstance(message.get('text'), str):
                    if len(message['text']) > len(description):
                        description = message['text']
                actor = node.get('owner') or {}
                if isinstance(actor, dict) and isinstance(actor.get('name'), str):
                    owner = actor['name']
    return {'image': image, 'description': description, 'owner': owner} if image else None


async def download_photo(dl, url):
    ident = photo_id(url)

    def run():
        from curl_cffi import requests
        cookies = dl._load_netscape_cookies(getattr(dl, 'facebook_cookies', None))
        for jar in ([None, cookies] if cookies else [None]):
            try:
                response = requests.get(url, impersonate='chrome99', cookies=jar,
                                        proxies=getattr(dl, 'proxy_dict', None), timeout=20)
                if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                    continue
                media = extract_photo(response.text, ident)
                if not media:
                    continue
                # Cookies are never forwarded to the CDN.
                response = requests.get(media['image'], impersonate='chrome99', timeout=30, stream=True,
                                        proxies=getattr(dl, 'proxy_dict', None))
                try:
                    if response.status_code != 200:
                        continue
                    path = Path(dl.temp_dir) / ('facebook_' + ident + '.jpg')
                    total = 0
                    first = True
                    with path.open('wb') as output:
                        for chunk in response.iter_content(256 * 1024):
                            if not chunk:
                                continue
                            if first and not (chunk.startswith(b'\xff\xd8\xff') or chunk.startswith(b'\x89PNG\r\n\x1a\n')
                                              or (chunk.startswith(b'RIFF') and chunk[8:12] == b'WEBP')):
                                raise ValueError('not an image')
                            first = False
                            total += len(chunk)
                            if total > 20 * 1024 * 1024:
                                raise ValueError('image too large')
                            output.write(chunk)
                    if total < 100:
                        path.unlink(missing_ok=True)
                        continue
                    return dl._pack_media_result([str(path)], media['description'], media['owner'], 'facebook', url)
                finally:
                    response.close()
            except Exception as exc:
                log.warning('Facebook photo extraction failed: %s', type(exc).__name__)
        log.warning('Facebook requested photo %s unavailable; refusing page avatars and previews', ident)
        return {'success': False, 'error': 'Facebook did not provide the requested photo'}

    return await asyncio.to_thread(run)
