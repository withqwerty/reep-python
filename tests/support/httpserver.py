"""A threaded HTTP server for tests, with range requests and fault injection."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


@dataclass
class Faults:
    """Faults to inject, by URL path."""

    statuses: dict[str, list[int]] = field(default_factory=dict)
    truncate: dict[str, list[int]] = field(default_factory=dict)
    headers: dict[str, dict[str, str]] = field(default_factory=dict)


class FileServer:
    """Serve files from a directory on a free local port.

    Attributes:
        root: The directory to serve.
        requests: How many GET requests each path received.
        ranges: The ``Range`` header of each request, by path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: Counter[str] = Counter()
        self.ranges: dict[str, list[str | None]] = {}
        self.faults = Faults()
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                server._handle(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """Return the base URL."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> FileServer:
        """Start serving."""
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving."""
        self._httpd.shutdown()
        self._httpd.server_close()

    def fail(self, path: str, *statuses: int) -> None:
        """Answer the next requests for ``path`` with these HTTP statuses."""
        self.faults.statuses.setdefault(path, []).extend(statuses)

    def truncate(self, path: str, nbytes: int) -> None:
        """Send only ``nbytes`` of the body on the next request for ``path``."""
        self.faults.truncate.setdefault(path, []).append(nbytes)

    def reset(self) -> None:
        """Clear the counters and faults."""
        with self._lock:
            self.requests.clear()
            self.ranges.clear()
            self.faults = Faults()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0]
        with self._lock:
            self.requests[path] += 1
            self.ranges.setdefault(path, []).append(handler.headers.get("Range"))
            statuses = self.faults.statuses.get(path)
            status = statuses.pop(0) if statuses else None
            cuts = self.faults.truncate.get(path)
            cut = cuts.pop(0) if cuts else None
            extra_headers = dict(self.faults.headers.get(path, {}))
        if status is not None:
            handler.send_response(status)
            for key, value in extra_headers.items():
                handler.send_header(key, value)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        target = (self.root / path.lstrip("/")).resolve()
        if not target.is_file() or self.root.resolve() not in target.parents:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        body = target.read_bytes()
        start = 0
        code = 200
        range_header = handler.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            if start >= len(body):
                handler.send_response(416)
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            code = 206
        payload = body[start:]
        handler.send_response(code)
        handler.send_header("Content-Length", str(len(payload)))
        if code == 206:
            handler.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
        for key, value in extra_headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        if cut is not None:
            handler.wfile.write(payload[:cut])
            handler.wfile.flush()
            handler.close_connection = True
            return
        handler.wfile.write(payload)
