"""Read changing burned captions only when they match the spoken English.

OCR runs on small binary text strips in batches, not on entire video frames.
Static signs, logos, and uncertain detections never authorize a black mask.
"""
import csv
import io
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

log = logging.getLogger(__name__)
ROW = 96
diagnostics = {}


def words(text):
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())


def recognized_rows(tsv, count, confidence=35):
    result = [[] for _ in range(count)]
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        try:
            index = int(row['top']) // ROW
            text = row['text'].strip()
            if 0 <= index < count and float(row['conf']) >= confidence and words(text):
                result[index].append(text)
        except (KeyError, ValueError):
            continue
    return [' '.join(r).strip() for r in result]


def ocr(strips, path, timeout, confidence=35):
    from PIL import Image
    sheet = Image.new('L', (400, ROW * len(strips)), 255)
    for i, strip in enumerate(strips):
        strip.thumbnail((360, 64))
        sheet.paste(strip, (20 + (360 - strip.width) // 2, i * ROW + 16))
    sheet.save(path)
    env = dict(os.environ, OMP_THREAD_LIMIT='1', OMP_NUM_THREADS='1')
    result = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'eng', '--psm', '6', 'tsv'],
                            capture_output=True, text=True, check=True, timeout=timeout, env=env)
    return recognized_rows(result.stdout, len(strips), confidence)


def binary(image):
    # White caption glyphs become black on white; colorful scenery is suppressed.
    from PIL import ImageChops
    r, g, b = image.convert('RGB').split()
    minimum = ImageChops.darker(ImageChops.darker(r, g), b)
    return minimum.point(lambda v: 0 if v >= 215 else 255)


def bands(image):
    import numpy as np
    pixels = np.asarray(image)
    counts = (pixels[:, 25:-25] < 128).sum(axis=1)
    rows = [i for i, n in enumerate(counts) if n >= 9 and image.height * .2 < i < image.height * .9]
    groups = []
    for y in rows:
        if groups and y - groups[-1][-1] <= 3:
            groups[-1].append(y)
        else:
            groups.append([y])
    candidates = [(max(0, g[0] - 4), min(image.height, g[-1] + 5), sum(counts[g]))
                  for g in groups if 9 <= g[-1] - g[0] <= 42]
    return [(a, b) for a, b, _ in sorted(candidates, key=lambda x: -x[2])[:4]]


def matches_speech(text, vocabulary):
    tokens = words(text)
    return bool(tokens) and sum(w in vocabulary for w in tokens) / len(tokens) >= .75


def changing_band(samples):
    """Agreement across different frames AND different spoken texts is required."""
    for frame, y1, y2, text in samples:
        nearby = [s for s in samples if abs(s[1] - y1) < 10]
        if len({s[0] for s in nearby}) >= 2 and len({s[3].lower() for s in nearby}) >= 2:
            return min(s[1] for s in nearby), max(s[2] for s in nearby)
    return None


def source_captions(source, cues, duration, metadata):
    diagnostics.clear()
    diagnostics.update(phase='start', outcome='unavailable')
    if not shutil.which('tesseract'):
        return None
    from PIL import Image
    video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    height = round(360 * video['height'] / video['width'] / 2) * 2
    if height > 900 or height < 180:
        return None
    vocabulary = set(words(' '.join(c[2] for c in cues)))
    sheet = Path(source).parent / 'caption_ocr.png'
    started = time.monotonic()
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1']
    try:
        strips, descriptors = [], []
        diagnostics['phase'] = 'sample_frames'
        for i, fraction in enumerate((.15, .45, .75)):
            raw = subprocess.check_output(command + ['-ss', str(duration * fraction), '-i', str(source),
                            '-vf', f'scale=360:{height}', '-frames:v', '1', '-threads', '1',
                            '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], stderr=subprocess.DEVNULL, timeout=8)
            frame = binary(Image.frombytes('RGB', (360, height), raw))
            for a, b in bands(frame):
                strips.append(frame.crop((0, a, 360, b)))
                descriptors.append((i, a, b))
        if not strips:
            return None
        diagnostics['phase'] = 'sample_ocr'
        text = ocr(strips, sheet, timeout=12)
        samples = [(*d, t) for d, t in zip(descriptors, text) if matches_speech(t, vocabulary)]
        band = changing_band(samples)
        if not band:
            diagnostics['outcome'] = 'no_verified_band'
            log.info('No changing burned captions matching speech; use bottom captions')
            return None
        a, b = band
        a, b = (a // 2) * 2, min(height, ((b + 1) // 2) * 2)
        # Decode only the detected strip, at 4 samples/second, in one process.
        fps = 4
        diagnostics['phase'] = 'strip_decode'
        raw = subprocess.check_output(command + ['-i', str(source), '-vf',
                    f'fps={fps},scale=360:{height},crop=360:{b-a}:0:{a}', '-threads', '1',
                    '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], stderr=subprocess.DEVNULL, timeout=30)
        size = 360 * (b - a) * 3
        count = len(raw) // size
        if not 0 < count <= 360:
            return None
        strips = [binary(Image.frombytes('RGB', (360, b-a), raw[i*size:(i+1)*size])) for i in range(count)]
        diagnostics['phase'] = 'track_ocr'
        texts = ocr(strips, sheet, timeout=max(5, 80 - (time.monotonic() - started)), confidence=55)
        result = []
        for i, text in enumerate(texts):
            text = re.sub(r"[^A-Z0-9'’ -]", '', text.upper()).strip(" '’")
            # The moving band is already verified. Do not discard proper names
            # just because the speech recognizer misspelled them.
            if not words(text) or len(text) > 60:
                continue
            start, end = round(i * 1000 / fps), min(round((i+1) * 1000 / fps), round(duration * 1000))
            if result and result[-1][2] == text and result[-1][1] == start:
                result[-1] = (result[-1][0], end, text)
            elif start < end:
                result.append((start, end, text))
        coverage = sum(e-s for s,e,_ in result) / (duration * 1000)
        diagnostics.update(cues=len(result), coverage=round(coverage, 2))
        if len(result) < 4 or coverage < .65:
            diagnostics['outcome'] = 'verified_mask_only'
            log.info('Burned captions incomplete: coverage=%.2f; preserve spoken captions', coverage)
            # The band is verified even when some words cannot be read.
            return cues, [0, a / height, 1, b / height], False
        log.info('Burned captions read: cues=%d coverage=%.2f seconds=%.1f', len(result), coverage, time.monotonic()-started)
        diagnostics['outcome'] = 'translated_visible_text'
        # The translation model was trained on prose, not all-caps graphics.
        # Rendering restores uppercase after translation.
        return [(s, e, text.capitalize()) for s, e, text in result], [0, a / height, 1, b / height], True
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        diagnostics['outcome'] = type(exc).__name__
        log.info('Burned captions unavailable: %s; use bottom captions', type(exc).__name__)
        return None
    finally:
        diagnostics['seconds'] = round(time.monotonic() - started, 1)
        sheet.unlink(missing_ok=True)
