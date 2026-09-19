"""Small, disposable CPU speech recognizer for videos without caption tracks."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from subtitles import translate_cues, srt_time, TranslationUnavailable
from youtube_job import memory_pressure, stop_job

log = logging.getLogger(__name__)
MAX_SECONDS = 90
MIN_ENGLISH_PROBABILITY = 0.85
MODEL = os.getenv('WHISPER_MODEL', '/opt/whisper/ggml-tiny-q5_1.bin')
CLI = os.getenv('WHISPER_CLI', '/opt/whisper/whisper-cli')
VAD_MODEL = os.getenv('WHISPER_VAD_MODEL', '/opt/whisper/ggml-silero-v5.1.2.bin')


class SkipSpeech(Exception):
    pass


def english_detection(output):
    matches = re.findall(r'auto-detected language:\s*([a-z]+)\s*\(p\s*=\s*([\d.]+)\)', output)
    if not matches:
        return False
    code, confidence = matches[-1]
    return code == 'en' and float(confidence) >= MIN_ENGLISH_PROBABILITY


def transcript_cues(data, duration):
    if data.get('result', {}).get('language') != 'en':
        raise SkipSpeech('not_english')
    cues = []
    for item in data.get('transcription', []):
        start, end = (int(item.get('offsets', {}).get(key, 0)) for key in ('from', 'to'))
        text = re.sub(r'\s+', ' ', item.get('text', '')).strip()
        text = text.replace('{', '(').replace('}', ')').replace('\\', '/')
        if not text or re.fullmatch(r'[\[(].*[\])]', text):
            continue
        end = min(end, int(duration * 1000))
        if 0 <= start < end:
            cues.append((start, end, text))
    if not cues or len(cues) > 150 or sum(len(c[2]) for c in cues) > 4000:
        raise SkipSpeech('speech_budget_or_empty')
    if len(cues) >= 6 and len({c[2].lower() for c in cues}) < len(cues) / 3:
        raise SkipSpeech('repeated_speech')
    # Translate phrases together instead of cutting grammar at each ASR line.
    merged = []
    for start, end, text in cues:
        if (merged and not re.search(r'[.!?]["\']?$', merged[-1][2])
                and start - merged[-1][1] <= 250
                and end - merged[-1][0] <= 8500
                and len((merged[-1][2] + ' ' + text).encode('utf-8')) <= 400):
            merged[-1] = (merged[-1][0], end, merged[-1][2] + ' ' + text)
        else:
            merged.append((start, end, text))
    return merged


def speech_windows(log_text):
    windows = []
    for start, end in re.findall(r'VAD segment \d+: start = ([\d.]+), end = ([\d.]+)', log_text):
        start, end = round(float(start) * 1000), round(float(end) * 1000)
        if windows and start - windows[-1][1] < 600:
            windows[-1] = (windows[-1][0], end)
        elif start < end:
            windows.append((start, end))
    return windows


def respect_pauses(data, windows):
    """Trim ASR segments to actual speech; never stretch a word over applause."""
    if not windows:
        raise SkipSpeech('no_verified_speech_windows')
    items = []
    for item in data.get('transcription', []):
        start, end = (int(item.get('offsets', {}).get(k, 0)) for k in ('from', 'to'))
        overlaps = [(max(start, a), min(end, b)) for a, b in windows if max(start, a) < min(end, b)]
        if not overlaps:
            continue
        a, b = max(overlaps, key=lambda p: p[1] - p[0])
        items.append(dict(item, offsets={'from': a, 'to': b}))
    return dict(data, transcription=items)


def readable_cues(cues):
    """Split translated phrases into short captions with proportional timing."""
    import textwrap
    result = []
    for start, end, text in cues:
        pieces = textwrap.wrap(text, width=52, break_long_words=False, break_on_hyphens=False)
        total = sum(len(p) for p in pieces)
        offset = start
        consumed = 0
        for piece in pieces:
            consumed += len(piece)
            until = start + round((end - start) * consumed / total)
            result.append((offset, until, piece))
            offset = until
    return result


def build_from_audio(source, output):
    """No network audio upload: only recognized English text is translated."""
    if not Path(CLI).is_file() or not Path(MODEL).is_file() or not Path(VAD_MODEL).is_file():
        raise SkipSpeech('speech_model_missing')
    directory = Path(output).parent
    (directory / 'caption_layout.json').unlink(missing_ok=True)
    audio = directory / 'speech.wav'
    detection_log = directory / 'speech_detect.log'
    transcript_log = directory / 'speech_transcribe.log'
    transcript = directory / 'speech'
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                '-of', 'json', str(source)], capture_output=True, check=True, timeout=15)
        meta = json.loads(probe.stdout)
        duration = float(meta.get('format', {}).get('duration') or 0)
        if not 0 < duration <= MAX_SECONDS:
            raise SkipSpeech('speech_duration_limit')
        if not any(s.get('codec_type') == 'audio' for s in meta.get('streams', [])):
            raise SkipSpeech('no_audio')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1',
                        '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
                        '-c:a', 'pcm_s16le', str(audio)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        command = [CLI, '-m', MODEL, '-f', str(audio), '-t', '1', '-ng', '-bo', '1', '-bs', '1', '-nf']
        # First detect language on 12 seconds, so Italian videos do not need a full decode.
        with detection_log.open('wb') as handle:
            subprocess.run(command + ['-l', 'auto', '-dl', '-d', '12000'],
                           stdout=subprocess.DEVNULL, stderr=handle, check=True, timeout=45)
        if not english_detection(detection_log.read_text(encoding='utf-8', errors='replace')):
            raise SkipSpeech('not_english_or_uncertain')
        with transcript_log.open('wb') as handle:
            subprocess.run(command + ['-l', 'en', '--vad', '-vm', VAD_MODEL, '-vsd', '500', '-vp', '50',
                                      '-oj', '-ml', '40', '-sow', '-sns', '-of', str(transcript)],
                           stdout=subprocess.DEVNULL, stderr=handle, check=True, timeout=150)
        data = json.loads(transcript.with_suffix('.json').read_text(encoding='utf-8'))
        data = respect_pauses(data, speech_windows(transcript_log.read_text(encoding='utf-8', errors='replace')))
        cues = transcript_cues(data, duration)
        from burned_captions import source_captions
        visual = source_captions(source, cues, duration, meta)
        if visual:
            cues, box, exact = visual
            (directory / 'caption_layout.json').write_text(json.dumps({'box': box, 'source': 'burned' if exact else 'speech'}), encoding='utf-8')
        import requests
        with requests.Session() as session:
            translated = translate_cues(cues, session)
        # Escape SRT markup, including any text returned by the translation service.
        from html import escape
        srt = '\n\n'.join(f'{i}\n{srt_time(start)} --> {srt_time(end)}\n{escape(text, quote=False)}'
                          for i, (start, end, text) in enumerate(translated, 1)) + '\n'
        Path(output).write_text(srt, encoding='utf-8')
        return len(cues)
    finally:
        for path in (audio, detection_log, transcript_log, transcript.with_suffix('.json')):
            path.unlink(missing_ok=True)


def prepare_spoken_subtitles(source, directory, timeout=240):
    """Supervise the entire ASR process group after the downloader has exited."""
    if memory_pressure():
        log.info('Speech subtitles skipped: memory pressure before start')
        return None
    output = Path(directory) / 'italian.srt'
    status = Path(directory) / 'speech_status.json'
    proc = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(source), str(output)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=os.name == 'posix')
        while proc.poll() is None:
            if memory_pressure() or time.monotonic() - started >= timeout:
                log.info('Speech subtitles skipped: memory/time budget')
                return None
            time.sleep(.1)
        report = json.loads(status.read_text(encoding='utf-8')) if status.exists() else {'reason': 'worker_failed'}
        log.info('Speech subtitles: reason=%s seconds=%.1f peak_rss_mb=%s worker_peak_rss_mb=%s',
                 report.get('reason'), time.monotonic() - started, report.get('peak_rss_mb'),
                 report.get('worker_peak_rss_mb'))
        if proc.returncode == 0 and output.exists() and output.stat().st_size:
            return str(output)
        return None
    finally:
        if proc and proc.poll() is None:
            stop_job(proc)
        status.unlink(missing_ok=True)


if __name__ == '__main__':
    reason, ok = 'unknown', False
    try:
        build_from_audio(sys.argv[1], sys.argv[2])
        reason, ok = 'translated_english_audio', True
    except SkipSpeech as exc:
        reason = str(exc)
    except TranslationUnavailable as exc:
        reason = str(exc)
    except Exception as exc:
        reason = type(exc).__name__
    report = {'reason': reason}
    if os.name == 'posix':
        import resource
        report['peak_rss_mb'] = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)
        report['worker_peak_rss_mb'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    (Path(sys.argv[2]).parent / 'speech_status.json').write_text(json.dumps(report), encoding='utf-8')
    raise SystemExit(0 if ok else 1)
