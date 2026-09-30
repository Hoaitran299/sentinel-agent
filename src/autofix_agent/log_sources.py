from __future__ import annotations

import base64
import gzip
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from .log_reader import HEADER, LogEvent, parse_entry


SOURCE_TYPES = ("file", "http", "ssh", "s3")
MAX_CHUNK = 4 * 1024 * 1024  # bytes read per poll; the rest is picked up on the next poll
MAX_GZIP_OBJECT = 64 * 1024 * 1024


class LogSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cursor:
    # identity detects rotation/replacement: inode for files/ssh, object key for S3 prefixes.
    identity: Optional[str]
    offset: int


@dataclass(frozen=True)
class Chunk:
    data: bytes
    start: Cursor  # where `data` begins (reset to offset 0 after rotation)
    growing: bool  # True when the writer may still be mid-line at the end of `data`


def parse_chunk(text: str) -> List[LogEvent]:
    events: List[LogEvent] = []
    pending: List[str] = []
    for line in text.splitlines(keepends=True):
        if HEADER.match(line):
            event = parse_entry(pending)
            if event is not None:
                events.append(event)
            pending = [line]
        elif pending:
            pending.append(line)
    # Laravel writes the exception header atomically, so the last entry is emitted now
    # instead of waiting for the next log line.
    event = parse_entry(pending)
    if event is not None:
        events.append(event)
    return events


class LogSource:
    key = ""  # cursor key; never contains credentials
    label = ""  # shown in terminal/dashboard; never contains credentials
    min_interval = 0.0

    def initial(self, start_at_end: bool) -> Cursor:
        raise NotImplementedError

    def fetch(self, cursor: Cursor, limit: int) -> Chunk:
        raise NotImplementedError


class LogTailer:
    """Reads new bytes from any LogSource and turns them into Laravel log events."""

    def __init__(self, source: LogSource, cursor: Cursor, clock: Callable[[], float] = time.monotonic):
        self.source = source
        self.cursor = cursor
        self._clock = clock
        self._last_poll: Optional[float] = None

    def poll(self) -> Tuple[List[LogEvent], Cursor, bool]:
        """Return (events, cursor, polled). `polled` is False when rate-limited."""
        now = self._clock()
        if self._last_poll is not None and now - self._last_poll < self.source.min_interval:
            return [], self.cursor, False
        self._last_poll = now
        chunk = self.source.fetch(self.cursor, MAX_CHUNK)
        data = chunk.data
        if data and not data.endswith(b"\n") and (chunk.growing or len(data) >= MAX_CHUNK):
            # Do not consume a line that is still being written or was cut by the read limit;
            # it is read again, whole, on the next poll.
            cut = data.rfind(b"\n")
            if cut >= 0:
                data = data[: cut + 1]
            elif len(data) < MAX_CHUNK:
                data = b""
        self.cursor = Cursor(chunk.start.identity, chunk.start.offset + len(data))
        return parse_chunk(data.decode("utf-8", errors="replace")), self.cursor, True


# -- local file ------------------------------------------------------------------------


class FileSource(LogSource):
    def __init__(self, path: Path):
        self.path = path
        self.key = str(path)  # same key as before remote sources existed, keeps old cursors
        self.label = "file:{}".format(path)

    def initial(self, start_at_end: bool) -> Cursor:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return Cursor(None, 0)
        return Cursor(str(stat.st_ino), stat.st_size if start_at_end else 0)

    def fetch(self, cursor: Cursor, limit: int) -> Chunk:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return Chunk(b"", cursor, True)
        identity = str(stat.st_ino)
        start = cursor if cursor.identity == identity and stat.st_size >= cursor.offset else Cursor(identity, 0)
        with self.path.open("rb") as handle:
            handle.seek(start.offset)
            return Chunk(handle.read(limit), start, True)


# -- HTTP(S) Range ---------------------------------------------------------------------


class HttpSource(LogSource):
    """Tails a log file exposed over HTTP(S) with Range support (e.g. nginx `alias` +
    auth). Rotation is detected when the file becomes smaller than the cursor."""

    def __init__(self, url: str, token: str = "", username: str = "", password: str = "", poll_seconds: float = 10):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AI_FIX_LOG_HTTP_URL must be an http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("do not put credentials in AI_FIX_LOG_HTTP_URL; use AI_FIX_LOG_HTTP_TOKEN or USERNAME/PASSWORD")
        self.url = url
        public = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        self.key = "http:{}".format(public)
        self.label = public
        self.min_interval = poll_seconds
        self.headers = {"User-Agent": "demo-auto-fixbug-orchestrator/0.1"}
        if token:
            self.headers["Authorization"] = "Bearer {}".format(token)
        elif username:
            basic = base64.b64encode("{}:{}".format(username, password).encode("utf-8")).decode("ascii")
            self.headers["Authorization"] = "Basic {}".format(basic)

    def _request(self, method: str, headers: Optional[dict] = None) -> Tuple[int, dict, bytes]:
        request = urllib.request.Request(self.url, method=method, headers=dict(self.headers, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read() if method == "GET" else b""
        except urllib.error.HTTPError as error:
            if error.code == 416:  # range not satisfiable: nothing new
                return 416, dict(error.headers), b""
            raise LogSourceError("log HTTP source returned {}".format(error.code)) from None
        except urllib.error.URLError as error:
            raise LogSourceError("log HTTP source unreachable: {}".format(error.reason)) from None

    def _size(self) -> int:
        status, headers, _ = self._request("HEAD")
        length = headers.get("Content-Length")
        if status == 200 and length is not None:
            return int(length)
        status, headers, _ = self._request("GET", {"Range": "bytes=0-0"})
        match = re.search(r"/(\d+)$", headers.get("Content-Range", ""))
        if match is None:
            raise LogSourceError("log HTTP source did not report its size")
        return int(match.group(1))

    def initial(self, start_at_end: bool) -> Cursor:
        return Cursor(None, self._size() if start_at_end else 0)

    def fetch(self, cursor: Cursor, limit: int) -> Chunk:
        size = self._size()
        start = cursor if size >= cursor.offset else Cursor(None, 0)
        if size == start.offset:
            return Chunk(b"", start, True)
        end = min(size, start.offset + limit) - 1
        status, _, body = self._request("GET", {"Range": "bytes={}-{}".format(start.offset, end)})
        if status == 200:  # server ignored Range
            body = body[start.offset : end + 1]
        elif status != 206:
            return Chunk(b"", start, True)
        return Chunk(body, start, True)


# -- SSH -------------------------------------------------------------------------------


Runner = Callable[[str], bytes]


class SshSource(LogSource):
    """Tails a file on another server with one `ssh` call per poll. Uses the runner user's
    SSH key/agent and known_hosts (BatchMode: never prompts)."""

    def __init__(self, host: str, path: str, user: str = "", port: int = 22, key_file: str = "", poll_seconds: float = 10, runner: Optional[Runner] = None):
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host or ""):
            raise ValueError("AI_FIX_LOG_SSH_HOST must be a hostname or IP")
        if user and not re.fullmatch(r"[A-Za-z0-9._-]+", user):
            raise ValueError("AI_FIX_LOG_SSH_USER contains unsupported characters")
        if not path.startswith("/"):
            raise ValueError("AI_FIX_LOG_SSH_PATH must be an absolute path")
        self.host, self.user, self.port, self.path, self.key_file = host, user, int(port), path, key_file
        target = "{}@{}".format(user, host) if user else host
        self.key = "ssh:{}:{}{}".format(target, self.port, path)
        self.label = "ssh://{}:{}{}".format(target, self.port, path)
        self.min_interval = poll_seconds
        self._runner = runner or self._ssh

    def _ssh(self, script: str) -> bytes:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(self.port)]
        if self.key_file:
            command += ["-i", self.key_file, "-o", "IdentitiesOnly=yes"]
        command += ["{}@{}".format(self.user, self.host) if self.user else self.host, script]
        environment = {key: os.environ[key] for key in ("HOME", "PATH", "USER", "LANG", "SSH_AUTH_SOCK") if key in os.environ}
        result = subprocess.run(command, capture_output=True, timeout=60, env=environment)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            raise LogSourceError("ssh log source failed ({}): {}".format(result.returncode, (detail[-1] if detail else "")[:200]))
        return result.stdout

    def _script(self, identity: str, offset: int, limit: int) -> str:
        # Prints "<inode> <start-offset>" then the bytes from that offset (GNU or BSD stat).
        return (
            "P={path}; if [ ! -f \"$P\" ]; then echo MISSING; exit 0; fi; "
            "S=$(stat -c '%i %s' \"$P\" 2>/dev/null || stat -f '%i %z' \"$P\"); set -- $S; "
            "OFF={offset}; if [ \"$1\" != \"{identity}\" ] || [ \"$2\" -lt \"$OFF\" ]; then OFF=0; fi; "
            "echo \"$1 $OFF $2\"; if [ {limit} -gt 0 ]; then tail -c +$((OFF+1)) \"$P\" | head -c {limit}; fi"
        ).format(path=shlex.quote(self.path), offset=int(offset), identity=identity, limit=int(limit))

    def _run(self, identity: Optional[str], offset: int, limit: int) -> Tuple[Optional[Cursor], int, bytes]:
        if identity is not None and not identity.isdigit():
            identity = None
        output = self._runner(self._script(identity or "", offset, limit))
        header, _, data = output.partition(b"\n")
        if header.strip() == b"MISSING":
            return None, 0, b""
        inode, start, size = header.decode("ascii").split()
        return Cursor(inode, int(start)), int(size), data

    def initial(self, start_at_end: bool) -> Cursor:
        cursor, size, _ = self._run(None, 0, 0)
        if cursor is None:
            return Cursor(None, 0)
        return Cursor(cursor.identity, size if start_at_end else 0)

    def fetch(self, cursor: Cursor, limit: int) -> Chunk:
        start, _, data = self._run(cursor.identity, cursor.offset, limit)
        return Chunk(data, start or cursor, True)


# -- S3 / Cloudflare R2 / MinIO --------------------------------------------------------


class S3Source(LogSource):
    """Reads Laravel logs from S3-compatible storage.

    key mode:    one object that is periodically re-uploaded (e.g. synced laravel.log);
                 read with Range from the cursor, reset when the object shrinks.
    prefix mode: a log shipper (Vector, Fluent Bit, cron upload...) writes new objects under
                 a prefix with lexicographically increasing keys (date/time based); each
                 object is read once, `.gz` objects are decompressed.
    """

    def __init__(self, bucket: str, key: str = "", prefix: str = "", client: Any = None, poll_seconds: float = 30, label_endpoint: str = ""):
        if not bucket:
            raise ValueError("AI_FIX_LOG_S3_BUCKET is required")
        if bool(key) == bool(prefix):
            raise ValueError("set exactly one of AI_FIX_LOG_S3_KEY or AI_FIX_LOG_S3_PREFIX")
        self.bucket, self.object_key, self.prefix = bucket, key, prefix
        self.client = client
        self.key = "s3:{}/{}".format(bucket, key or prefix)
        self.label = "s3://{}/{}{}".format(bucket, key or prefix, " ({})".format(label_endpoint) if label_endpoint else "")
        self.min_interval = poll_seconds

    def _call(self, method: str, **arguments: Any) -> Any:
        try:
            return getattr(self.client, method)(Bucket=self.bucket, **arguments)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code", error.__class__.__name__)
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise LogSourceError("S3 {} failed: {}".format(method, code)) from None

    def _size(self, key: str) -> Optional[int]:
        head = self._call("head_object", Key=key)
        return None if head is None else int(head["ContentLength"])

    def _range(self, key: str, start: int, limit: int) -> bytes:
        response = self._call("get_object", Key=key, Range="bytes={}-{}".format(start, start + limit - 1))
        return b"" if response is None else response["Body"].read()

    def _keys_after(self, after: str, max_keys: int = 100) -> List[Tuple[str, int]]:
        arguments = {"Prefix": self.prefix, "MaxKeys": max_keys}
        if after:
            arguments["StartAfter"] = after
        page = self._call("list_objects_v2", **arguments) or {}
        return [(item["Key"], int(item["Size"])) for item in page.get("Contents", []) if not item["Key"].endswith("/")]

    def initial(self, start_at_end: bool) -> Cursor:
        if self.object_key:
            return Cursor(None, (self._size(self.object_key) or 0) if start_at_end else 0)
        if not start_at_end:
            return Cursor(None, 0)
        last: Optional[Tuple[str, int]] = None
        after = ""
        while True:
            page = self._keys_after(after, 1000)
            if not page:
                break
            last = page[-1]
            after = last[0]
        # Mark existing objects as already read; only newer objects are processed.
        return Cursor(last[0], last[1] if not last[0].endswith(".gz") else 1) if last else Cursor(None, 0)

    def fetch(self, cursor: Cursor, limit: int) -> Chunk:
        if self.object_key:
            size = self._size(self.object_key)
            if size is None:
                return Chunk(b"", cursor, False)
            start = cursor if size >= cursor.offset else Cursor(None, 0)
            if size == start.offset:
                return Chunk(b"", start, False)
            return Chunk(self._range(self.object_key, start.offset, limit), start, False)

        current = cursor.identity
        if current and not current.endswith(".gz"):
            size = self._size(current)
            if size is not None and size > cursor.offset:
                return Chunk(self._range(current, cursor.offset, limit), cursor, False)
        following = self._keys_after(current or "", 1)
        if not following:
            return Chunk(b"", cursor, False)
        key, size = following[0]
        start = Cursor(key, 0)
        if key.endswith(".gz"):
            if size > MAX_GZIP_OBJECT:
                raise LogSourceError("compressed log object too large: {}".format(key))
            response = self._call("get_object", Key=key)
            data = gzip.decompress(response["Body"].read()) if response else b""
            return Chunk(data or b"\n", start, False)  # non-empty so the cursor moves past it
        return Chunk(self._range(key, 0, limit) if size else b"", start, False)


def s3_client(endpoint: str, region: str, access_key: str, secret_key: str) -> Any:
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError("S3/R2 log source requires boto3; run `make install-s3`") from error
    options = {"region_name": region or "auto"}
    if endpoint:
        options["endpoint_url"] = endpoint
    if access_key:
        options.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    return boto3.client("s3", **options)


def build_source(config: Any) -> LogSource:
    """Create the configured source from `.env` (AI_FIX_LOG_SOURCE=file|http|ssh|s3)."""
    kind = (os.environ.get("AI_FIX_LOG_SOURCE") or "file").strip().lower()
    poll = float(os.environ.get("AI_FIX_LOG_POLL_SECONDS") or 10)
    env = lambda name, default="": os.environ.get(name, default).strip()  # noqa: E731
    if kind == "file":
        path = env("AI_FIX_LOG_PATH")
        return FileSource(Path(path).expanduser().resolve() if path else config.log_path)
    if kind == "http":
        return HttpSource(
            env("AI_FIX_LOG_HTTP_URL"),
            token=env("AI_FIX_LOG_HTTP_TOKEN"),
            username=env("AI_FIX_LOG_HTTP_USERNAME"),
            password=os.environ.get("AI_FIX_LOG_HTTP_PASSWORD", ""),
            poll_seconds=poll,
        )
    if kind == "ssh":
        return SshSource(
            env("AI_FIX_LOG_SSH_HOST"),
            env("AI_FIX_LOG_SSH_PATH"),
            user=env("AI_FIX_LOG_SSH_USER"),
            port=int(env("AI_FIX_LOG_SSH_PORT", "22") or 22),
            key_file=str(Path(env("AI_FIX_LOG_SSH_KEY")).expanduser()) if env("AI_FIX_LOG_SSH_KEY") else "",
            poll_seconds=poll,
        )
    if kind == "s3":
        endpoint = env("AI_FIX_LOG_S3_ENDPOINT")
        return S3Source(
            env("AI_FIX_LOG_S3_BUCKET"),
            key=env("AI_FIX_LOG_S3_KEY"),
            prefix=env("AI_FIX_LOG_S3_PREFIX"),
            client=s3_client(
                endpoint,
                env("AI_FIX_LOG_S3_REGION"),
                env("AI_FIX_LOG_S3_ACCESS_KEY_ID"),
                os.environ.get("AI_FIX_LOG_S3_SECRET_ACCESS_KEY", ""),
            ),
            poll_seconds=max(poll, 10),
            label_endpoint=(urlparse(endpoint).hostname or "") if endpoint else "",
        )
    raise ValueError("AI_FIX_LOG_SOURCE must be one of: {}".format(", ".join(SOURCE_TYPES)))
