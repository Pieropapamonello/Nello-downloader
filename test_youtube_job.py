import http.server
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from youtube_job import run_youtube_job, YouTubeResourceError, memory_pressure


class YouTubeJobTests(unittest.TestCase):
    def test_cgroup_pressure(self):
        with patch.object(Path, 'read_text', side_effect=['490000000', '536870912', 'inactive_file 0']):
            self.assertTrue(memory_pressure())
        with patch.object(Path, 'read_text', side_effect=['300000000', '536870912', 'inactive_file 0']):
            self.assertFalse(memory_pressure())

    def test_reclaimable_file_cache_is_not_process_memory(self):
        with patch.object(Path, 'read_text', side_effect=[
                '510000000', '536870912', 'inactive_file 150000000']):
            self.assertFalse(memory_pressure())

    def test_isolated_worker_does_not_launch_nested_process(self):
        with patch.dict('os.environ', {'NELLO_ISOLATED_MEDIA_WORKER': '1'}), \
                patch('youtube_job.execute_job', return_value={'info': {'id': 'test'}}), \
                patch('youtube_job.subprocess.Popen') as launch:
            self.assertEqual(run_youtube_job({}, 'unused')['info']['id'], 'test')
            launch.assert_not_called()

    def test_http_server_does_not_load_extractor(self):
        result = subprocess.run([sys.executable, '-c',
            'import downloader_service, sys; assert "yt_dlp" not in sys.modules; '
            'assert "social_downloader" not in sys.modules'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_launch_under_pressure(self):
        with patch('youtube_job.memory_pressure', return_value=True), patch('youtube_job.subprocess.Popen') as launch:
            with self.assertRaises(YouTubeResourceError):
                run_youtube_job({}, 'https://youtu.be/example')
            launch.assert_not_called()

    def test_timeout_reaps_process(self):
        launch = subprocess.Popen
        children = []
        def sleeper(*args, **kwargs):
            if args[0][0] == 'taskkill':
                return launch(*args, **kwargs)
            proc = launch([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(proc)
            return proc
        with patch('youtube_job.memory_pressure', return_value=False), \
                patch('youtube_job.subprocess.Popen', side_effect=sleeper):
            started = time.monotonic()
            with self.assertRaises(YouTubeResourceError):
                run_youtube_job({}, 'unused', timeout=0.15)
            self.assertIsNotNone(children[0].poll())
            self.assertLess(time.monotonic() - started, 5)

    def test_extract_then_download_through_real_worker(self):
        payload = b'local fixture, no external network'
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch('youtube_job.memory_pressure', return_value=False):
                url = f'http://127.0.0.1:{server.server_port}/clip.mp4'
                opts = {'outtmpl': directory + '/clip.%(ext)s', 'quiet': True,
                        'force_generic_extractor': True, 'noprogress': True, 'proxy': ''}
                info = run_youtube_job(opts, url)['info']
                result = run_youtube_job(opts, url, download=True, info=info)
                self.assertEqual(Path(result['filename']).read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
