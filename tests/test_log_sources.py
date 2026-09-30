import gzip
import http.server
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.log_sources import Cursor, FileSource, HttpSource, LogTailer, S3Source, SshSource, build_source, parse_chunk


ERROR = "[2026-09-28 10:00:0{n}] production.ERROR: SQLSTATE[23000]: Column 'phone' cannot be null\n#0 /srv/app/app/Http/Controllers/ProfileController.php(39): save()\n"
INFO = "[2026-09-28 10:00:00] production.INFO: user logged in\n"


def errors(count, start=0):
    return "".join(ERROR.format(n=start + n) for n in range(count))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ParseChunkTest(unittest.TestCase):
    def test_parses_multiline_entries_and_ignores_info(self):
        events = parse_chunk(INFO + errors(2))
        self.assertEqual(2, len(events))
        self.assertEqual("app/Http/Controllers/ProfileController.php:39", events[0].top_application_frame)


class FileSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "laravel.log"

    def test_starts_at_end_then_reads_appends(self):
        self.path.write_text(errors(2), encoding="utf-8")
        source = FileSource(self.path)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        self.assertEqual([], tailer.poll()[0])
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(errors(1, start=5))
        events, cursor, polled = tailer.poll()
        self.assertTrue(polled)
        self.assertEqual(1, len(events))
        self.assertEqual(self.path.stat().st_size, cursor.offset)

    def test_does_not_consume_a_half_written_line(self):
        self.path.write_text("", encoding="utf-8")
        source = FileSource(self.path)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("[2026-09-28 10:00:00] production.ERROR: SQLSTATE[23000]: Column 'ph")
        self.assertEqual(([], 0), (tailer.poll()[0], tailer.cursor.offset))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("one' cannot be null\n")
        events = tailer.poll()[0]
        self.assertEqual(1, len(events))
        self.assertIn("column 'phone' cannot be null", events[0].message)

    def test_rotation_restarts_from_the_beginning(self):
        self.path.write_text(errors(3), encoding="utf-8")
        source = FileSource(self.path)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        rotated = Path(self.temp.name) / "laravel.log.1"
        self.path.rename(rotated)
        self.path.write_text(errors(1), encoding="utf-8")
        self.assertEqual(1, len(tailer.poll()[0]))

    def test_keeps_the_legacy_cursor_key(self):
        self.assertEqual(str(self.path), FileSource(self.path).key)


class RangeHandler(http.server.BaseHTTPRequestHandler):
    content = b""
    requests = []

    def log_message(self, *args):
        pass

    def _authorized(self):
        type(self).requests.append(dict(self.headers))
        if self.headers.get("Authorization") != "Bearer log-token":
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_HEAD(self):
        if self._authorized():
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.content)))
            self.end_headers()

    def do_GET(self):
        if not self._authorized():
            return
        match = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
        start, end = int(match.group(1)), min(int(match.group(2)), len(self.content) - 1)
        body = self.content[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, len(self.content)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HttpSourceTest(unittest.TestCase):
    def setUp(self):
        RangeHandler.content = errors(2).encode()
        RangeHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:{}/logs/laravel.log?sig=secret".format(self.server.server_address[1])

    def test_tails_with_range_requests_and_bearer_token(self):
        source = HttpSource(self.url, token="log-token", poll_seconds=0)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        RangeHandler.content += errors(1, start=5).encode()
        events, cursor, _ = tailer.poll()
        self.assertEqual(1, len(events))
        self.assertEqual(len(RangeHandler.content), cursor.offset)
        self.assertNotIn("secret", source.key + source.label)
        RangeHandler.content = errors(1).encode()  # rotated: smaller than cursor
        self.assertEqual(1, len(tailer.poll()[0]))

    def test_rejects_credentials_in_url_and_reports_http_errors(self):
        with self.assertRaises(ValueError):
            HttpSource("https://user:pass@example.com/laravel.log")
        with self.assertRaises(RuntimeError):
            HttpSource(self.url, token="wrong").initial(True)

    def test_remote_sources_are_rate_limited(self):
        clock = Clock()
        source = HttpSource(self.url, token="log-token", poll_seconds=10)
        tailer = LogTailer(source, Cursor(None, 0), clock=clock)
        self.assertTrue(tailer.poll()[2])
        clock.now = 5
        self.assertFalse(tailer.poll()[2])
        clock.now = 11
        self.assertTrue(tailer.poll()[2])


class SshSourceTest(unittest.TestCase):
    """Runs the exact remote script with a local shell instead of ssh."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "laravel log.txt"  # space: path must be quoted
        self.scripts = []

    def run_locally(self, script):
        self.scripts.append(script)
        return subprocess.run(["sh", "-c", script], capture_output=True, check=True).stdout

    def test_tails_remote_file_and_detects_rotation(self):
        self.path.write_text(errors(2), encoding="utf-8")
        source = SshSource("app-server", str(self.path), user="deploy", runner=self.run_locally, poll_seconds=0)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(errors(1, start=5))
        self.assertEqual(1, len(tailer.poll()[0]))
        self.path.unlink()
        self.path.write_text(errors(2), encoding="utf-8")
        self.assertEqual(2, len(tailer.poll()[0]))
        self.assertEqual("ssh:deploy@app-server:22{}".format(self.path), source.key)

    def test_missing_file_and_input_validation(self):
        source = SshSource("app-server", str(self.path), runner=self.run_locally)
        self.assertEqual(Cursor(None, 0), source.initial(True))
        with self.assertRaises(ValueError):
            SshSource("host;rm -rf /", "/var/log/x")
        with self.assertRaises(ValueError):
            SshSource("host", "relative/path")


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key, Range=None):
        data = self.objects[Key]
        if Range:
            start, end = map(int, Range.split("=")[1].split("-"))
            data = data[start : end + 1]
        return {"Body": FakeBody(data)}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, StartAfter=""):
        keys = sorted(key for key in self.objects if key.startswith(Prefix) and key > StartAfter)[:MaxKeys]
        return {"Contents": [{"Key": key, "Size": len(self.objects[key])} for key in keys]}


class S3SourceTest(unittest.TestCase):
    def test_single_object_mode_reads_growth_with_range(self):
        client = FakeS3({"logs/laravel.log": errors(2).encode()})
        source = S3Source("bucket", key="logs/laravel.log", client=client, poll_seconds=0)
        tailer = LogTailer(source, source.initial(start_at_end=True))
        client.objects["logs/laravel.log"] += errors(1, start=5).encode()
        self.assertEqual(1, len(tailer.poll()[0]))

    def test_prefix_mode_reads_each_new_object_once_including_gzip(self):
        client = FakeS3({"app/2026/09/28/0900.log": errors(3).encode()})
        source = S3Source("bucket", prefix="app/", client=client, poll_seconds=0)
        tailer = LogTailer(source, source.initial(start_at_end=True))  # existing objects skipped
        self.assertEqual([], tailer.poll()[0])
        client.objects["app/2026/09/28/1000.log"] = errors(1).encode()
        client.objects["app/2026/09/28/1100.log.gz"] = gzip.compress(errors(2).encode())
        self.assertEqual(1, len(tailer.poll()[0]))
        self.assertEqual(2, len(tailer.poll()[0]))
        self.assertEqual([], tailer.poll()[0])
        self.assertEqual("app/2026/09/28/1100.log.gz", tailer.cursor.identity)

    def test_requires_exactly_one_of_key_or_prefix(self):
        with self.assertRaises(ValueError):
            S3Source("bucket", client=FakeS3({}))
        with self.assertRaises(ValueError):
            S3Source("bucket", key="a", prefix="b", client=FakeS3({}))


class BuildSourceTest(unittest.TestCase):
    def test_defaults_to_local_file_and_rejects_unknown_type(self):
        config = type("C", (), {"log_path": Path("/tmp/laravel.log")})()
        with patch.dict(os.environ, {"AI_FIX_LOG_SOURCE": "", "AI_FIX_LOG_PATH": ""}):
            self.assertIsInstance(build_source(config), FileSource)
        with patch.dict(os.environ, {"AI_FIX_LOG_SOURCE": "ftp"}):
            with self.assertRaises(ValueError):
                build_source(config)
        with patch.dict(os.environ, {"AI_FIX_LOG_SOURCE": "ssh", "AI_FIX_LOG_SSH_HOST": "10.0.0.5", "AI_FIX_LOG_SSH_PATH": "/var/www/app/storage/logs/laravel.log", "AI_FIX_LOG_SSH_USER": "deploy"}):
            self.assertEqual("ssh://deploy@10.0.0.5:22/var/www/app/storage/logs/laravel.log", build_source(config).label)


if __name__ == "__main__":
    unittest.main()
