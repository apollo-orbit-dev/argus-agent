"""The sandbox's egress proxy. Runs as a sidecar container; the workspace container is on an
`--internal` podman network with no route off it, so this process is the only way out.

STDLIB ONLY. It is COPYed into the image alongside egress_policy.py and started with
`python /opt/argus/proxy.py --port 3128`. The import below tries the flat in-image layout first and
falls back to the package path so the same file is testable from the repo.

It enforces at the NETWORK layer what a language-level guard can only ask nicely for: code inside
the container cannot opt out by avoiding httpx, because there is nowhere else for a packet to go.
"""
from __future__ import annotations

import argparse
import logging
import select
import socket
import socketserver
import threading
from urllib.parse import urlparse

try:                                    # in-image layout: /opt/argus/{proxy,egress_policy}.py
    from egress_policy import host_allowed
except ImportError:                     # repo layout, for the tests
    from engine.sandbox.egress_policy import host_allowed

log = logging.getLogger("argus.sandbox.proxy")

_RELAY_CHUNK = 65536
_CONNECT_TIMEOUT = 15.0


def parse_connect_target(line: str) -> "tuple[str, int] | None":
    """'CONNECT host:port HTTP/1.1' -> (host, port). None if it is not a well-formed CONNECT.

    Anything this cannot parse must be refused, never guessed at: a mis-parsed line is a host that
    never gets policy-checked."""
    parts = (line or "").split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    host, _, port = parts[1].rpartition(":")
    if not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


def parse_absolute_request(line: str) -> "tuple[str, int] | None":
    """'GET http://host:port/path HTTP/1.1' -> (host, port). Plain-HTTP proxying uses the
    absolute-form request target; a relative target means the client did not treat us as a proxy."""
    parts = (line or "").split()
    if len(parts) < 2:
        return None
    p = urlparse(parts[1])
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    return p.hostname, (p.port or (443 if p.scheme.lower() == "https" else 80))


def _relay(a: socket.socket, b: socket.socket) -> None:
    """Pump bytes both ways until either side closes. The proxy never inspects tunnelled bytes —
    for CONNECT it cannot (that is the point of TLS), and the policy decision was already made on
    the host name before the tunnel opened."""
    socks = [a, b]
    try:
        while True:
            readable, _, errored = select.select(socks, [], socks, 60)
            if errored or not readable:
                return
            for s in readable:
                data = s.recv(_RELAY_CHUNK)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self) -> None:
        try:
            line = self.rfile.readline(8192).decode("latin-1").strip()
        except Exception:
            return
        target = parse_connect_target(line) or parse_absolute_request(line)
        if target is None:
            self._deny(400, "malformed or unsupported proxy request")
            return
        host, port = target
        ok, reason = host_allowed(host, port)
        if not ok:
            log.warning("egress DENIED %s:%s — %s", host, port, reason)
            self._deny(403, reason)
            return
        if parse_connect_target(line):
            self._tunnel(host, port)
        else:
            self._deny(501, "plain-HTTP proxying is not supported; use HTTPS (CONNECT)")

    def _deny(self, code: int, reason: str) -> None:
        body = f"argus egress proxy: {reason}\n".encode()
        try:
            self.wfile.write(f"HTTP/1.1 {code} Forbidden\r\nContent-Length: {len(body)}\r\n"
                             f"Connection: close\r\n\r\n".encode() + body)
        except OSError:
            pass

    def _tunnel(self, host: str, port: int) -> None:
        try:
            upstream = socket.create_connection((host, port), _CONNECT_TIMEOUT)
        except OSError as e:
            self._deny(502, f"could not reach {host}:{port} ({type(e).__name__})")
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            _relay(self.connection, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_forever_in_thread(port: int):
    """Start the proxy on 127.0.0.1:port in a background thread. Returns a callable that stops it.
    Used by the tests; the container entrypoint uses serve()."""
    srv = _Server(("127.0.0.1", port), ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return stop


def serve(port: int = 3128) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("argus egress proxy listening on 0.0.0.0:%s", port)
    _Server(("0.0.0.0", port), ProxyHandler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Argus sandbox egress proxy")
    ap.add_argument("--port", type=int, default=3128)
    serve(ap.parse_args().port)
