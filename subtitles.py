"""Optional free captions: no speech model, no paid API, fail open to original video."""
import html
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from youtube_job import memory_pressure, stop_job

log = logging.getLogger(__name__)
MAX_DURATION = 180
MAX_BYTES = 512 * 1024
MAX_TEXT = 4000


class TranslationUnavailable(ValueError):
    """Bounded provider diagnostics, without source text or URLs."""
    pass


def language(code):
    base = str(code or '').lower().replace('_', '-').split('-')[0]
    return {'eng': 'en', 'ita': 'it'}.get(base, base)


def tiktok_source_language(data):
    """Only ASR tracks describe the spoken language; MT tracks do not."""
    tracks = (data.get('video') or {}).get('subtitleInfos') or []
    langs = {language(t.get('LanguageCodeName')) for t in tracks
             if str(t.get('Source', '')).upper() == 'ASR'}
    return next(iter(langs)) if len(langs) == 1 else None


def caption_metadata(info):
    if not isinstance(info, dict):
        return {}
    # Language of the selected audio has precedence over original captions (dubbing).
    audio = [f for f in info.get('requested_formats', []) if f.get('acodec') not in (None, 'none')]
    langs = {language(f.get('language')) for f in audio if f.get('language')}
    source = next(iter(langs)) if len(langs) == 1 else language(info.get('language'))
    if len(langs) > 1:
        return {}
    auto = info.get('automatic_captions') or {}
    if not source:
        original = {language(k) for k in auto if k.endswith('-orig')}
        if len(original) == 1:
            source = original.pop()
    if source != 'en':
        return {}
    selected = {}
    for group in (info.get('subtitles') or {}, auto):
        for code, tracks in group.items():
            lang = language(code)
            if lang not in ('en', 'it'):
                continue
            for track in tracks or []:
                if track.get('ext') not in ('vtt', 'srt', 'json3'):
                    continue
                item = {k: track[k] for k in ('ext', 'url', 'data') if k in track}
                if len(json.dumps(item)) <= MAX_BYTES:
                    selected.setdefault(lang, []).append(item)
    if not selected:
        return {}
    return {'language': source, 'tracks': {k: v[:8] for k, v in selected.items()},
            'duration': info.get('duration')}


def timestamp(value):
    fields = value.replace(',', '.').split(':')
    if len(fields) == 2:
        fields.insert(0, '0')
    return int(round((int(fields[0]) * 3600 + int(fields[1]) * 60 + float(fields[2])) * 1000))


def parse_captions(content, ext):
    cues = []
    if ext == 'json3':
        for event in json.loads(content).get('events', []):
            text = ''.join(s.get('utf8', '') for s in event.get('segs', []))
            start = int(event.get('tStartMs', 0))
            cues.append((start, start + int(event.get('dDurationMs', 0)), text))
    else:
        pattern = r'(?m)^((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d+:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n([^\n]+(?:\n(?!\s*\n)[^\n]+)*)'
        for match in re.finditer(pattern, content.replace('\r', '')):
            cues.append((timestamp(match[1]), timestamp(match[2]), match[3]))
    result = []
    for start, end, text in cues:
        text = html.unescape(re.sub(r'<[^>]*>', '', text))
        text = re.sub(r'\s+', ' ', text).strip()
        # Plain SRT only: no ASS commands or control characters from source text.
        text = text.replace('{', '(').replace('}', ')').replace('\\', '/')
        if not text or not (0 <= start < end <= (MAX_DURATION + 5) * 1000):
            continue
        if result and result[-1][2] == text and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]), text)
        else:
            result.append((start, end, text))
    if len(result) > 300:
        raise ValueError('too many cues')
    # Rolling captions overlap; display only one cue at a time.
    result.sort(key=lambda cue: cue[0])
    return [(start, min(end, result[i + 1][0]) if i + 1 < len(result) else end, text)
            for i, (start, end, text) in enumerate(result)
            if i + 1 == len(result) or result[i + 1][0] > start]


def fetch_track(track, session):
    if 'data' in track:
        content = track['data']
        if len(content.encode()) > MAX_BYTES:
            raise ValueError('captions too large')
    else:
        from urllib.parse import urlsplit
        url = track.get('url', '')
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        domains = ('youtube.com', 'googlevideo.com', 'tiktok.com', 'tiktokcdn.com',
                   'tiktokcdn-us.com', 'tiktokcdn-eu.com', 'byteoversea.com',
                   'ibytedtos.com', 'fbcdn.net', 'cdninstagram.com')
        if parts.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in domains):
            raise ValueError('unsupported subtitle host')
        # Signed track URLs are self-contained. Never send login cookies to a CDN.
        with session.get(url, timeout=(5, 10), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError('subtitle track unavailable')
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('captions too large')
            content = data.decode('utf-8-sig')
    return parse_captions(content, track['ext'])


def translate_cues(cues, session):
    """Batch short lines without losing cue alignment; no partial translations."""
    texts = list(dict.fromkeys(c[2] for c in cues))
    if sum(len(t) for t in texts) > MAX_TEXT:
        raise ValueError('free translation budget exceeded')
    batches, batch = [], []
    for text in texts:
        if len(text.encode()) > 450:
            raise ValueError('cue exceeds free translation request limit')
        if len('\n'.join(batch + [text]).encode()) > 450:
            batches.append(batch)
            batch = []
        batch.append(text)
    if batch:
        batches.append(batch)
    translated = {}
    provider = 'mymemory'
    for batch in batches:
        lines = None
        failures = []
        for candidate in (('mymemory', 'google_public') if provider == 'mymemory' else ('google_public',)):
            try:
                if candidate == 'mymemory':
                    with session.get('https://api.mymemory.translated.net/get',
                                     params={'q': '\n'.join(batch), 'langpair': 'en|it'}, timeout=(5, 10)) as response:
                        response.raise_for_status()
                        data = response.json()
                    value = data.get('responseData', {}).get('translatedText')
                    if str(data.get('responseStatus')) != '200' or data.get('quotaFinished') or not value:
                        raise ValueError('free translation quota/unavailable')
                else:
                    # Same public endpoint used by googletrans, not a billable Cloud API.
                    with session.get('https://translate.googleapis.com/translate_a/single',
                                     params={'client': 'gtx', 'sl': 'en', 'tl': 'it', 'dt': 't',
                                             'q': '\n'.join(batch)}, timeout=(5, 10)) as response:
                        response.raise_for_status()
                        data = response.json()
                    value = ''.join(part[0] for part in data[0] if part and isinstance(part[0], str))
                result = [line.strip() for line in html.unescape(value).splitlines() if line.strip()]
                if candidate == 'google_public' and len(result) != len(batch) and len(batch) > 1:
                    # Some responses merge paragraph boundaries; translate each cue separately.
                    result = []
                    for cue in batch:
                        with session.get('https://translate.googleapis.com/translate_a/single',
                                         params={'client': 'gtx', 'sl': 'en', 'tl': 'it', 'dt': 't', 'q': cue},
                                         timeout=(5, 10)) as response:
                            response.raise_for_status()
                            data = response.json()
                        result.append(''.join(part[0] for part in data[0] if part and isinstance(part[0], str)).strip())
                if len(result) != len(batch) or not all(line.strip() for line in result):
                    raise ValueError('translation changed cue alignment')
                lines, provider = result, candidate
                break
            except Exception as exc:
                failures.append(f'{candidate}:{type(exc).__name__}:{getattr(getattr(exc, "response", None), "status_code", "none")}')
                log.info('Free translation provider unavailable: provider=%s type=%s status=%s',
                         candidate, type(exc).__name__, getattr(getattr(exc, 'response', None), 'status_code', None))
        if lines is None:
            raise TranslationUnavailable(';'.join(failures))
        translated.update(zip(batch, lines))
    return [(start, end, translated[text]) for start, end, text in cues]


def srt_time(ms):
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def build_subtitles(meta, output):
    if meta.get('language') != 'en' or not 0 < float(meta.get('duration') or 0) <= MAX_DURATION:
        return False
    import requests
    with requests.Session() as session:
        for lang in ('it', 'en'):
            for track in meta.get('tracks', {}).get(lang, [])[:2]:
                try:
                    cues = fetch_track(track, session)
                    if not cues:
                        continue
                    if lang == 'en':
                        cues = translate_cues(cues, session)
                    text = '\n\n'.join(f'{i}\n{srt_time(start)} --> {srt_time(end)}\n{html.escape(value, quote=False)}'
                                       for i, (start, end, value) in enumerate(cues, 1)) + '\n'
                    Path(output).write_text(text, encoding='utf-8')
                    return True
                except Exception as exc:
                    log.info('Subtitles track unavailable: %s', type(exc).__name__)
    return False


def prepare_subtitles(meta, directory, timeout=60):
    """Separate resource budget: failure cannot discard an already downloaded video."""
    if not meta or memory_pressure():
        return None
    request = Path(directory) / 'subtitle_request.json'
    output = Path(directory) / 'italian.srt'
    request.write_text(json.dumps(meta), encoding='utf-8')
    proc = None
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(request), str(output)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=os.name == 'posix')
        started = time.monotonic()
        while proc.poll() is None:
            if memory_pressure() or time.monotonic() - started > timeout:
                log.info('Subtitles skipped: optional time/memory budget reached')
                return None
            time.sleep(.1)
        if proc.returncode == 0 and output.exists() and output.stat().st_size:
            return str(output)
        return None
    finally:
        if proc and proc.poll() is None:
            stop_job(proc)
        request.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        ok = build_subtitles(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')), sys.argv[2])
    except Exception:
        ok = False
    raise SystemExit(0 if ok else 1)
