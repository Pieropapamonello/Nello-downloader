"""Bounded, offline transcription of Italian voice messages only."""
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
import os
import wave

from speech_subtitles import CLI, MODEL, VAD_MODEL, speech_windows, respect_pauses
from youtube_job import memory_pressure, stop_job

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 180
log = logging.getLogger(__name__)


def italian_detection(output):
    matches = re.findall(r'auto-detected language:\s*([a-z]+)\s*\(p\s*=\s*([\d.]+)\)', output)
    return bool(matches) and matches[-1][0] == 'it' and float(matches[-1][1]) >= .85


def transcript_text(data, windows):
    if data.get('result', {}).get('language') != 'it':
        return ''
    if not windows:
        return ''
    data = respect_pauses(data, windows)
    lines = []
    for item in data.get('transcription', []):
        text = re.sub(r'\s+', ' ', item.get('text', '')).strip()
        if not text or re.fullmatch(r'[\[(].*[\])]', text):
            continue
        if not lines or lines[-1] != text:
            lines.append(text)
    if len(lines) >= 6 and len(set(lines)) < len(lines) / 3:
        return ''
    text = ' '.join(lines)
    return text if len(text) <= 15000 else ''


def transcribe(source):
    if not all(Path(p).is_file() for p in (CLI, MODEL, VAD_MODEL)):
        return {'success': False, 'reason': 'model_unavailable'}
    directory = Path(source).parent
    meta = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_format',
                     '-show_streams', '-of', 'json', source], timeout=10))
    duration = float(meta.get('format', {}).get('duration') or 0)
    if not 0 < duration <= MAX_SECONDS or not any(s['codec_type'] == 'audio' for s in meta['streams']):
        return {'success': False, 'reason': 'duration_limit'}
    audio = directory / 'voice.wav'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1', '-protocol_whitelist', 'file,pipe', '-i', source,
                    '-vn', '-t', str(MAX_SECONDS + 1), '-ac', '1', '-ar', '16000',
                    '-c:a', 'pcm_s16le', str(audio)], check=True, capture_output=True, timeout=25)
    command = [CLI, '-m', MODEL, '-t', '1', '-ng', '-bo', '1', '-bs', '1', '-nf']
    # Check each 30-second window, not just the opening of a long voice note.
    # whisper.cpp detects language before applying -ot; use separate WAV chunks.
    with wave.open(str(audio), 'rb') as original:
        while frames := original.readframes(30 * 16000):
            sample = directory / 'language_sample.wav'
            with wave.open(str(sample), 'wb') as output:
                output.setparams(original.getparams())
                output.writeframes(frames)
            detected = subprocess.run(command + ['-f', str(sample), '-l', 'auto', '-dl'],
                                      check=True, capture_output=True, text=True, timeout=45)
            if not italian_detection(detected.stderr):
                return {'success': True, 'skipped': 'not_italian_or_uncertain'}
    output = directory / 'voice_transcript'
    result = subprocess.run(command + ['-f', str(audio), '-l', 'it', '--vad', '-vm', VAD_MODEL, '-vsd', '500',
                            '-vp', '50', '-sns', '-oj', '-of', str(output)],
                            check=True, capture_output=True, text=True, timeout=300)
    data = json.loads(output.with_suffix('.json').read_text(encoding='utf-8'))
    text = transcript_text(data, speech_windows(result.stderr))
    return ({'success': True, 'language': 'it', 'text': text} if text else
            {'success': True, 'skipped': 'no_clear_speech'})


def run_voice_job(source, timeout=420):
    """Shares the downloader's serial queue; never loads a model in the bot."""
    if memory_pressure():
        return {'success': False, 'reason': 'resource_limit'}
    report = Path(source).parent / 'voice_result.json'
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(source)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=os.name == 'posix')
    try:
        while proc.poll() is None:
            if memory_pressure() or time.monotonic() - started > timeout:
                return {'success': False, 'reason': 'resource_limit'}
            time.sleep(.1)
        if proc.returncode != 0 or not report.is_file():
            return {'success': False, 'reason': 'recognition_failed'}
        return json.loads(report.read_text(encoding='utf-8'))
    finally:
        if proc.poll() is None:
            stop_job(proc)
        log.info('Voice transcription finished: seconds=%.1f', time.monotonic() - started)


if __name__ == '__main__':
    try:
        result = transcribe(sys.argv[1])
    except Exception:
        result = {'success': False, 'reason': 'recognition_failed'}
    (Path(sys.argv[1]).parent / 'voice_result.json').write_text(json.dumps(result), encoding='utf-8')
