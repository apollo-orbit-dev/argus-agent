"""The egress policy: may this host be reached?

ONE definition, three enforcement points — the host-side guard created tools run under
(tool_creation._SafeHTTPX), the host-side guard download_file/watches run under (net_guard), and
the in-container proxy that is the sandbox's only route out.

STDLIB ONLY, and no imports from the rest of Argus: this file is COPYed into the sandbox image,
which has no third-party packages and no Argus package. tests/test_egress_policy.py enforces that
mechanically.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Names that must never resolve anywhere useful, independent of what DNS says today.
BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})

# RFC 6598 carrier-grade NAT space: ISP-side shared address space, not the owner's LAN, but not
# publicly reachable either. Not covered by is_private/is_reserved/etc, so it needs its own check.
# Deliberately enumerated here rather than switched to ipaddress.is_global: is_global's range
# membership has shifted across Python point releases, and this policy should not change behaviour
# out from under us just because the interpreter was upgraded.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def ip_is_public(addr: str) -> bool:
    """False for anything on the local machine, the LAN, or a special-use range — and for anything
    that is not a parseable address at all, because an unparseable address cannot be shown safe."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 4 and ip in _CGNAT:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def host_allowed(host: str, port: int = 443, *, resolve: bool = True) -> "tuple[bool, str]":
    """(allowed, reason). `reason` is '' when allowed, else a short human-readable refusal.

    With resolve=True (the default) EVERY address the name resolves to must be public. That is the
    property a literal-only check cannot provide: a hostname pointing at 192.168.x.x looks innocent
    right up until it is connected. If any address is non-public the whole host is refused, so a
    split-horizon name cannot be exploited by racing which address gets used.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False, "no host"
    if host in BLOCKED_HOSTNAMES:
        return False, f"host {host!r} is blocked"
    try:                                       # an IP literal needs no DNS
        ipaddress.ip_address(host)
        return (True, "") if ip_is_public(host) else (False, f"{host} is not a public address")
    except ValueError:
        pass
    if not resolve:
        # Without resolving, a non-literal host cannot be shown safe: ipaddress rejects disguised
        # loopback forms like "2130706433", "0x7f000001", "0177.0.0.1", or "127.1" as unparseable,
        # so they'd otherwise fall through this literal-only path unresolved and unchecked. Fail
        # closed rather than claim a name is safe when resolution — the thing that actually proves
        # it — was skipped.
        return False, f"{host!r} cannot be verified without resolving (resolve=False)"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"could not resolve {host!r} ({type(e).__name__})"
    if not infos:
        return False, f"{host!r} resolved to nothing"
    bad = [i[4][0] for i in infos if not ip_is_public(i[4][0])]
    if bad:
        return False, f"{host} resolves to a non-public address ({bad[0]})"
    return True, ""


def url_allowed(url: str) -> "tuple[bool, str]":
    """http(s) only, and its host must satisfy host_allowed()."""
    try:
        p = urlparse(str(url))
        scheme = (p.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, f"scheme {scheme or '(none)'!r} is not http(s)"
        # p.port is lazily parsed from the netloc on attribute access, not by urlparse() itself, so
        # a malformed port ("99999", "abc", "-1") raises ValueError here rather than above — keep
        # it inside this try/except or it escapes this function's declared (bool, str) contract.
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError as e:
        return False, f"malformed URL ({e})"
    except Exception:
        return False, "unparseable URL"
    return host_allowed(p.hostname or "", port)
