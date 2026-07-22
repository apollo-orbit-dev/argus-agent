"""One egress policy, shared by three enforcement points: the host-side guard used by created
tools, the host-side guard used by download_file/watches, and the in-container proxy.

The reason this exists: tool_creation.url_is_safe checked IP LITERALS only. Its own docstring
admitted the gap — "Naive on DNS rebinding (hostnames that resolve to private IPs are allowed)" —
so a created tool could reach the LAN by using a hostname that resolves to a private address.
net_guard.url_fetch_ok already resolved correctly. Unifying on the resolving version closes that
for every created tool, sandbox or not.
"""
import socket

import pytest

from engine.sandbox.egress_policy import (BLOCKED_HOSTNAMES, host_allowed, ip_is_public,
                                          url_allowed)


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "192.168.0.93", "172.16.0.1",
                                  "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "fe80::1"])
def test_non_public_addresses_are_rejected(addr):
    assert ip_is_public(addr) is False


@pytest.mark.parametrize("addr", ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(addr):
    assert ip_is_public(addr) is True


def test_garbage_is_rejected_not_crashed():
    assert ip_is_public("not-an-ip") is False
    assert ip_is_public("") is False


@pytest.mark.parametrize("host", sorted(BLOCKED_HOSTNAMES))
def test_blocked_hostnames(host):
    ok, reason = host_allowed(host, resolve=False)
    assert ok is False and reason


def test_private_ip_literal_rejected_without_resolving():
    ok, reason = host_allowed("192.168.0.93", 8000, resolve=False)
    assert ok is False and "private" in reason.lower() or not ok


def test_public_ip_literal_allowed():
    ok, reason = host_allowed("1.1.1.1", 80, resolve=False)
    assert ok is True and reason == ""


def test_a_hostname_resolving_to_a_private_address_is_rejected(monkeypatch):
    """THE fix. A name that points at the LAN must be refused even though the name itself
    looks innocent — this is what the old literal-only guard let through."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.168.0.93", 8000))])
    ok, reason = host_allowed("totally-innocent.example.com", 8000)
    assert ok is False and reason


def test_a_hostname_resolving_to_public_addresses_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    ok, reason = host_allowed("example.com", 443)
    assert ok is True and reason == ""


def test_mixed_resolution_is_rejected(monkeypatch):
    """If ANY resolved address is private the host is refused — a split-horizon name must not be
    usable by racing which address gets connected."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.5", 443))])
    ok, reason = host_allowed("split.example.com", 443)
    assert ok is False and reason


def test_unresolvable_host_is_rejected(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    ok, reason = host_allowed("does-not-exist.invalid", 443)
    assert ok is False and reason


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x",
                                 "", "not a url"])
def test_non_http_schemes_are_rejected(url):
    ok, reason = url_allowed(url)
    assert ok is False and reason


def test_url_allowed_uses_the_default_port_per_scheme(monkeypatch):
    seen = {}

    def fake(host, port, **k):
        seen["port"] = port
        return [(2, 1, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    url_allowed("https://example.com/a")
    assert seen["port"] == 443
    url_allowed("http://example.com/a")
    assert seen["port"] == 80


def test_module_imports_only_stdlib():
    """It is COPYed into the sandbox image, which has no third-party packages and no Argus code.
    A stray import turns the proxy into a container that will not start."""
    import ast
    import pathlib

    src = pathlib.Path("engine/sandbox/egress_policy.py").read_text()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert mods <= {"ipaddress", "socket", "urllib", "__future__"}, f"non-stdlib import: {mods}"
