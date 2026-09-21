"""Offline, conservative Italian proofreading after the ASR process exits."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time

from youtube_job import memory_pressure, stop_job

log = logging.getLogger(__name__)
LANGUAGE_TOOL_JAR = os.getenv('LANGUAGE_TOOL_JAR', '/opt/proofreader/LanguageTool-6.6/languagetool-commandline.jar')
SAFE_RULES = {'ST_03_001', 'GR_04_001', 'ER_01_001'}


def one_edit(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    shorter, longer = sorted((a, b), key=len)
    i = j = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
        j += 1
        if j - i > 1:
            return False
    return True


def apply_corrections(text, matches):
    # Java offsets count UTF-16 units, not Python characters (notably for emoji).
    offsets, units = {0: 0}, 0
    for i, char in enumerate(text):
        units += len(char.encode('utf-16-le')) // 2
        offsets[units] = i + 1
    edits = []
    for match in matches[:1000]:
        choices = match.get('replacements', [])
        if len(choices) != 1:
            continue
        start_units, length = match.get('offset'), match.get('length')
        if not isinstance(start_units, int) or not isinstance(length, int) or length <= 0:
            continue
        start, end = offsets.get(start_units), offsets.get(start_units + length)
        if start is None or end is None:
            continue
        original, replacement = text[start:end], choices[0].get('value', '')
        if not replacement or replacement == original or len(replacement) > len(original) + 12:
            continue
        if re.search(r'\d|https?://|[@#]', original + replacement):
            continue
        # Never delete a negation or change a name through dictionary guessing.
        protected = {'non', 'mai', 'senza', 'nessuno', 'nessuna'}
        if (set(original.lower().split()) & protected) != (set(replacement.lower().split()) & protected):
            continue
        rule = match.get('rule', {})
        kind = rule.get('issueType')
        if kind == 'misspelling':
            if (not original.islower() or not original.isalpha() or not replacement.isalpha()
                    or not replacement.islower() or not one_edit(original, replacement)):
                continue
        elif kind not in ('grammar', 'typographical') and rule.get('id') not in SAFE_RULES:
            continue
        edits.append((start, end, replacement))
    count, boundary = 0, len(text) + 1
    for start, end, replacement in sorted(edits, reverse=True):
        if end > boundary:
            continue
        text = text[:start] + replacement + text[end:]
        boundary = start
        count += 1
    return text, count


def proofread(text, directory, timeout=40):
    """If checking fails, keep the successful transcription, never lose it."""
    if not text or len(text) > 15000 or not Path(LANGUAGE_TOOL_JAR).is_file() or memory_pressure():
        return text
    source, report = Path(directory) / 'proofreading.txt', Path(directory) / 'proofreading.json'
    started, proc = time.monotonic(), None
    try:
        source.write_text(text, encoding='utf-8')
        command = ['java', '-Xms16m', '-Xmx160m', '-XX:MaxMetaspaceSize=96m',
                   '-XX:+UseSerialGC', '-XX:ActiveProcessorCount=1', '-Dfile.encoding=UTF-8',
                   '-jar', LANGUAGE_TOOL_JAR, '-l', 'it', '--json', str(source)]
        with report.open('wb') as output:
            proc = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL,
                                    start_new_session=os.name == 'posix')
            while proc.poll() is None:
                if memory_pressure() or time.monotonic() - started > timeout:
                    log.info('Voice proofreading: skipped_resource_limit')
                    return text
                time.sleep(.1)
        if proc.returncode != 0 or report.stat().st_size > 2 * 1024 * 1024:
            return text
        checked, count = apply_corrections(text, json.loads(report.read_text(encoding='utf-8')).get('matches', []))
        log.info('Voice proofreading: corrections=%d seconds=%.1f', count, time.monotonic() - started)
        return checked
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return text
    finally:
        if proc is not None and proc.poll() is None:
            stop_job(proc)
        for path in (source, report):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # The containing job directory is also cleaned by the worker.
