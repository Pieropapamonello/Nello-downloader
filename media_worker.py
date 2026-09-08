"""Keep the HTTP server lean; release all extractor memory after every job."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from youtube_job import memory_pressure, stop_job


def run_media_job(body, directory, timeout=300):
    request = Path(directory) / 'request.json'
    result = Path(directory) / 'result.json'
    request.write_text(json.dumps(body), encoding='utf-8')
    env = os.environ.copy()
    env['NELLO_ISOLATED_MEDIA_WORKER'] = '1'
    env.pop('DOWNLOADER_URL', None)
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                             str(request), str(result)], env=env,
                            start_new_session=os.name == 'posix')
    started = time.monotonic()
    try:
        while proc.poll() is None:
            if memory_pressure():
                return {'success': False, 'error': 'Media worker memory limit reached'}
            if time.monotonic() - started > timeout:
                return {'success': False, 'error': 'Media worker timed out'}
            time.sleep(0.1)
        if proc.returncode != 0 or not result.exists():
            return {'success': False, 'error': 'Media worker interrupted'}
        return json.loads(result.read_text(encoding='utf-8'))
    finally:
        if os.name == 'posix' or proc.poll() is None:
            stop_job(proc)
        request.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


async def main():
    import logging
    from social_downloader import SocialMediaDownloader
    logging.basicConfig(level=logging.INFO)
    request, result_path = map(Path, sys.argv[1:3])
    body = json.loads(request.read_text(encoding='utf-8'))
    dl = SocialMediaDownloader()
    dl.base_opts['max_filesize'] = 100 * 1024 * 1024
    dl.base_opts['format_sort'] = ['res:480']
    dl.temp_dir = str(request.parent)
    dl.base_opts['outtmpl'] = str(request.parent / '%(id)s.%(ext)s')
    if body.get('target') == 'whatsapp':
        dl.base_opts['format'] = ('best[ext=mp4][vcodec~="^(avc1|h264)"][acodec!=none]/'
                                 + dl.base_opts['format'])
    result = await (dl.download_audio(body['url']) if body.get('kind') == 'audio'
                    else dl.download_video(body['url']))
    result_path.write_text(json.dumps(result), encoding='utf-8')


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
