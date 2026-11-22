"""A static HTTP server with byte ranges and optional dropped connections.

    python range_server.py DIR [--port 0] [--drop-after BYTES] [--delay SECONDS]
        [--stall-after BYTES] [--outage SECONDS] [--flaky NAME ...] [--ignore-ranges]

Prints the port it listens on, then serves DIR. With --drop-after, the first response to
each distinct Range closes the connection after that many bytes, the way a flaky network
does, so installers must resume. The other faults are the ways a bad connection breaks a
download:

  --stall-after  the first response to each distinct Range sends that many bytes, then
                 holds the socket open and silent (a dead Wi-Fi link, a wedged proxy);
  --outage       for that many seconds from the first body range, every request is cut
                 off before a response (the network is down, then comes back);
  --flaky        the first GET of each NAME answers 503 (a CDN hiccup on a small file);
  --ignore-ranges  every body range is answered 200 with the whole file, as some proxies
                 do; the 1-byte size probe still gets its 206.
 Requests go to DIR/requests.log, one per line, and
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


def make_handler(root: Path, drop_after: int | None, delay: float = 0.0, *, stall_after: int | None = None,
                 outage: float = 0.0, flaky: tuple[str, ...] = (), ignore_ranges: bool = False,
                 stop: threading.Event | None = None):
    dropped: set[str] = set()
    stalled: set[str] = set()
    failed: set[str] = set()
    outage_from: list[float] = []
    lock = threading.Lock()
    stop = stop or threading.Event()

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
            body_range = bool(rng) and rng != "bytes=0-0"
            with lock:
                if outage and body_range and not outage_from:
                    outage_from.append(time.time())
                down = bool(outage_from) and time.time() < outage_from[0] + outage
                flake = self.command == "GET" and path.name in flaky and path.name not in failed
                if flake:
                    failed.add(path.name)
            if down:
                self.close_connection = True
                self.connection.shutdown(2)
                return
            if flake:
                self.send_error(503)
                return
            if not path.is_file() or path.name in ("requests.log", "timing.log"):
                self.send_error(404)
                return
            total = path.stat().st_size
            start, end, status = 0, total - 1, 200
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
            if m and not (ignore_ranges and body_range):
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
                stall = stall_after is not None and len(chunk) > stall_after and key not in stalled
                if stall:
                    stalled.add(key)
            if stall:
                self.wfile.write(chunk[:stall_after])
                self.wfile.flush()
                stop.wait(300)  # silent until the client gives up or the server shuts down
                self.close_connection = True
                return
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


class StoppableServer(Server):
    """shutdown() also releases responses parked by --stall-after."""

    stop: threading.Event

    def shutdown(self):
        self.stop.set()
        super().shutdown()


def serve(root: Path, port: int = 0, drop_after: int | None = None, delay: float = 0.0,
          **faults) -> http.server.ThreadingHTTPServer:
    stop = threading.Event()
    server = StoppableServer(("127.0.0.1", port), make_handler(root, drop_after, delay, stop=stop, **faults))
    server.stop = stop
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--drop-after", type=int)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--stall-after", type=int)
    ap.add_argument("--outage", type=float, default=0.0)
    ap.add_argument("--flaky", nargs="*", default=[])
    ap.add_argument("--ignore-ranges", action="store_true")
    args = ap.parse_args()
    server = serve(args.root.resolve(), args.port, args.drop_after, args.delay, stall_after=args.stall_after,
                   outage=args.outage, flaky=tuple(args.flaky), ignore_ranges=args.ignore_ranges)
    print(server.server_address[1], flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
