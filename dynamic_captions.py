"""Compact yellow captions and bounded detection of existing burned text."""
import csv
import io
import logging
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import time

log = logging.getLogger(__name__)


def chunks(cues):
    for start, end, text in cues:
        words = text.upper().split()
        groups, group = [], []
        for word in words:
            if group and (len(group) == 3 or len(' '.join(group + [word])) > 24):
                groups.append(' '.join(group))
                group = []
            group.append(word)
        if group:
            groups.append(' '.join(group))
        weight = sum(len(g) for g in groups)
        consumed, previous = 0, start
        for group in groups:
            consumed += len(group)
            until = start + round((end - start) * consumed / weight)
            if until > previous:
                yield previous, until, group
            previous = until


def text_band(tsv, height):
    """Only large central uppercase text, not small labels or corner watermarks."""
    lines = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        try:
            word = row['text'].strip()
            if float(row['conf']) < 55 or not re.search(r'[A-Za-z]{2}', word):
                continue
            x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
            if not (.15 < y / height < .85 and h / height >= .023 and 50 < x + w / 2 < 310):
                continue
            if word != word.upper():
                continue
            key = tuple(row[k] for k in ('block_num', 'par_num', 'line_num'))
            lines.setdefault(key, []).append((y, word))
        except (ValueError, KeyError):
            continue
    candidates = [min(y for y, _ in words) / height for words in lines.values()
                  if sum(len(w) for _, w in words) >= 4]
    return candidates


def caption_y(source, duration, width, height):
    if not shutil.which('tesseract') or duration <= 0:
        return .64
    image = Path(source).parent / 'caption_probe.png'
    positions = []
    started = time.monotonic()
    try:
        for fraction in (.15, .45, .75):
            if time.monotonic() - started > 30:
                break
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1',
                            '-filter_threads', '1',
                            '-ss', str(duration * fraction), '-i', str(source), '-frames:v', '1',
                            '-vf', 'scale=360:-2', '-threads', '1', str(image)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            result = subprocess.run(['tesseract', str(image), 'stdout', '-l', 'eng', '--psm', '11', 'tsv'],
                                    capture_output=True, text=True, timeout=10, check=True)
            positions.append(text_band(result.stdout, round(360 * height / width)))
        # Require agreement between samples rather than following arbitrary page text.
        for sample in positions:
            for y in sample:
                nearby = [min(frame, key=lambda p: abs(p - y)) for frame in positions
                          if any(abs(p - y) < .04 for p in frame)]
                if len(nearby) >= 2:
                    anchor = max(.12, statistics.median(nearby) - .012)
                    log.info('Dynamic captions positioned above detected text: y=%.3f', anchor)
                    return anchor
    except (subprocess.SubprocessError, OSError) as exc:
        log.info('Caption position detection unavailable (%s); using compact default position', type(exc).__name__)
    finally:
        image.unlink(missing_ok=True)
    return .64


def make_ass(srt, source, duration, width, height):
    from subtitles import parse_captions
    cues = parse_captions(Path(srt).read_text(encoding='utf-8'), 'srt')
    if not cues:
        raise ValueError('empty dynamic captions')
    canvas_height = 640
    canvas_width = round(canvas_height * width / height)
    y = round(canvas_height * caption_y(source, duration, width, height))
    font = min(26, round(canvas_width / 14))
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_width}
PlayResY: {canvas_height}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{font},&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,12,12,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    def stamp(ms):
        cs = round(ms / 10)
        return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'
    events = []
    for start, end, text in chunks(cues):
        safe = text.replace('\\', '/').replace('{', '(').replace('}', ')')
        tags = f'{{\\an2\\pos({canvas_width // 2},{y})\\fscx92\\fscy92\\t(0,90,\\fscx100\\fscy100)}}'
        events.append(f'Dialogue: 0,{stamp(start)},{stamp(end)},Default,,0,0,0,,{tags}{safe}')
    path = Path(srt).with_name('italian.ass')
    path.write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    return path
