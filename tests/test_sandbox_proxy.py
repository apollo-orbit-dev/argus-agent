"""The proxy is the sandbox's only route out, so its parsing is a security boundary: a request line
it mis-parses is a host it fails to check. These tests are all pure-function or loopback — no
container needed."""
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from engine.sandbox.proxy import (_http_denial, parse_absolute_request, parse_connect_target,
                                   serve_forever_in_thread)


@pytest.mark.parametrize("line,expected", [
    ("CONNECT example.com:443 HTTP/1.1", ("example.com", 443)),
    ("CONNECT example.com:8443 HTTP/1.1", ("example.com", 8443)),
    ("CONNECT 1.1.1.1:443 HTTP/1.1", ("1.1.1.1", 443)),
    ("connect example.com:443 http/1.1", ("example.com", 443)),
])
def test_parse_connect(line, expected):
    assert parse_connect_target(line) == expected


@pytest.mark.parametrize("line", [
    "CONNECT example.com HTTP/1.1",          # no port
    "CONNECT :443 HTTP/1.1",                 # no host
    "CONNECT example.com:notaport HTTP/1.1",
    "GET / HTTP/1.1",
    "", "   ", "CONNECT",
])
def test_parse_connect_rejects_malformed(line):
    assert parse_connect_target(line) is None


@pytest.mark.parametrize("line,expected", [
    ("GET http://example.com/a HTTP/1.1", ("example.com", 80)),
    ("POST http://example.com:8080/a HTTP/1.1", ("example.com", 8080)),
    ("GET https://example.com/a HTTP/1.1", ("example.com", 443)),
])
def test_parse_absolute_request(line, expected):
    assert parse_absolute_request(line) == expected


@pytest.mark.parametrize("line", ["GET /relative HTTP/1.1", "GET file:///etc/passwd HTTP/1.1", ""])
def test_parse_absolute_rejects_non_absolute_or_non_http(line):
    assert parse_absolute_request(line) is None


@pytest.fixture
def proxy():
    port = _free_port()
    stop = serve_forever_in_thread(port)
    yield port
    stop()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _connect_via(port, target):
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    resp = s.recv(200).decode(errors="replace")
    s.close()
    return resp


def test_connect_to_a_private_address_is_refused(proxy):
    """The whole point: a container behind this proxy must not reach the LAN."""
    resp = _connect_via(proxy, "192.168.0.93:8000")
    assert "403" in resp


def test_connect_to_loopback_is_refused(proxy):
    assert "403" in _connect_via(proxy, "127.0.0.1:8700")


def test_connect_to_cloud_metadata_is_refused(proxy):
    assert "403" in _connect_via(proxy, "169.254.169.254:80")


def test_malformed_request_is_refused_not_crashed(proxy):
    s = socket.create_connection(("127.0.0.1", proxy), 5)
    s.sendall(b"GARBAGE\r\n\r\n")
    resp = s.recv(200).decode(errors="replace")
    s.close()
    assert "400" in resp


def test_the_proxy_survives_a_refused_request(proxy):
    """One bad request must not take the sidecar down — it is shared by every workspace."""
    _connect_via(proxy, "127.0.0.1:1")
    assert "403" in _connect_via(proxy, "10.0.0.1:80")


def test_module_imports_only_stdlib():
    import ast
    import pathlib

    src = pathlib.Path("engine/sandbox/proxy.py").read_text()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    allowed = {"argparse", "logging", "select", "socket", "socketserver", "sys", "threading",
               "time", "urllib", "__future__", "egress_policy", "engine"}
    assert mods <= allowed, f"non-stdlib import: {mods - allowed}"


def _upstream_listener():
    """A throwaway local TCP 'upstream' standing in for the TLS server behind the tunnel — loopback
    only, no DNS, no real network."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    return srv, srv.getsockname()[1]


def test_split_request_line_and_headers_are_not_smuggled_into_the_tunnel(proxy, monkeypatch):
    """Forces exactly the bug the reviewer reproduced: the CONNECT line arrives in its own TCP
    segment (its own sendall()), and the headers follow in a second one after a short sleep. Before
    the fix, the header bytes were left unread on the raw socket and `_relay` forwarded them into
    the tunnel ahead of the client's real payload — upstream would see header text prepended to the
    payload. After the fix, upstream must see the client's payload and nothing else."""
    monkeypatch.setattr("engine.sandbox.proxy.host_allowed", lambda host, port: (True, ""))

    up_srv, up_port = _upstream_listener()
    received = {}

    def accept_and_read():
        conn, _ = up_srv.accept()
        conn.settimeout(5)
        data = b""
        try:
            while len(data) < len(b"CLIENT-PAYLOAD-NOT-HEADERS"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        received["data"] = data
        conn.close()

    t = threading.Thread(target=accept_and_read, daemon=True)
    t.start()

    target = f"127.0.0.1:{up_port}"
    c = socket.create_connection(("127.0.0.1", proxy), 5)
    try:
        c.sendall(f"CONNECT {target} HTTP/1.1\r\n".encode())   # request line alone, own segment
        time.sleep(0.3)
        c.sendall(f"Host: {target}\r\n\r\n".encode())          # headers, separate segment

        resp = c.recv(200)
        assert b"200" in resp

        payload = b"CLIENT-PAYLOAD-NOT-HEADERS"
        c.sendall(payload)

        t.join(5)
        assert not t.is_alive()
        assert received.get("data", b"").startswith(payload), (
            f"upstream received {received.get('data', b'')!r} — header bytes were smuggled "
            f"ahead of the client's payload")
    finally:
        c.close()
        up_srv.close()


def test_header_drain_is_bounded_and_denies_a_client_that_never_sends_the_blank_line(proxy, monkeypatch):
    """A client that sends a request line and then dribbles bytes without ever reaching the
    terminating blank line must be denied (and this thread must return), not hang forever. The
    drain's deadline is shortened here purely so the test doesn't have to wait out the production
    value — the bound itself, not its size, is what's under test."""
    monkeypatch.setattr("engine.sandbox.proxy._HEADER_DRAIN_TIMEOUT", 0.5)
    c = socket.create_connection(("127.0.0.1", proxy), 5)
    try:
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n")
        c.sendall(b"X-Never-Ending: ")   # no CRLF CRLF ever follows
        c.settimeout(5)
        resp = c.recv(300)
        assert b"400" in resp
    finally:
        c.close()


def test_deny_status_lines_use_the_correct_reason_phrase():
    """`_deny` used to hardcode 'Forbidden' for every status code. Each denial code must carry its
    own correct HTTP reason phrase."""
    cases = {
        400: b"HTTP/1.1 400 Bad Request\r\n",
        403: b"HTTP/1.1 403 Forbidden\r\n",
        501: b"HTTP/1.1 501 Not Implemented\r\n",
        502: b"HTTP/1.1 502 Bad Gateway\r\n",
        503: b"HTTP/1.1 503 Service Unavailable\r\n",
    }
    for code, expected_status_line in cases.items():
        assert _http_denial(code, "because").startswith(expected_status_line)


def test_plain_http_denial_uses_the_correct_reason_phrase(proxy, monkeypatch):
    """End-to-end check that the 501 path (plain-HTTP proxying is deliberately unsupported) carries
    its own reason phrase rather than 'Forbidden'."""
    monkeypatch.setattr("engine.sandbox.proxy.host_allowed", lambda host, port: (True, ""))
    c = socket.create_connection(("127.0.0.1", proxy), 5)
    try:
        c.sendall(b"GET http://example.invalid/ HTTP/1.1\r\nHost: example.invalid\r\n\r\n")
        resp = c.recv(300).decode(errors="replace")
        assert "501 Not Implemented" in resp
    finally:
        c.close()


def test_refuses_past_the_concurrent_connection_cap(monkeypatch):
    """The sidecar is shared by every workspace; a client opening connections without limit must be
    refused (503) once the cap is hit, rather than accepted unboundedly. Uses a small explicit cap
    so the test doesn't need to open the production-sized number of connections."""
    monkeypatch.setattr("engine.sandbox.proxy.host_allowed", lambda host, port: (True, ""))
    port = _free_port()
    stop = serve_forever_in_thread(port, max_connections=2)
    up_srv, up_port = _upstream_listener()
    accepted = []

    def accept_two():
        for _ in range(2):
            conn, _ = up_srv.accept()
            accepted.append(conn)

    t = threading.Thread(target=accept_two, daemon=True)
    t.start()

    clients = []
    try:
        for _ in range(2):
            c = socket.create_connection(("127.0.0.1", port), 5)
            c.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            resp = c.recv(200)
            assert b"200" in resp
            clients.append(c)
        t.join(5)
        assert len(accepted) == 2, "both tunnels should have reached the throwaway upstream"

        extra = socket.create_connection(("127.0.0.1", port), 5)
        try:
            resp = extra.recv(300).decode(errors="replace")
            assert "503" in resp
        finally:
            extra.close()
    finally:
        for c in clients:
            c.close()
        for conn in accepted:
            conn.close()
        up_srv.close()
        stop()
