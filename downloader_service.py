"""Authenticated media job service. No bot sessions or database credentials."""
import asyncio
import hmac
import logging
import os
from pathlib import Path
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from aiohttp import web
from media_worker import run_media_job
from wa_media import prepare_video
VIDEO_EXTS = ('.mp4', '.mov', '.webm', '.mkv', '.avi', '.flv', '.ts')

log = logging.getLogger(__name__)
DOMAINS = ('youtube.com', 'youtu.be', 'facebook.com', 'fb.watch', 'tiktok.com',
           'instagram.com', 'twitter.com', 'x.com', 'reddit.com', 'redd.it', 'twitch.tv')


def build_app(token=None, downloader_factory=None):
    token = token or os.environ.get('DOWNLOADER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('DOWNLOADER_TOKEN must contain at least 32 characters')
    jobs = {}
    queue = asyncio.Queue(maxsize=8)

    @web.middleware
    async def authenticate(request, handler):
        if request.path != '/healthz' and not hmac.compare_digest(
                request.headers.get('Authorization', ''), 'Bearer ' + token):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def submit(request):
        body = await request.json()
        url = body.get('url', '')
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower()
            valid = (parsed.scheme in ('http', 'https') and not parsed.username
                     and parsed.port in (None, 80, 443)
                     and any(host == d or host.endswith('.' + d) for d in DOMAINS))
            ident = str(uuid.UUID(body.get('id', '')))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or body.get('kind', 'video') not in ('video', 'audio'):
            raise web.HTTPBadRequest()
        if ident in jobs:
            return web.json_response({'id': ident}, status=202)
        if queue.full() or len(jobs) >= 32:
            raise web.HTTPServiceUnavailable(text='job queue full')
        jobs[ident] = {'state': 'queued', 'body': body, 'created': time.monotonic()}
        queue.put_nowait(ident)
        return web.json_response({'id': ident}, status=202)

    async def status(request):
        job = jobs.get(request.match_info['ident'])
        if not job:
            raise web.HTTPNotFound()
        return web.json_response({k: job[k] for k in ('state', 'result') if k in job})

    async def media(request):
        job = jobs.get(request.match_info['ident'])
        try:
            index = int(request.match_info['index'])
            if not job or job['state'] != 'done' or index < 0:
                raise ValueError()
            path = job['paths'][index]
        except (KeyError, IndexError, ValueError):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def remove(request):
        ident = request.match_info['ident']
        job = jobs.get(ident)
        if job and job['state'] == 'done':
            jobs.pop(ident)
            job['directory'].cleanup()
        return web.json_response({'ok': True})

    async def worker():
        while True:
            ident = await queue.get()
            job = jobs[ident]
            body = job['body']
            job['state'] = 'running'
            directory = tempfile.TemporaryDirectory(prefix='media_job_')
            job['directory'] = directory
            job['paths'] = []
            try:
                target = body.get('target', '')
                if downloader_factory is None:
                    result = await asyncio.to_thread(run_media_job, body, directory.name)
                else:
                    # Injected downloader for API contract tests.
                    dl = downloader_factory()
                    dl.temp_dir = directory.name
                    dl.base_opts['outtmpl'] = os.path.join(directory.name, '%(id)s.%(ext)s')
                    result = await (dl.download_audio(body['url']) if body.get('kind') == 'audio'
                                    else dl.download_video(body['url']))
                if result.get('success'):
                    paths = ([result['file_path']] if result.get('file_path') else result.get('files', []))
                    descriptors = []
                    for path in paths:
                        path = str(Path(path).resolve())
                        if not Path(path).is_relative_to(Path(directory.name).resolve()):
                            raise ValueError('media outside job directory')
                        document = False
                        if target in ('whatsapp', 'discord') and Path(path).suffix.lower() in VIDEO_EXTS:
                            limit = min(int(body.get('max_bytes', 16 * 1024 * 1024)), 50 * 1024 * 1024)
                            if limit < 1024 * 1024:
                                raise ValueError('invalid size limit')
                            try:
                                converted = await asyncio.to_thread(prepare_video, path, max_bytes=limit, timeout=300)
                                os.remove(path)
                                path = converted
                            except Exception as exc:
                                log.warning('Remote preparation failed: %s', type(exc).__name__)
                                document = target == 'whatsapp'
                        if os.path.getsize(path) > 100 * 1024 * 1024:
                            raise ValueError('media too large')
                        job['paths'].append(path)
                        descriptors.append({'index': len(descriptors), 'suffix': Path(path).suffix,
                                            'size': os.path.getsize(path), 'document': document})
                    result.pop('file_path', None)
                    result.pop('files', None)
                    result['media'] = descriptors
                    result['_delivery_prepared'] = target in ('whatsapp', 'discord')
                job['result'] = result
                log.info('Job %s complete: success=%s url=%s', ident, result.get('success'), body['url'])
            except Exception as exc:
                log.exception('Job %s failed', ident)
                job['result'] = {'success': False, 'error': type(exc).__name__}
            finally:
                job['state'] = 'done'
                job['finished'] = time.monotonic()
                queue.task_done()

    async def lifecycle(app):
        async def expire():
            while True:
                await asyncio.sleep(60)
                for ident, job in list(jobs.items()):
                    if job['state'] == 'done' and time.monotonic() - job['finished'] > 1200:
                        jobs.pop(ident)
                        job['directory'].cleanup()
        tasks = [asyncio.create_task(worker()), asyncio.create_task(expire())]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = web.Application(middlewares=[authenticate], client_max_size=16384)
    async def health(request):
        return web.json_response({'status': 'ok', 'queued': queue.qsize()})
    app.add_routes([web.get('/healthz', health), web.post('/jobs', submit),
                    web.get('/jobs/{ident}', status), web.delete('/jobs/{ident}', remove),
                    web.get('/jobs/{ident}/files/{index}', media)])
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Avoid recursion if an environment is accidentally copied from the bot.
    os.environ.pop('DOWNLOADER_URL', None)
    web.run_app(build_app(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
