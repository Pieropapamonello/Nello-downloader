"""Run YouTube extraction in a disposable process, with memory headroom checks.

The cgroup watchdog is best effort, not a hard kernel memory limit. It protects
the bot from sustained growth and releases extractor memory after each job.
"""
import json
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
                return used >= limit - min(64 * 1024 * 1024, limit // 5)
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
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        finally:
            # Kill the whole group, including an orphaned JS solver.
            if os.name == 'posix' or proc.poll() is None:
                stop_job(proc)


def main():
    import yt_dlp
    job = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        with yt_dlp.YoutubeDL(job['opts']) as ydl:
            info = (ydl.process_ie_result(job['info'], download=True)
                    if job['download'] and job.get('info') else
                    ydl.extract_info(job['url'], download=job['download']))
            result = {'info': ydl.sanitize_info(info), 'filename': ydl.prepare_filename(info)}
    except Exception as exc:
        result = {'error': str(exc)[:500]}
    Path(sys.argv[2]).write_text(json.dumps(result), encoding='utf-8')


if __name__ == '__main__':
    main()
