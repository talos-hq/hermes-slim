"""A static HTTP server with byte ranges and optional dropped connections.

    python range_server.py DIR [--port 0] [--drop-after BYTES] [--delay SECONDS]

Prints the port it listens on, then serves DIR. With --drop-after, the first response to
each distinct Range closes the connection after that many bytes, the way a flaky network
does, so installers must resume. Requests go to DIR/requests.log, one per line, and
DIR/timing.log records `START END METHOD PATH RANGE` (epoch seconds) when each response
ends, so a test can tell whether requests overlapped. --delay holds every GET that long
before its body, so concurrent requests overlap measurably even on loopback.
"""

from __future__ import annotations

import argparse
import http.server
import re
import sys
import threading
import time
from pathlib import Path


def make_handler(root: Path, drop_after: int | None, delay: float = 0.0):
    dropped: set[str] = set()
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # quiet; requests.log is the record
            pass

        def do_HEAD(self):
            self.serve(body=False)

        def do_GET(self):
            self.serve(body=True)

        def serve(self, body: bool):
            started = time.time()
            try:
                self.respond(body)
            finally:
                with lock, (root / "timing.log").open("a") as log:
                    log.write(f"{started:.4f} {time.time():.4f} {self.command} {self.path} {self.headers.get('Range', '')}\n")

        def respond(self, body: bool):
            path = root / self.path.lstrip("/").split("?")[0]
            rng = self.headers.get("Range", "")
            with lock, (root / "requests.log").open("a") as log:
                log.write(f"{self.command} {self.path} {rng}\n")
            if not path.is_file() or path.name in ("requests.log", "timing.log"):
                self.send_error(404)
                return
            total = path.stat().st_size
            start, end, status = 0, total - 1, 200
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else end, total - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            # Read only the requested slice: release bundles are 100+ MB and ranges run in parallel.
            with path.open("rb") as f:
                f.seek(start)
                chunk = f.read(end + 1 - start)
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            if not body:
                return
            if delay:
                time.sleep(delay)  # health: allow HX013 -- simulated network latency is the behaviour under test
            key = f"{self.path} {rng}"
            with lock:
                drop = drop_after is not None and len(chunk) > drop_after and key not in dropped
                if drop:
                    dropped.add(key)
            if drop:
                self.wfile.write(chunk[:drop_after])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
                return
            self.wfile.write(chunk)

    return Handler


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Clients drop idle keep-alive sockets and the server drops ranges on purpose.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def serve(root: Path, port: int = 0, drop_after: int | None = None, delay: float = 0.0) -> http.server.ThreadingHTTPServer:
    server = Server(("127.0.0.1", port), make_handler(root, drop_after, delay))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--drop-after", type=int)
    ap.add_argument("--delay", type=float, default=0.0)
    args = ap.parse_args()
    server = serve(args.root.resolve(), args.port, args.drop_after, args.delay)
    print(server.server_address[1], flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
