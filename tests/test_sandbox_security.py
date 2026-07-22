"""Adversarial sandbox tests for create_tool: every known escape vector must be
blocked at compile time, and the SSRF guard must block LAN/loopback egress.
"""
import asyncio

import pytest

from engine.experimental.tool_creation import (
    ToolValidationError, _compile_run, url_is_safe, build_params_model, DynamicTool,
)

ESCAPES = [
    # filesystem / process / network modules
    "import os\ndef run(args): return os.listdir('/')",
    "import sys\ndef run(args): return str(sys.argv)",
    "import subprocess\ndef run(args): return subprocess.check_output(['id'])",
    "import socket\ndef run(args): return socket.gethostname()",
    "import shutil\ndef run(args): return shutil.rmtree('/tmp/x')",
    "import pathlib\ndef run(args): return str(pathlib.Path('/etc/passwd').read_text())",
    "import importlib\ndef run(args): return importlib.import_module('os')",
    "import ctypes\ndef run(args): return 1",
    "import multiprocessing\ndef run(args): return 1",
    "from os import system\ndef run(args): return system('id')",
    "from subprocess import run as r\ndef run(args): return 1",
    # builtins / eval / file
    "def run(args): return open('/etc/passwd').read()",
    "def run(args): return eval('1+1')",
    "def run(args): return exec('x=1')",
    "def run(args): return compile('1','','eval')",
    "def run(args): return __import__('os').system('id')",
    "def run(args): return globals()",
    "def run(args): return getattr(args, 'x')",
    "def run(args): return breakpoint()",
    # dunder-based reflection escapes
    "def run(args): return (1).__class__.__bases__[0].__subclasses__()",
    "def run(args): return type(args).__mro__",
    "def run(args): return ().__class__.__base__.__subclasses__()",
    "def run(args): return args.__globals__",
    # async contract violation
    "async def run(args): return 'x'",
]


@pytest.mark.parametrize("code", ESCAPES)
def test_escape_blocked(code):
    with pytest.raises(ToolValidationError):
        _compile_run(code, allow_network=True)


def test_safe_code_still_compiles():
    fn = _compile_run("import math\ndef run(args):\n    return str(math.sqrt(args['x']))",
                      allow_network=False)
    assert fn({"x": 16}) == "4.0"


# ---- SSRF guard ----

@pytest.mark.parametrize("url,safe", [
    ("https://api.open-meteo.com/v1/forecast", True),
    ("https://en.wikipedia.org/wiki/X", True),
    ("http://127.0.0.1:3002/v2/scrape", False),     # local Firecrawl
    ("http://localhost:9222/json", False),          # local chromium
    ("http://192.168.1.50:8000/v1/models", False),  # private-LAN vLLM
    ("http://10.0.0.5/", False),
    ("http://169.254.169.254/latest/meta-data/", False),  # cloud metadata
    ("http://[::1]:8700/", False),
    ("http://0.0.0.0:8700/", False),
])
def test_url_is_safe(url, safe, monkeypatch):
    # url_is_safe now DNS-resolves real hostnames (the fix this task ships). Every case here that
    # is expected False is caught by the blocked-hostname/private-IP-literal checks before any
    # resolution happens, so stubbing getaddrinfo to a public address cannot mask a regression —
    # it only keeps the two real-hostname cases (open-meteo, wikipedia) from hitting live DNS.
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert url_is_safe(url) is safe


def test_created_tool_cannot_reach_localhost():
    # a tool that tries to hit a LAN service gets an httpx error, not a connection
    code = ("import httpx\n"
            "def run(args):\n"
            "    try:\n"
            "        return httpx.get('http://127.0.0.1:3002/').text\n"
            "    except Exception as e:\n"
            "        return 'blocked: ' + str(e)\n")
    fn = _compile_run(code, allow_network=True)
    M = build_params_model("t", {})
    out = asyncio.run(DynamicTool("t", "x", M, fn).run(M()))
    assert "blocked" in out.lower()


def test_safe_httpx_forces_no_redirect_follow(monkeypatch):
    # a created tool can't set follow_redirects=True to chase a 3xx into the LAN
    from engine.experimental.tool_creation import _SafeHTTPX

    monkeypatch.setattr("socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])

    class _Rec:
        RequestError = RuntimeError
        def get(self, url, *a, **k):
            _Rec.seen = k
            return "ok"
    shim = _SafeHTTPX(_Rec())
    shim.get("https://api.open-meteo.com/x", follow_redirects=True)
    assert _Rec.seen["follow_redirects"] is False


def test_created_tool_cannot_use_httpx_client_bypass():
    # httpx.Client is hidden so it can't be used to bypass the get/post gate
    code = ("import httpx\n"
            "def run(args):\n"
            "    try:\n"
            "        return httpx.Client().get('http://127.0.0.1:3002/').text\n"
            "    except Exception as e:\n"
            "        return 'no-client: ' + str(e)\n")
    fn = _compile_run(code, allow_network=True)
    M = build_params_model("t", {})
    out = asyncio.run(DynamicTool("t", "x", M, fn).run(M()))
    assert "no-client" in out.lower()
