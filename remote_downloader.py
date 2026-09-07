"""Submit jobs and stream completed media to local temporary files."""
import asyncio
import logging
import os
import re
import tempfile
import uuid

import aiohttp

log = logging.getLogger(__name__)


async def remote_download(url, kind='video', target='', max_bytes=16 * 1024 * 1024,
                          on_download_ready=None):
    base = os.environ['DOWNLOADER_URL'].rstrip('/')
    token = os.environ['DOWNLOADER_TOKEN']
    ident = str(uuid.uuid4())
    paths = []
    done = False
    timeout = aiohttp.ClientTimeout(total=900, sock_connect=20, sock_read=150)
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}, timeout=timeout) as session:
        try:
            # Waking a free Render service can return HTML before the API is ready.
            for attempt in range(40):
                try:
                    async with session.get(base + '/healthz') as response:
                        if response.status == 200 and (await response.json()).get('status') == 'ok':
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                await asyncio.sleep(3)
            else:
                raise RuntimeError('remote downloader unavailable')
            async with session.post(base + '/jobs', json={'id': ident, 'url': url, 'kind': kind,
                                                          'target': target, 'max_bytes': max_bytes}) as response:
                response.raise_for_status()
            for attempt in range(400):
                async with session.get(base + '/jobs/' + ident) as response:
                    response.raise_for_status()
                    job = await response.json()
                if job['state'] == 'done':
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError('remote download timed out')
            result = job['result']
            if not result.get('success'):
                log.warning('Remote download failed (%s): %s', url, result)
                return result
            if on_download_ready:
                await on_download_ready()
            documents = []
            total = 0
            for media in result.pop('media', []):
                suffix = media.get('suffix', '')
                if not re.fullmatch(r'\.[A-Za-z0-9]{1,8}', suffix):
                    raise ValueError('invalid media suffix')
                fd, path = tempfile.mkstemp(prefix='remote_', suffix=suffix)
                paths.append(path)
                size = 0
                with os.fdopen(fd, 'wb') as output:
                    async with session.get(f'{base}/jobs/{ident}/files/{int(media["index"])}') as response:
                        response.raise_for_status()
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if size > 100 * 1024 * 1024 or total > 300 * 1024 * 1024:
                                raise ValueError('media exceeds transfer limit')
                            output.write(chunk)
                if size != media['size']:
                    raise ValueError('incomplete media transfer')
                if media.get('document'):
                    documents.append(path)
            if not paths:
                raise ValueError('no media returned')
            if kind == 'audio' or result.get('type', 'video') == 'video':
                result['file_path'] = paths[0]
            else:
                result['files'] = paths
            result['_documents'] = documents
            done = True
            return result
        except Exception as exc:
            log.warning('Remote downloader error (%s): %s', url, exc)
            return {'success': False, 'error': str(exc)}
        finally:
            if not done:
                for path in paths:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            try:
                async with session.delete(base + '/jobs/' + ident, timeout=aiohttp.ClientTimeout(total=10)):
                    pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
