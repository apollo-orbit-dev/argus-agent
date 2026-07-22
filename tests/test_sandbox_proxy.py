"""The proxy is the sandbox's only route out, so its parsing is a security boundary: a request line
it mis-parses is a host it fails to check. These tests are all pure-function or loopback — no
container needed."""
import socket
import threading
import urllib.error
import urllib.request

import pytest

from engine.sandbox.proxy import (parse_absolute_request, parse_connect_target, serve_forever_in_thread)


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
               "urllib", "__future__", "egress_policy", "engine"}
    assert mods <= allowed, f"non-stdlib import: {mods - allowed}"
