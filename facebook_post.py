"""Resolve Facebook shares to their own story and its explicit attachments."""
import asyncio
import json
import logging
import re
from urllib.parse import urlsplit, parse_qs, urlunsplit

from facebook_photo import Scripts, photo_id, download_photo
from smd_facebook import facebook_video_id

log = logging.getLogger(__name__)


def is_post_url(url):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    return (host == 'facebook.com' or host.endswith('.facebook.com')) and (
        p.path.startswith('/share/') or '/posts/' in p.path or
        p.path in ('/permalink.php', '/story.php'))


def post_id(url):
    p = urlsplit(url)
    if (p.hostname or '').lower() not in ('facebook.com', 'www.facebook.com', 'm.facebook.com'):
        return None
    match = re.search(r'/posts/(?:[^/]+/)?(\d+|pfbid[A-Za-z0-9]+)/?$', p.path)
    if match:
        return match[1]
    if p.path in ('/permalink.php', '/story.php'):
        ident = parse_qs(p.query).get('story_fbid', [''])[0]
        return ident if re.fullmatch(r'\d+|pfbid[A-Za-z0-9]+', ident) else None
    return None


class Page(Scripts):
    def __init__(self):
        super().__init__()
        self.canonical = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical.append(attrs.get('href', ''))
        if tag == 'meta' and attrs.get('property') == 'og:url':
            self.canonical.append(attrs.get('content', ''))


def resolve_page(page, final_url):
    parser = Page()
    parser.feed(page)
    candidates = [final_url] + parser.canonical
    def supported(u):
        host = (urlsplit(u).hostname or '').lower()
        return (host == 'facebook.com' or host.endswith('.facebook.com')) and (
            post_id(u) or photo_id(u) or facebook_video_id(u))
    target = next((u for u in candidates if supported(u)), None)
    if not target:
        return None
    target = urlunsplit(urlsplit(target)._replace(fragment=''))
    if photo_id(target):
        return {'kind': 'Photo', 'id': photo_id(target), 'url': target, 'description': ''}
    if facebook_video_id(target):
        return {'kind': 'Video', 'id': facebook_video_id(target), 'url': target, 'description': ''}
    ident = post_id(target)
    stories, references, description = [], set(), ''
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
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                if str(node.get('post_id')) == ident or post_id(node.get('permalink_url') or '') == ident:
                    stories.append(node)
    # Only attachments of the matched story; never the page's recommended media.
    for story in stories:
        message = story.get('message') or {}
        if isinstance(message, dict) and len(message.get('text', '')) > len(description):
            description = message['text']
        stack = list(story.get('attachments') or [])
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                media = node.get('media')
                if isinstance(media, dict) and media.get('__typename') in ('Photo', 'Video') and str(media.get('id', '')).isdigit():
                    references.add((media['__typename'], str(media['id'])))
                for key in ('styles', 'attachment', 'subattachments', 'all_subattachments', 'nodes', 'edges', 'node'):
                    value = node.get(key)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
    if len(references) != 1:
        return None  # Do not guess a media for unsupported/ambiguous albums.
    kind, media_id = references.pop()
    return {'kind': kind, 'id': media_id, 'url': target, 'description': description}


async def download_post(dl, url):
    def fetch():
        from curl_cffi import requests
        cookies = dl._load_netscape_cookies(getattr(dl, 'facebook_cookies', None))
        for jar in ([None, cookies] if cookies else [None]):
            try:
                response = requests.get(url, impersonate='chrome99', cookies=jar, timeout=20,
                                        proxies=getattr(dl, 'proxy_dict', None))
                if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                    continue
                resolved = resolve_page(response.text, response.url)
                if resolved:
                    return resolved, response.text
            except Exception as exc:
                log.info('Facebook share resolution unavailable: %s', type(exc).__name__)
        return None, None

    resolved, page = await asyncio.to_thread(fetch)
    if not resolved:
        log.warning('Facebook share/post identity unavailable; refusing unrelated media')
        return {'success': False, 'error': 'Facebook post identity unavailable'}
    log.info('Facebook post resolved to exact attachment: type=%s id=%s', resolved['kind'], resolved['id'])
    if resolved['kind'] == 'Photo':
        return await download_photo(dl, url, seed_page=page, photo_ident=resolved['id'],
                                    description=resolved['description'])
    return await dl.download_video('https://www.facebook.com/reel/' + resolved['id'] + '/')
