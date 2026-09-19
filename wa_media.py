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
def prepare_video(path, timeout=180, max_bytes=16 * 1024 * 1024, subtitle_path=None):
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
        copy_streams = compatible and os.path.getsize(path) <= max_bytes * 0.95 and not subtitle_path
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
            total_rate = int(max_bytes * 8 * 0.85 / duration) if duration > 0 else 896000
            # Long videos under Discord's small upload cap need a smaller audio
            # allocation too: fixed 96k audio could consume most of the budget.
            audio_rate = (48000 if total_rate < 240000 else 96000) if audio else 0
            audio_channels = 1 if audio_rate == 48000 else 2
            rate = min(1200000, total_rate - audio_rate)
            if rate < 64000:
                raise ValueError('video too long for WhatsApp size limit')
            side = 360 if rate < 400000 else 640
            fps = '20' if rate < 400000 else '30'
            filters = (f"fps={fps},scale=w='min({side},iw)':h='min({side},ih)':"
                       'force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1')
            if subtitle_path:
                # Fixed filename plus cwd avoids filter escaping and path injection.
                if os.path.basename(subtitle_path) != 'italian.srt' or os.path.dirname(os.path.abspath(subtitle_path)) != os.path.dirname(os.path.abspath(path)):
                    raise ValueError('subtitle file outside media directory')
                from dynamic_captions import make_ass
                make_ass(subtitle_path, path, duration, video['width'], video['height'])
                filters += ',ass=italian.ass'
            cmd += [
                '-vf', filters,
                '-r', fps, '-c:v', 'libx264', '-threads', '1',
                '-preset', 'ultrafast', '-b:v', str(rate), '-maxrate', str(rate),
                '-bufsize', str(rate * 2), '-profile:v', 'baseline',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', str(audio_channels), '-ar', '48000',
                '-b:a', str(audio_rate or 96000),
            ]
        logger.info('WA video preparation: mode=%s input_bytes=%s codec=%s audio=%s dimensions=%sx%s',
                    'remux' if copy_streams else 'encode', os.path.getsize(path),
                    video.get('codec_name'), (audio or {}).get('codec_name'),
                    video.get('width'), video.get('height'))
        cmd += ['-movflags', '+faststart', output]
        if subtitle_path:
            from youtube_job import memory_pressure, stop_job
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    cwd=os.path.dirname(os.path.abspath(path)),
                                    start_new_session=os.name == 'posix')
            try:
                while proc.poll() is None:
                    if memory_pressure() or time.monotonic() - started >= timeout:
                        raise TimeoutError('optional subtitle encode budget reached')
                    time.sleep(.1)
                if proc.returncode:
                    raise ValueError('subtitle encode failed')
            finally:
                if proc.poll() is None:
                    stop_job(proc)
        else:
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
