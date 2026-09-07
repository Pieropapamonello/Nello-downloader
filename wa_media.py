"""Prepare downloaded videos for WhatsApp, independently of their source codec."""

import os
import json
import logging
import subprocess
import tempfile
import time
from media_resources import limited_media

logger = logging.getLogger(__name__)


@limited_media
def prepare_video(path, timeout=180, max_bytes=16 * 1024 * 1024):
    """Return a new H.264/AAC MP4; leave the source intact on failure.

    Remux compatible streams without encoding. Otherwise use a lightweight
    encode with a bitrate budget, leaving room for audio and MP4 overhead.
    """
    fd, output = tempfile.mkstemp(prefix='wa_', suffix='.mp4',
                                  dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    started = time.monotonic()
    try:
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-show_streams', '-show_format',
            '-of', 'json', os.path.abspath(path),
        ], check=True, capture_output=True, timeout=min(timeout, 20))
        metadata = json.loads(probe.stdout)
        video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
        audio = next((s for s in metadata['streams'] if s['codec_type'] == 'audio'), None)
        compatible = (video.get('codec_name') == 'h264'
                      and video.get('pix_fmt') == 'yuv420p'
                      and max(video.get('width', 0), video.get('height', 0)) <= 1920)
        audio_compatible = not audio or (audio.get('codec_name') == 'aac'
                                        and audio.get('channels', 0) <= 2)
        copy_streams = compatible and os.path.getsize(path) <= max_bytes * 0.95
        cmd = [
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-xerror', '-y', '-threads', '1', '-filter_threads', '1',
            '-filter_complex_threads', '1', '-i', os.path.abspath(path),
            '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1',
        ]
        if copy_streams:
            cmd += ['-c:v', 'copy']
            cmd += (['-c:a', 'copy'] if audio_compatible else
                    ['-c:a', 'aac', '-ac', '2', '-ar', '48000', '-b:a', '96k'])
        else:
            duration = float(metadata.get('format', {}).get('duration') or video.get('duration') or 0)
            audio_rate = 96000 if audio else 0
            rate = min(1200000, int(max_bytes * 8 * 0.85 / duration) - audio_rate) if duration > 0 else 800000
            if rate < 64000:
                raise ValueError('video too long for WhatsApp size limit')
            cmd += [
                '-vf', "scale=w='min(640,iw)':h='min(640,ih)':"
                       'force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1',
                '-r', '30', '-c:v', 'libx264', '-threads', '1',
                '-preset', 'ultrafast', '-b:v', str(rate), '-maxrate', str(rate),
                '-bufsize', str(rate * 2), '-profile:v', 'baseline',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2', '-ar', '48000',
                '-b:a', '96k',
            ]
        logger.info('WA video preparation: mode=%s input_bytes=%s codec=%s audio=%s dimensions=%sx%s',
                    'remux' if copy_streams else 'encode', os.path.getsize(path),
                    video.get('codec_name'), (audio or {}).get('codec_name'),
                    video.get('width'), video.get('height'))
        cmd += ['-movflags', '+faststart', output]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=max(1, timeout - (time.monotonic() - started)))
        if os.path.getsize(output) == 0:
            raise ValueError('empty converted video')
        if os.path.getsize(output) > max_bytes:
            raise ValueError('converted video exceeds WhatsApp size limit')
        logger.info('WA video ready: output_bytes=%s seconds=%.1f',
                    os.path.getsize(output), time.monotonic() - started)
        return output
    except BaseException:
        os.remove(output)
        raise
