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
import time
from urllib.parse import urlparse

try:                                    # in-image layout: /opt/argus/{proxy,egress_policy}.py
    from egress_policy import resolve_allowed
except ImportError:                     # repo layout, for the tests
    from engine.sandbox.egress_policy import resolve_allowed

log = logging.getLogger("argus.sandbox.proxy")

_RELAY_CHUNK = 65536
_CONNECT_TIMEOUT = 15.0

_MAX_HEADER_BYTES = 16 * 1024    # 16 KiB — a real CONNECT request has one or two header lines;
                                  # this is generous slack while still bounding memory for a client
                                  # that tries to feed the header-drain loop endless header lines.
_HEADER_DRAIN_TIMEOUT = 5.0      # seconds, wall-clock. Real clients send their headers as part of
                                  # the same connection setup as the request line; this is not a
                                  # network RTT budget — it exists so a client that dribbles single
                                  # bytes just under any per-read socket timeout still cannot pin
                                  # this thread indefinitely.

# This sidecar is shared by every workspace on the host, so one client opening connections without
# limit could exhaust threads/fds before the per-connection 60s idle timeout ever fires. Cap it and
# refuse politely past the cap rather than accepting unboundedly.
_MAX_CONCURRENT_CONNECTIONS = 128
_LISTEN_BACKLOG = 256            # stdlib TCPServer default is 5; under a connection burst that
                                  # drops SYNs silently at the kernel — raise it so bursts are seen
                                  # and answered (with a 503, once past _MAX_CONCURRENT_CONNECTIONS)
                                  # at the application layer instead.

_REASON_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def _http_denial(code: int, reason: str) -> bytes:
    """A minimal, compliant HTTP response denying the request, with the status line's reason
    phrase matching `code` (it used to hardcode "Forbidden" for everything, including 502s and
    501s). Shared by the per-connection `ProxyHandler._deny` (which has a buffered `self.wfile`)
    and `_Server.verify_request` (which only has the raw accepted socket, because the concurrency
    cap is enforced before a handler thread — and its rfile/wfile — exist)."""
    body = f"argus egress proxy: {reason}\n".encode()
    phrase = _REASON_PHRASES.get(code, "Error")
    return (f"HTTP/1.1 {code} {phrase}\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n").encode() + body


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


def _connect_to_vetted(addrs: list, timeout: float) -> socket.socket:
    """Connect to one of the addresses `resolve_allowed` already vetted — never re-resolve the
    hostname. Mirrors `socket.create_connection`'s own try-each-address-in-order fallback, but over
    a fixed, pre-vetted address list instead of a hostname, which is exactly what closes the DNS-
    rebind TOCTOU: the address connected to is provably the address that was checked, because it is
    the very same tuple, not a fresh lookup that could have changed in between."""
    last_err: "OSError | None" = None
    for family, socktype, proto, _canonname, sockaddr in addrs:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last_err = e
            sock.close()
    raise last_err or OSError("no vetted addresses to connect to")


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
        # Drain the request's trailing headers before doing anything else with this connection.
        # `_relay` later reads the client side from the raw socket, not the buffered `rfile` — any
        # header bytes left unread here would still be sitting on the socket and would be forwarded
        # into the tunnel ahead of the client's actual payload (a TLS ClientHello). This has to
        # happen before the tunnel opens, not merely before the policy check.
        if not self._drain_headers():
            self._deny(400, "malformed or incomplete request headers")
            return
        host, port = target
        # `addrs` is the SAME lookup that was just vetted — never re-resolved. Re-resolving here
        # (e.g. `socket.create_connection((host, port))` down in `_tunnel`) would be a DNS-rebind
        # TOCTOU: the model controls `host`, this proxy is the only boundary for sandboxed egress,
        # and a second lookup between the check and the connect can return a different — unvetted —
        # address (a short-TTL record flipped mid-request, or an attacker-controlled authoritative
        # server racing the two lookups on purpose).
        ok, reason, addrs = resolve_allowed(host, port)
        if not ok:
            log.warning("egress DENIED %s:%s — %s", host, port, reason)
            self._deny(403, reason)
            return
        if parse_connect_target(line):
            self._tunnel(host, port, addrs)
        else:
            self._deny(501, "plain-HTTP proxying is not supported; use HTTPS (CONNECT)")

    def _drain_headers(self) -> bool:
        """Consume header lines up to the terminating blank line (or EOF), so nothing the client
        already sent is left in `rfile`'s buffer when `_relay` switches to reading the raw socket —
        that hand-off is exactly where an unconsumed header tail becomes a smuggled prefix on the
        tunnelled connection. Bounded two ways: a wall-clock deadline (a client that trickles bytes
        one at a time, staying just under any single read's timeout, must not pin this thread
        indefinitely) and a byte cap (a client sending endless header lines must not grow memory
        without bound). Returns False — caller must deny and stop, never proceed — if either bound
        is hit or the client disconnects mid-headers.
        """
        deadline = time.monotonic() + _HEADER_DRAIN_TIMEOUT
        total = 0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                budget = _MAX_HEADER_BYTES - total
                if budget <= 0:
                    return False
                self.connection.settimeout(remaining)
                line = self.rfile.readline(budget)
                if not line:
                    return False                      # EOF before the terminating blank line
                total += len(line)
                if line in (b"\r\n", b"\n"):
                    return True
        except OSError:
            return False
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _deny(self, code: int, reason: str) -> None:
        try:
            self.wfile.write(_http_denial(code, reason))
        except OSError:
            pass

    def _tunnel(self, host: str, port: int, addrs: list) -> None:
        try:
            upstream = _connect_to_vetted(addrs, _CONNECT_TIMEOUT)
        except OSError as e:
            self._deny(502, f"could not reach {host}:{port} ({type(e).__name__})")
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            self._flush_buffered_client_bytes(upstream)
            _relay(self.connection, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _flush_buffered_client_bytes(self, upstream: socket.socket) -> None:
        """`_relay` reads the client side from the raw socket, not `rfile` — but `rfile` is
        buffered, so if any client bytes were already read off the wire into that buffer (e.g. a
        TLS ClientHello arriving in the same TCP segment as the request headers), the raw socket
        will never see them again: they'd be silently dropped rather than smuggled, which is
        quieter but still a real corruption of the tunnel. Forward whatever is already sitting in
        the buffer before the raw-socket relay takes over. `peek()` returns only what is already
        buffered without issuing a fresh socket read when the buffer is non-empty, so this cannot
        introduce a second, competing read of the raw socket.
        """
        try:
            leftover = self.rfile.peek()
        except OSError:
            return
        if not leftover:
            return
        try:
            self.rfile.read(len(leftover))   # consume from the buffer; already forwarded below
            upstream.sendall(leftover)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = _LISTEN_BACKLOG

    def __init__(self, server_address, handler_cls,
                 max_connections: int = _MAX_CONCURRENT_CONNECTIONS):
        super().__init__(server_address, handler_cls)
        self._slots = threading.Semaphore(max_connections)

    def verify_request(self, request, client_address) -> bool:
        """Runs synchronously in the accept loop, before ThreadingMixIn spawns a handler thread —
        so a non-blocking acquire here bounds thread count directly, rather than merely bounding it
        after the fact. Refuse politely (503) past the cap instead of accepting unboundedly."""
        if self._slots.acquire(blocking=False):
            return True
        try:
            request.sendall(_http_denial(503, "too many concurrent connections through this proxy"))
        except OSError:
            pass
        return False

    def finish_request(self, request, client_address) -> None:
        """Only reached when verify_request acquired a slot, so this is always paired with exactly
        one acquire — release unconditionally, even if the handler raises."""
        try:
            super().finish_request(request, client_address)
        finally:
            self._slots.release()


def serve_forever_in_thread(port: int, max_connections: int = _MAX_CONCURRENT_CONNECTIONS):
    """Start the proxy on 127.0.0.1:port in a background thread. Returns a callable that stops it.
    Used by the tests; the container entrypoint uses serve()."""
    srv = _Server(("127.0.0.1", port), ProxyHandler, max_connections=max_connections)
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
