"""Run YouTube extraction in a disposable process, with memory headroom checks.

The cgroup watchdog is best effort, not a hard kernel memory limit. It protects
the bot from sustained growth and releases extractor memory after each job.
"""
import json
import logging
import re
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


class YouTubeResourceError(RuntimeError):
    pass


def memory_pressure():
    for used_path, limit_path in (
        ('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory.max'),
        ('/sys/fs/cgroup/memory/memory.usage_in_bytes',
         '/sys/fs/cgroup/memory/memory.limit_in_bytes'),
    ):
        try:
            used = int(Path(used_path).read_text().strip())
            limit = int(Path(limit_path).read_text().strip())
            if 0 < limit < 2 ** 60:
                reclaimable = 0
                try:
                    stats = dict(line.split() for line in
                                 (Path(used_path).parent / 'memory.stat').read_text().splitlines())
                    key = 'total_inactive_file' if 'usage_in_bytes' in used_path else 'inactive_file'
                    reclaimable = max(0, int(stats.get(key, 0)))
                except (OSError, ValueError):
                    pass
                working = used - min(used, reclaimable)
                pressure = working >= limit - min(64 * 1024 * 1024, limit // 5)
                if pressure:
                    logging.getLogger(__name__).warning(
                        'YouTube memory: total=%d inactive_file=%d working=%d limit=%d',
                        used, reclaimable, working, limit)
                return pressure
        except (OSError, ValueError):
            continue
    return False


def stop_job(proc):
    if os.name == 'posix':
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Test/development on Windows; Render uses the POSIX process group.
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()


def run_youtube_job(opts, url, download=False, info=None, timeout=90):
    if os.environ.get('NELLO_ISOLATED_MEDIA_WORKER') == '1':
        # The outer supervisor already owns and monitors this process group.
        result = execute_job({'opts': opts, 'url': url, 'download': download, 'info': info})
        for warning in result.get('warnings', []):
            logging.getLogger(__name__).warning('yt-dlp: %s', warning)
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result
    if memory_pressure():
        raise YouTubeResourceError('YouTube sospeso: memoria del server insufficiente. Gli altri bot restano attivi.')
    with tempfile.TemporaryDirectory(prefix='youtube_job_') as directory:
        job_path = Path(directory) / 'job.json'
        result_path = Path(directory) / 'result.json'
        job_path.write_text(json.dumps({'opts': opts, 'url': url, 'download': download,
                                        'info': info}), encoding='utf-8')
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 str(job_path), str(result_path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=os.name == 'posix')
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if memory_pressure():
                    raise YouTubeResourceError('Download YouTube interrotto per proteggere la memoria del server.')
                if time.monotonic() - started > timeout:
                    raise YouTubeResourceError('YouTube non risponde entro il tempo disponibile.')
                time.sleep(0.1)
            if not result_path.exists():
                raise YouTubeResourceError('Il processo YouTube si è interrotto; download non completato.')
            result = json.loads(result_path.read_text(encoding='utf-8'))
            for warning in result.get('warnings', []):
                logging.getLogger(__name__).warning('yt-dlp: %s', warning)
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        finally:
            # Kill the whole group, including an orphaned JS solver.
            if os.name == 'posix' or proc.poll() is None:
                stop_job(proc)


def execute_job(job):
    import yt_dlp
    warnings = []
    class JobLogger:
        def debug(self, message):
            pass
        def warning(self, message):
            warnings.append(re.sub(r'https?://\S+', '[URL]', str(message))[:700])
        def error(self, message):
            pass
    job['opts']['logger'] = JobLogger()
    job['opts']['no_warnings'] = False
    try:
        with yt_dlp.YoutubeDL(job['opts']) as ydl:
            info = (ydl.process_ie_result(job['info'], download=True)
                    if job['download'] and job.get('info') else
                    ydl.extract_info(job['url'], download=job['download']))
            result = {'info': ydl.sanitize_info(info), 'filename': ydl.prepare_filename(info)}
    except Exception as exc:
        result = {'error': str(exc)[:500]}
    result['warnings'] = warnings[-20:]
    return result


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    Path(sys.argv[2]).write_text(json.dumps(execute_job(job)), encoding='utf-8')


if __name__ == '__main__':
    main()
