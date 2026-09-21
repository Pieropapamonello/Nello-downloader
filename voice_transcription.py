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
import math
from array import array

from speech_subtitles import CLI, MODEL, MULTILINGUAL_MODEL, VAD_MODEL, speech_windows, respect_pauses
from youtube_job import memory_pressure, stop_job

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 180
log = logging.getLogger(__name__)
VOICE_MODEL = os.getenv('WHISPER_VOICE_MODEL', '/opt/whisper/ggml-small-q5_1.bin')


def format_transcript(text):
    """Conservative proofreading: preserve facts, names and spoken meaning."""
    text = re.sub(r'\s+', ' ', text).strip()
    # Only unambiguous orthography, never guess replacements for ASR mistakes.
    for pattern, replacement in (
        (r'\b[Pp]erchè\b', 'perché'), (r'\b[Pp]oichè\b', 'poiché'),
        (r'\b[Aa]ffinch[eè]\b', 'affinché'), (r'\b[Cc]ioe\b', 'cioè'),
        (r'\b[Pp]erche\b', 'perché'),
        (r"\b([Qq])ual\s*['’]\s*[èe]\b", r'\1ual è'),
        (r"\b([Uu])n\s+p[oò](?:['’]|\b)", r"\1n po’"),
        (r'\b([Pp])ò\b', r'\1o’'),
    ):
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\b([LlDd])\s*['’]\s+(?=\w)", r'\1’', text)
    text = re.sub(r'\s+([,.;:!?])', r'\1', text)
    # Keep decimals, times, URLs and abbreviations intact.
    text = re.sub(r'([,;!?])(?=[A-Za-zÀ-ÿ])', r'\1 ', text)
    text = re.sub(r'([.])(?=[A-ZÀ-Ý])', r'\1 ', text)
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Za-zÀ-ÿ])', text)
    paragraphs, current = [], ''
    for sentence in sentences:
        if not sentence:
            continue
        sentence = sentence[0].upper() + sentence[1:]
        if current and len(current) + len(sentence) > 320:
            paragraphs.append(current)
            current = ''
        current = (current + ' ' + sentence).strip()
    if current:
        paragraphs.append(current)
    result = '\n\n'.join(re.sub(r'(.{220,360}[,;])\s+(?=.{80})', r'\1\n\n', p) for p in paragraphs)
    if result and result[-1] not in '.!?…':
        result += '.'
    return result


def audio_chunks(original):
    """Cut at quiet boundaries, keeping every sample and at most 20 seconds."""
    rate, width = original.getframerate(), original.getsampwidth()
    remaining = original.readframes(MAX_SECONDS * rate)
    while remaining:
        if len(remaining) <= 20 * rate * width:
            yield remaining
            return
        # Short encoder windows reduce CPU work as well as memory on Render Free.
        samples = array('h', remaining[:20 * rate * width])
        if sys.byteorder != 'little':
            samples.byteswap()
        quiet, candidates = None, []
        step = rate // 50
        for start in range(12 * rate, 20 * rate, step):
            block = samples[start:start + step]
            silent = sum(value * value for value in block) / len(block) < 580 ** 2
            if silent and quiet is None:
                quiet = start
            if quiet is not None and (not silent or start + step >= 20 * rate):
                end = start if not silent else start + step
                if end - quiet >= .18 * rate:
                    candidates.append((quiet + end) // 2)
                quiet = None
        # A tiny leftover often loses the language/context of the conversation.
        maximum = min(20 * rate, len(remaining) // width - 8 * rate)
        candidates = [n for n in candidates if n <= maximum]
        end = min(candidates, key=lambda n: abs(n - 18 * rate)) if candidates else maximum
        yield remaining[:end * width]
        remaining = remaining[end * width:]


def detected_language(output):
    matches = re.findall(r'auto-detected language:\s*([a-z]+)\s*\(p\s*=\s*([\d.]+)\)', output)
    return (matches[-1][0], float(matches[-1][1])) if matches else ('', 0.0)


def italian_detection(output, seconds=30):
    language, confidence = detected_language(output)
    return language == 'it' and confidence >= (.40 if seconds <= 8 else .85)


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
    if not all(Path(p).is_file() for p in (CLI, MODEL, VOICE_MODEL, VAD_MODEL)):
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
    # Avoid timing out on long notes while retaining Small for short/medium ones.
    selected_model = VOICE_MODEL if duration <= 90 else MULTILINGUAL_MODEL
    recognition = [CLI, '-m', selected_model, '-t', '1', '-ng', '-fa', '-bo', '1', '-bs', '1', '-nf']
    # Decode bounded chunks in separate processes: long VAD buffers otherwise
    # exceed the free worker's RAM. Nothing is returned before every chunk passes.
    texts = []
    with wave.open(str(audio), 'rb') as original:
        for frames in audio_chunks(original):
            sample = directory / 'language_sample.wav'
            with wave.open(str(sample), 'wb') as output:
                output.setparams(original.getparams())
                output.writeframes(frames)
            detected = subprocess.run(command + ['-f', str(sample), '-l', 'auto', '-dl'],
                                      check=True, capture_output=True, text=True, timeout=45)
            seconds = len(frames) / (original.getsampwidth() * original.getnchannels() * original.getframerate())
            language, confidence = detected_language(detected.stderr)
            if not italian_detection(detected.stderr, seconds) and confidence < .85:
                # Tiny sometimes calls a short Italian ending French/Spanish.
                # Verify it with the already installed multilingual model;
                # never force Italian on uncertain/foreign speech.
                if Path(MULTILINGUAL_MODEL).is_file():
                    verification = list(command)
                    verification[verification.index('-m') + 1] = MULTILINGUAL_MODEL
                    detected = subprocess.run(verification + ['-f', str(sample), '-l', 'auto', '-dl'],
                                              check=True, capture_output=True, text=True, timeout=90)
                    language, confidence = detected_language(detected.stderr)
            if not italian_detection(detected.stderr, seconds):
                reason = 'not_italian' if language and language != 'it' and confidence >= .55 else 'uncertain_language'
                return {'success': True, 'skipped': reason, 'detected_language': language,
                        'confidence': round(confidence, 3)}
            output = directory / 'voice_transcript'
            vad = ['--vad', '-vm', VAD_MODEL, '-vsd', '500', '-vp', '50'] if seconds > 8 else []
            context = min(1500, max(256, math.ceil(seconds * 50 / 64) * 64))
            result = subprocess.run(recognition + ['-f', str(sample), '-l', 'it', '-ac', str(context)] + vad + ['-sns', '-oj', '-of', str(output)],
                                    check=True, capture_output=True, text=True, encoding='utf-8', timeout=240)
            data = json.loads(output.with_suffix('.json').read_text(encoding='utf-8'))
            windows = speech_windows(result.stderr) if vad else [(0, round(seconds * 1000))]
            text = transcript_text(data, windows)
            if text:
                texts.append(text)
    text = format_transcript(' '.join(texts))
    return ({'success': True, 'language': 'it', 'text': text} if text and len(text) <= 15000 else
            {'success': True, 'skipped': 'no_clear_speech'})


def run_voice_job(source, timeout=900):
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
        result = json.loads(report.read_text(encoding='utf-8'))
        log.info('Voice outcome: status=%s language=%s confidence=%s',
                 result.get('skipped') or result.get('reason') or 'transcribed',
                 result.get('detected_language') or result.get('language'), result.get('confidence'))
        return result
    finally:
        if proc.poll() is None:
            stop_job(proc)
        log.info('Voice transcription finished: seconds=%.1f', time.monotonic() - started)


if __name__ == '__main__':
    try:
        result = transcribe(sys.argv[1])
    except subprocess.TimeoutExpired:
        result = {'success': False, 'reason': 'recognition_timeout'}
    except Exception:
        result = {'success': False, 'reason': 'recognition_failed'}
    (Path(sys.argv[1]).parent / 'voice_result.json').write_text(json.dumps(result), encoding='utf-8')
