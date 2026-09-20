"""Bounded multilingual screen-text recognition, including silent videos."""
import csv
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from functools import lru_cache

from youtube_job import memory_pressure, stop_job

log = logging.getLogger(__name__)
ROW = 128
LANGS = dict(en='eng', it='ita', fr='fra', es='spa', de='deu', pt='por', nl='nld',
             ru='rus', uk='ukr', pl='pol', cs='ces', sk='slk', ro='ron', hu='hun',
             da='dan', sv='swe', no='nor', fi='fin', tr='tur', el='ell', ar='ara',
             he='heb', fa='fas', hi='hin', bn='ben', ta='tam', te='tel', th='tha',
             vi='vie', id='ind', ja='jpn', ko='kor', zh='chi_sim')


@lru_cache(maxsize=32)
def text_language(text):
    if sum(c.isalpha() for c in text) < 15:
        return None
    from langid.langid import LanguageIdentifier, model
    identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    code, confidence = identifier.classify(text)
    return code if confidence >= .85 else None


def tsv_lines(tsv, row_height=None):
    groups = {}
    for item in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        try:
            text = item['text'].strip()
            if float(item['conf']) < 65 or not any(c.isalpha() for c in text):
                continue
            x, y, w, h = (int(item[k]) for k in ('left', 'top', 'width', 'height'))
            key = y // row_height if row_height else tuple(item[k] for k in ('page_num', 'block_num', 'par_num', 'line_num'))
            groups.setdefault(key, []).append((x, y, w, h, text))
        except (ValueError, KeyError):
            continue
    return [(key, min(p[0] for p in parts), min(p[1] for p in parts),
             max(p[0]+p[2] for p in parts), max(p[1]+p[3] for p in parts),
             ' '.join(p[4] for p in parts)) for key, parts in groups.items()]


def verified_band(samples, height):
    candidates = []
    for frame, x1, y1, x2, y2, text in samples:
        nearby = [s for s in samples if abs((s[2]+s[4])/2 - (y1+y2)/2) < height * .035]
        if len({s[0] for s in nearby}) < 2:
            continue
        texts = list(dict.fromkeys(s[5] for s in nearby))
        if len(texts) == 1 and (len(text.split()) < 5 or x2-x1 < 240):
            continue  # logos and short static labels never authorize a mask
        code = text_language(' '.join(texts))
        if code:
            candidates.append((len(texts), sum(len(t) for t in texts), code, nearby))
    if not candidates:
        return None
    italian = [candidate for candidate in candidates if candidate[2] == 'it']
    if italian:
        candidates = italian
    _, _, code, nearby = max(candidates, key=lambda p: p[:2])
    return code, max(0, min(s[2] for s in nearby)-8), min(height, max(s[4] for s in nearby)+8)


def ocr(path, languages, psm=11, timeout=20):
    result = subprocess.run(['tesseract', str(path), 'stdout', '-l', languages,
                             '--psm', str(psm), 'tsv'], capture_output=True, text=True,
                            timeout=timeout, check=True,
                            env=dict(os.environ, OMP_THREAD_LIMIT='1', OMP_NUM_THREADS='1'))
    return result.stdout


def read_screen(source):
    from PIL import Image, ImageOps, ImageStat
    meta = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                       '-show_format', '-of', 'json', str(source)], timeout=10))
    duration = float(meta.get('format', {}).get('duration') or 0)
    if not 0 < duration <= 90:
        return None
    video = next(s for s in meta['streams'] if s['codec_type'] == 'video')
    width, height = 480, round(480 * video['height'] / video['width'] / 2) * 2
    if not 180 <= height <= 960:
        return None
    directory = Path(source).parent
    sample = directory / 'screen_sample.png'
    languages = 'eng+ita+spa+fra+deu+por'
    ffmpeg = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1']
    samples = []
    for i, fraction in enumerate((.15, .5, .8)):
        raw = subprocess.check_output(ffmpeg + ['-ss', str(duration*fraction), '-i', str(source),
                    '-vf', f'scale={width}:{height}', '-frames:v', '1', '-threads', '1',
                    '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], timeout=12)
        Image.frombytes('L', (width, height), raw).save(sample)
        if i == 0:
            # OSD chooses the writing system, not the spoken language.
            osd = subprocess.run(['tesseract', str(sample), 'stdout', '-l', 'osd', '--psm', '0',
                                  '-c', 'min_characters_to_try=10'], capture_output=True, text=True,
                                 timeout=10, env=dict(os.environ, OMP_THREAD_LIMIT='1'))
            match = re.search(r'^Script: (.+)$', osd.stdout, re.M)
            script = match[1].strip() if match else ''
            languages = {'Cyrillic': 'rus+ukr', 'Arabic': 'ara', 'Han': 'chi_sim+jpn',
                         'Japanese': 'jpn', 'Hangul': 'kor', 'Devanagari': 'hin',
                         'Greek': 'ell', 'Hebrew': 'heb', 'Thai': 'tha'}.get(script, languages)
        for _, x1, y1, x2, y2, text in tsv_lines(ocr(sample, languages)):
            if height * .02 < y1 < height * .98 and sum(c.isalpha() for c in text) >= 5:
                samples.append((i, x1, y1, x2, y2, text))
    band = verified_band(samples, height)
    if not band:
        return None
    code, a, b = band
    if code == 'it':
        return {'language': 'it'}
    a, b = (a // 2)*2, min(height, ((b+1)//2)*2)
    if b-a > height * .25:
        return None
    raw = subprocess.check_output(ffmpeg + ['-i', str(source), '-vf',
             f'fps=2,scale={width}:{height},crop={width}:{b-a}:0:{a}', '-threads', '1',
             '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], timeout=30)
    size = width * (b-a)
    count = len(raw) // size
    if not 1 <= count <= 180:
        return None
    sheet = Image.new('L', (width, count*ROW), 255)
    for i in range(count):
        strip = ImageOps.autocontrast(Image.frombytes('L', (width, b-a), raw[i*size:(i+1)*size]))
        if ImageStat.Stat(strip).median[0] < 128:
            strip = ImageOps.invert(strip)
        strip.thumbnail((width, ROW-16))
        sheet.paste(strip, (0, i*ROW+8))
    sheet.save(sample)
    lines = tsv_lines(ocr(sample, LANGS.get(code, languages), psm=6, timeout=45), ROW)
    cues = []
    for index, _, _, _, _, text in sorted(lines):
        start, end = index*500, min((index+1)*500, round(duration*1000))
        if start >= end or not 0 <= index < count:
            continue
        if cues and cues[-1][1] == start and cues[-1][2] == text:
            cues[-1] = (cues[-1][0], end, text)
        else:
            cues.append((start, end, text))
    if not cues or sum(e-s for s,e,_ in cues) < duration*1000*.35:
        return None
    return {'language': code, 'cues': cues, 'box': [0, a/height, 1, b/height]}


def build_screen(source):
    from subtitles import translate_cues, srt_time
    import requests
    import html
    found = read_screen(source)
    if not found:
        return 'no_verified_text'
    if found['language'] == 'it':
        return 'already_italian'
    with requests.Session() as session:
        translated = translate_cues(found['cues'], session, found['language'])
    directory = Path(source).parent
    (directory / 'italian.srt').write_text('\n\n'.join(f'{i}\n{srt_time(a)} --> {srt_time(b)}\n{html.escape(t, quote=False)}'
                        for i,(a,b,t) in enumerate(translated, 1))+'\n', encoding='utf-8')
    (directory / 'caption_layout.json').write_text(json.dumps({'source': 'burned', 'box': found['box']}))
    return 'translated_screen_text'


def prepare_screen_subtitles(source, directory, timeout=150):
    if memory_pressure():
        return None, False
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(source)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=os.name == 'posix')
    try:
        while proc.poll() is None:
            if memory_pressure() or time.monotonic()-started > timeout:
                return None, False
            time.sleep(.1)
        status = Path(directory) / 'screen_status.json'
        reason = json.loads(status.read_text())['reason'] if status.exists() else 'failed'
        log.info('Screen captions: reason=%s seconds=%.1f', reason, time.monotonic()-started)
        output = Path(directory) / 'italian.srt'
        return (str(output) if reason == 'translated_screen_text' and output.exists() else None,
                reason == 'already_italian')
    finally:
        if proc.poll() is None:
            stop_job(proc)
        for name in ('screen_sample.png', 'screen_status.json'):
            (Path(directory) / name).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        reason = build_screen(sys.argv[1])
    except Exception as exc:
        reason = type(exc).__name__
    (Path(sys.argv[1]).parent / 'screen_status.json').write_text(json.dumps({'reason': reason}))
