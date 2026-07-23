"""EXPERIMENTAL — self-authored tools for small models.

The `create_tool` meta-tool lets the model define a new tool at runtime when it
lacks a capability. Because a small model authors the code, this is heavily
guard-railed:

  * The model must write `def run(args): ... return <string>` — a fixed contract.
  * Code is AST-scanned: no dunder access, no open/exec/eval/getattr/etc., and
    imports restricted to a safe allowlist.
  * Execution runs with a curated builtins set and a wall-clock timeout.
  * On any validation failure the tool returns a CLEAR error the model can act on
    — a repair feedback loop, the same pattern that makes tool-calling reliable.

This module is intentionally isolated from the production engine. It is exercised
by scripts/probe_toolcreation.py; wiring it into the live engine is a follow-up
decision for the user.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
from typing import Callable, Optional

log = logging.getLogger("argus.toolcreation")

from pydantic import BaseModel, Field, create_model

from engine.tools.base import Tool, ToolRegistry


def url_is_safe(url) -> bool:
    """SSRF guard for created-tool requests. Delegates to engine.sandbox.egress_policy, which
    RESOLVES the host — this function used to check IP literals only, so a hostname pointing at a
    private address (the LAN, the Argus server, cloud metadata) was allowed straight through."""
    from engine.sandbox.egress_policy import url_allowed
    return url_allowed(url)[0]


class _SafeHTTPX:
    """A restricted stand-in for the httpx module inside a created tool: gates the
    common request helpers through url_is_safe and hides Client/AsyncClient (which
    would bypass the gate). Exception/Response types pass through so tool code's
    `except httpx.HTTPError` still works."""
    _PASSTHROUGH = {"HTTPError", "RequestError", "TimeoutException", "ConnectError",
                    "ConnectTimeout", "ReadTimeout", "HTTPStatusError", "Response",
                    "codes", "URL", "Timeout", "Limits", "TransportError"}

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def _check(self, url, k):
        if not url_is_safe(url):
            raise self._real.RequestError(f"blocked internal/loopback host in URL: {url}")
        # Never auto-follow redirects: an external URL could 3xx to an internal host,
        # bypassing the per-URL check. Force it off (created tools handle 3xx themselves).
        k["follow_redirects"] = False

    def get(self, url, *a, **k): self._check(url, k); return self._real.get(url, *a, **k)
    def post(self, url, *a, **k): self._check(url, k); return self._real.post(url, *a, **k)
    def head(self, url, *a, **k): self._check(url, k); return self._real.head(url, *a, **k)
    def put(self, url, *a, **k): self._check(url, k); return self._real.put(url, *a, **k)
    def delete(self, url, *a, **k): self._check(url, k); return self._real.delete(url, *a, **k)
    def patch(self, url, *a, **k): self._check(url, k); return self._real.patch(url, *a, **k)

    def request(self, method, url, *a, **k):
        self._check(url, k); return self._real.request(method, url, *a, **k)

    def __getattr__(self, name):
        if name in self._PASSTHROUGH:
            return getattr(self._real, name)
        raise AttributeError(
            f"httpx.{name} is not available in a created tool; use httpx.get/post/etc.")

# Modules a created tool may import. Everything else (os, sys, subprocess, socket,
# pathlib, shutil, importlib, ...) is rejected.
ALLOWED_MODULES = {
    "math", "statistics", "datetime", "json", "re", "random", "time",
    "decimal", "fractions", "string", "textwrap", "collections", "itertools",
    "functools", "calendar", "zoneinfo", "urllib.parse",
}
NETWORK_MODULES = {"httpx"}

_FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "input", "__import__",
                    "getattr", "setattr", "delattr", "globals", "locals", "vars",
                    "memoryview", "breakpoint"}

SAFE_BUILTINS = {
    b: __builtins__[b] if isinstance(__builtins__, dict) else getattr(__builtins__, b)
    for b in ("abs", "min", "max", "sum", "round", "len", "range", "enumerate",
              "zip", "sorted", "reversed", "map", "filter", "list", "dict", "set",
              "tuple", "str", "int", "float", "bool", "bytes", "print", "any", "all",
              "isinstance", "type", "repr", "format", "ord", "chr", "divmod", "pow",
              # read-only introspection so a tool can DISCOVER a library's API surface
              # (e.g. `dir(client)` to find the methods it exposes) instead of guessing
              "dir", "hasattr", "callable",
              "True", "False", "None", "ValueError", "TypeError", "KeyError",
              "IndexError", "ZeroDivisionError", "Exception", "sorted")
    if (b in (__builtins__ if isinstance(__builtins__, dict) else dir(__builtins__)))
}

_TYPE_MAP = {"string": str, "str": str, "number": float, "float": float,
             "integer": int, "int": int, "boolean": bool, "bool": bool,
             # structured types — without these, an `array`/`object` param fell back to `str`,
             # so a tool taking list-of-dicts input rejected the real value at test time (the
             # data-transform tool-build failure). `list`/`dict` accept any items (permissive,
             # which is right for model-authored tools).
             "array": list, "list": list, "object": dict, "dict": dict}


_NO_DATA_MARKERS = ("no data", "not found", "no results", "no record",
                    "none found", "nothing found", "could not find", "no entries",
                    "0 results", "empty", "n/a", "null", "no information",
                    # error-shaped output: a tool that CAUGHT an exception and returned a message
                    # (so it doesn't crash) still isn't working — don't pass it as "verified".
                    "error fetching", "unable to fetch", "could not fetch", "failed to fetch",
                    # parse-shaped failure: the fetch worked but the code couldn't read the response
                    # (wrong keys/nesting) and fell back to a placeholder — a real, common bug.
                    "unable to parse", "could not parse", "failed to parse", "couldn't parse",
                    "check your")

# Placeholder tokens that signal a report the code couldn't fill in — TBD/0h/0%/None used as
# stand-ins. A few of these together (not one in isolation) means the parsing produced empty values.
_PLACEHOLDER_RE = re.compile(
    r"\btbd\b|\bn/?a\b|\b0(?:\.0)?\s*h\b|\b0\s*%|\b0\s*min\b|:\s*(?:none|null|tbd)\b", re.I)


_DATE_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")
# Two variants with DIFFERENT weekdays (Sun / Tue) and different magnitudes, so a legit tool
# whose output space is small (e.g. day-of-week has 7 values) won't collide on both by chance.
_PERTURB_DATES = ("2001-09-09", "2001-09-11")
_PERTURB_DELTAS = (100, 37)


def _perturbations(args_dict: dict) -> list:
    """Up to two args copies with date/number values changed to clearly-different ones. Empty
    if nothing is unambiguously perturbable. Used to detect tools that ignore their inputs."""
    variants = []
    for i in (0, 1):
        out = dict(args_dict)
        changed = False
        for k, v in list(out.items()):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[k] = v + _PERTURB_DELTAS[i]
                changed = True
            elif isinstance(v, str) and _DATE_RE.match(v):
                out[k] = _PERTURB_DATES[i]
                changed = True
        if changed:
            variants.append(out)
    return variants


def _looks_like_no_data(text: str) -> bool:
    """Heuristic: does a (non-erroring) test result look like an empty/no-data answer rather
    than real data? Used to stop a graceful 'no data' return from passing as 'verified'.
    Flags empties, error-shaped output, or explicit no-data phrasing — short valid answers
    ('42') are fine."""
    t = (text or "").strip().lower()
    if not t or t.startswith("error"):
        return True
    if any(m in t for m in _NO_DATA_MARKERS):
        return True
    # A report peppered with placeholders (TBD / 0h / 0% / None) is empty data, not a real result.
    return len(_PLACEHOLDER_RE.findall(t)) >= 2


def _discarded_tool_calls(code: str, known_tools: set) -> list:
    """Bare-statement calls to CALL_TOOL or another tool whose RETURN VALUE is thrown away — not
    assigned, returned, or used. A created tool calls another tool for its DATA, so discarding the
    result is almost always a bug: e.g. `ascii_chart({...})` alone computes a chart and drops it
    instead of embedding it in the returned string. Returns the discarded callees (deduped)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out, seen = [], set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        if not isinstance(fn, ast.Name):
            continue
        name = None
        if fn.id == "CALL_TOOL":
            a0 = node.value.args[0] if node.value.args else None
            name = a0.value if isinstance(a0, ast.Constant) and isinstance(a0.value, str) else "CALL_TOOL"
        elif fn.id in known_tools:
            name = fn.id
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


class ToolValidationError(Exception):
    pass


class DisallowedImportError(ToolValidationError):
    """A specific validation failure: the code imports a module outside the allowlist.
    Carries the top-level module name so the caller can file an approval request."""
    def __init__(self, module: str, allowed: set):
        self.module = module
        super().__init__(
            f"import of '{module}' is not allowed. Allowed: {', '.join(sorted(allowed))}")


class RestrictedCapabilityError(ToolValidationError):
    """The code uses a restricted CAPABILITY (a forbidden builtin like open/exec/getattr, or dunder
    access) — not installable, not a package. Unlockable only by the human-reviewed trusted tier."""


def scan_ast(code: str, allow_network: bool, extra_modules: Optional[set] = None) -> None:
    """Raise ToolValidationError if the code uses anything outside the sandbox.
    `extra_modules` are human-approved packages unioned into the import allowlist."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ToolValidationError(f"syntax error: {e.msg} (line {e.lineno})")

    allowed = ALLOWED_MODULES | (NETWORK_MODULES if allow_network else set()) | (extra_modules or set())

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = ([n.name for n in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""])
            for m in mods:
                top = m.split(".")[0]
                if m not in allowed and top not in allowed:
                    raise DisallowedImportError(top, allowed)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise RestrictedCapabilityError(f"access to dunder attribute '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_CALLS:
                raise RestrictedCapabilityError(f"use of '{node.id}' is not allowed")
            if node.id.startswith("__") and node.id.endswith("__"):
                raise RestrictedCapabilityError(f"use of '{node.id}' is not allowed")


# Runtime imports that stay blocked even as internal machinery — the dangerous modules and
# their private C backers. Everything else in the stdlib is permitted AT RUNTIME so that an
# allowed module's lazy internal imports work (the landmine: datetime.strptime lazily imports
# the private `_strptime`, which the old strict allowlist blocked, breaking all date parsing).
# The tool's OWN source still can't import any of this — scan_ast + the no-__import__ rule are
# the real boundary; this only serves allowed modules' internals.
_RUNTIME_DENY = {
    "os", "sys", "subprocess", "_posixsubprocess", "socket", "_socket", "ssl", "_ssl",
    "shutil", "pathlib", "importlib", "ctypes", "_ctypes", "multiprocessing", "signal",
    "_signal", "mmap", "fcntl", "pty", "tty", "termios", "resource", "select", "_select",
    "asyncio", "_asyncio", "concurrent", "code", "codeop", "pdb", "bdb", "threading",
    "_winapi", "msvcrt", "nt", "posix", "grp", "pwd", "spwd", "webbrowser", "ftplib",
    "smtplib", "http", "urllib", "xmlrpc", "socketserver",
}


def _make_guarded_import(allowed: set) -> Callable:
    """A drop-in __import__ for created tools: permits the tool's allowlisted modules plus
    harmless stdlib machinery their internals need, blocks the dangerous set, and returns the
    SSRF-guarded httpx stand-in for `import httpx`."""
    import builtins
    import sys as _sys
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if name not in allowed and top not in allowed:
            if top in _RUNTIME_DENY:
                raise ImportError(f"import of '{name}' is not allowed in a created tool")
            # allow only safe stdlib / private helper modules (an allowed module's internals);
            # third-party (non-stdlib) stays blocked to the strict allowlist.
            if not (top in _sys.stdlib_module_names or top.startswith("_")):
                raise ImportError(f"import of '{name}' is not allowed in a created tool")
        if top == "httpx":
            import httpx as _real_httpx
            return _SafeHTTPX(_real_httpx)
        return real_import(name, globals, locals, fromlist, level)

    return guarded_import


def _compile_run(code: str, allow_network: bool, extra_modules: Optional[set] = None,
                 secrets: Optional[dict] = None) -> Callable:
    """Compile the code in a restricted namespace and return its `run` callable.
    `secrets` is exposed to the tool code as a dict named SECRETS (the sandbox forbids
    `import os`, so this is the only way a created tool can read a credential)."""
    scan_ast(code, allow_network, extra_modules)
    import importlib
    mods = ALLOWED_MODULES | (NETWORK_MODULES if allow_network else set()) | (extra_modules or set())
    builtins_ns = dict(SAFE_BUILTINS)
    builtins_ns["__import__"] = _make_guarded_import(mods)  # runtime allowlist enforcement
    ns: dict = {"__builtins__": builtins_ns, "SECRETS": dict(secrets or {})}
    # Pre-import allowed modules so bare `import x` at module scope also works.
    for m in mods:
        top = m.split(".")[0]
        try:
            mod = importlib.import_module(top)
            ns[top] = _SafeHTTPX(mod) if top == "httpx" else mod
        except Exception:
            pass
    try:
        exec(compile(code, "<created_tool>", "exec"), ns)  # noqa: S102 - sandboxed above
    except Exception as e:
        raise ToolValidationError(f"code failed to load: {type(e).__name__}: {e}")
    run = ns.get("run")
    if run is None or not callable(run):
        raise ToolValidationError("code must define a function named `run(args)`")
    if asyncio.iscoroutinefunction(run):
        raise ToolValidationError(
            "`run` must be a REGULAR function `def run(args): ...`, not `async def`. "
            "Call HTTP APIs synchronously, e.g. `resp = httpx.get(url)` (no await).")
    return run


def _compile_trusted(code: str, secrets: Optional[dict] = None) -> Callable:
    """Compile a HUMAN-APPROVED trusted tool with NO sandbox: real builtins, unrestricted imports,
    no SSRF guard. Only ever called for code whose exact hash a human approved (TrustStore). Still
    runs under DynamicTool's wall-clock timeout, and CALL_TOOL/tool-name composition is injected at
    call time like any created tool."""
    ns: dict = {"SECRETS": dict(secrets or {})}   # no __builtins__ override -> real builtins
    try:
        exec(compile(code, "<trusted_tool>", "exec"), ns)  # noqa: S102 - human-reviewed & approved
    except Exception as e:
        raise ToolValidationError(f"code failed to load: {type(e).__name__}: {e}")
    run = ns.get("run")
    if run is None or not callable(run):
        raise ToolValidationError("code must define a function named `run(args)`")
    if asyncio.iscoroutinefunction(run):
        raise ToolValidationError("`run` must be a regular `def run(args): ...`, not `async def`.")
    return run


def build_params_model(name: str, params_spec: dict) -> type[BaseModel]:
    """Build a pydantic model from a parameter spec. Small models supply this in
    two shapes, so accept BOTH:
      * flat map:    {"city": {"type": "string", "description": "..."}}
      * JSON Schema: {"type": "object", "properties": {"city": {...}}, "required": [...]}
    """
    if not params_spec:
        return create_model(f"{name}_Params")

    # Detect JSON-Schema shape and normalize to the flat map.
    required_set = None
    if isinstance(params_spec.get("properties"), dict) and "type" in params_spec:
        required_set = set(params_spec.get("required", []) or [])
        params_spec = params_spec["properties"]

    fields = {}
    for pname, info in params_spec.items():
        info = info if isinstance(info, dict) else {"type": str(info)}
        pytype = _TYPE_MAP.get(str(info.get("type", "string")).lower(), str)
        if required_set is not None:
            required = pname in required_set
        else:
            required = info.get("required", True)
        default = ... if required else info.get("default", None)
        fields[pname] = (pytype, Field(default, description=info.get("description", "")))
    return create_model(f"{name}_Params", **fields)


def _tool_fn(tname: str, call_tool: Callable) -> Callable:
    """Wrap a tool as a plain callable so sandboxed code can invoke it the INTUITIVE way —
    `get_account_data({'date_range': 'last 7 days'})` — instead of CALL_TOOL('name', {...})
    (which the model reaches for far less often). Accepts a dict or keyword args."""
    def fn(args=None, **kwargs):
        return call_tool(tname, args if args is not None else kwargs)
    return fn


def _make_call_tool(registry, loop, timeout: float = 120.0) -> Callable:
    """Return a `CALL_TOOL(name, args)` the sandboxed tool code can use to invoke ANOTHER
    registered tool and get its string result (tool composition — e.g. a report tool calls
    get_account_data instead of re-implementing or faking it). Bridges the worker thread
    back to the event loop. Never raises into tool code — returns a 'CALL_TOOL error: ...'."""
    def call_tool(name, args=None):
        if registry is None:
            return "CALL_TOOL error: tool composition is unavailable in this context"
        tool = registry.get(name)
        if tool is None:
            return f"CALL_TOOL error: no tool named '{name}'. Available: {', '.join(registry.names())}"
        try:
            params = tool.Params(**(args or {}))
        except Exception as e:
            return f"CALL_TOOL error: invalid args for '{name}': {e}"
        try:
            fut = asyncio.run_coroutine_threadsafe(tool.run(params), loop)
            return str(fut.result(timeout=timeout))
        except Exception as e:
            return f"CALL_TOOL error while running '{name}': {type(e).__name__}: {e}"
    return call_tool


class DynamicTool(Tool):
    def __init__(self, name, description, params_model, run_fn=None, timeout=15.0, registry=None,
                 *, sandboxed=False, code="", runtime=None, workspace="default"):
        self.name = name
        self.description = description
        self.Params = params_model
        self._run_fn = run_fn                # host-side compiled fn; None for a sandboxed tool
        self._timeout = timeout
        self.registry = registry             # enables CALL_TOOL composition (host-side only)
        # Container path (stage 2b): when sandboxed, the raw code runs in the sandbox container via
        # runner.py — full stdlib, no AST gate, NO composition. run_fn is unused.
        self.sandboxed = sandboxed
        self.code = code
        self.runtime = runtime               # SandboxRuntime | None
        self.workspace = workspace

    async def run(self, args: BaseModel) -> str:
        if self.sandboxed:
            return await self._run_in_container(args)
        return await self._run_host_side(args)

    async def _run_in_container(self, args: BaseModel) -> str:
        """Ship {code, args} to runner.py in the container and parse the JSON result. Fail CLOSED:
        a sandboxed tool was authored assuming the full stdlib (it may `import os`), so if the
        sandbox is off/unavailable it must refuse — never run host-side."""
        import json as _json

        from engine.sandbox.runtime import SandboxUnavailable
        if self.runtime is None or not self.runtime.available():
            return (f"{self.name}: this tool runs in the container sandbox, which is currently off "
                    "or unavailable. Enable it in the dashboard's Settings > Sandbox, then try again.")
        loop = asyncio.get_running_loop()
        try:
            payload = _json.dumps({"code": self.code, "args": args.model_dump()})
            r = await loop.run_in_executor(None, lambda: self.runtime.exec(
                self.workspace, ["python", "/opt/argus/runner.py"], stdin=payload,
                timeout=self._timeout))
        except SandboxUnavailable as e:
            return f"{self.name}: the sandbox is unavailable ({e})."
        except Exception as e:                       # noqa: BLE001 - never crash the loop
            return f"{self.name} error: {type(e).__name__}: {e}"
        if r.timed_out:
            return f"{self.name} error: timed out after {self._timeout}s"
        if not (r.stdout or "").strip():
            return f"{self.name} error: no output from sandbox; stderr: {(r.stderr or '')[:400]}"
        try:
            out = _json.loads(r.stdout)
        except Exception:
            return f"{self.name} error: bad runner output: {(r.stdout or r.stderr)[:400]}"
        if out.get("ok"):
            return str(out.get("result", ""))
        return f"{self.name} error: {out.get('error', 'unknown error')}"

    async def _run_host_side(self, args: BaseModel) -> str:
        payload = args.model_dump()
        loop = asyncio.get_running_loop()
        call_tool = _make_call_tool(self.registry, loop)
        gl = self._run_fn.__globals__
        gl["CALL_TOOL"] = call_tool
        # ALSO expose every existing tool as a bare callable, so intuitive code works:
        #   data = get_account_data({'date_range': 'last 7 days'})
        # (the model writes this, not CALL_TOOL). Skip self to avoid trivial recursion.
        if self.registry is not None:
            for tname in self.registry.names():
                if tname != self.name:
                    gl[tname] = _tool_fn(tname, call_tool)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._run_fn, payload),
                                            timeout=self._timeout)
        except asyncio.TimeoutError:
            return f"{self.name} error: timed out after {self._timeout}s"
        except Exception as e:
            return f"{self.name} error: {type(e).__name__}: {e}"
        return str(result)


def load_persisted_tools(persist_dir: str, timeout: float = 15.0,
                         extra_modules: Optional[set] = None,
                         secrets: Optional[dict] = None, trust_store=None,
                         *, sandbox_runtime=None, sandbox_workspace: str = "default") -> list:
    """Recompile previously-created tools (JSON manifests) into DynamicTools at startup.
    Compile only — the tool body runs only when actually called. Skips any that fail.
    `extra_modules` are approved packages so tools that used them still compile. A manifest marked
    trusted is compiled UNSANDBOXED only if the trust store still trusts it at the exact code hash
    (revoked or changed → falls back to the sandbox, which will likely reject and skip it).
    A manifest marked `sandboxed` is NOT compiled host-side at all (its code may `import os`) —
    it's shipped raw to the container's runner.py at run time."""
    from engine.experimental.trust_store import code_hash as _chash
    tools = []
    if not persist_dir or not os.path.isdir(persist_dir):
        return tools
    for fn in sorted(os.listdir(persist_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            m = json.load(open(os.path.join(persist_dir, fn), encoding="utf-8"))
            params = build_params_model(m["name"], m.get("parameters", {}))
            if m.get("sandboxed"):
                # Container tool: DON'T compile host-side (its code may `import os`). Ship the raw
                # code to runner.py at run time.
                tools.append(DynamicTool(m["name"], m["description"], params, run_fn=None,
                                         timeout=timeout, sandboxed=True, code=m["code"],
                                         runtime=sandbox_runtime, workspace=sandbox_workspace))
                continue
            trusted = bool(m.get("trusted") and trust_store is not None
                           and trust_store.is_trusted(m["name"], _chash(m["code"])))
            if trusted:
                run_fn = _compile_trusted(m["code"], secrets)
            else:
                run_fn = _compile_run(m["code"], m.get("allow_network", False), extra_modules, secrets)
            tools.append(DynamicTool(m["name"], m["description"], params, run_fn, timeout))
        except Exception:
            log.exception("could not load persisted tool %s", fn)
    return tools


class CreateToolTool(Tool):
    name = "create_tool"
    description = (
        "Create a NEW tool when no existing tool can do what the user needs. "
        "Provide: name (snake_case); description (when to use it); parameters (object "
        "mapping each argument to {type, description}); code — a REGULAR Python function "
        "`def run(args): ...` (NOT async) that takes a dict of the arguments and returns a "
        "string; and test_args — example arguments so the tool is test-run immediately. "
        "You may import: math, statistics, datetime, json, re, calendar, zoneinfo, and "
        "httpx for web APIs (call it synchronously: resp = httpx.get(url)). "
        "To REUSE an existing tool inside your code, just CALL IT BY NAME like a function: "
        "`data = get_account_data({'date_range': 'last 7 days'})` returns that tool's result "
        "as a string. Prefer this over re-implementing or (never!) hardcoding data — e.g. a "
        "report tool should call get_account_data({...}) and format the real result. "
        "To FIX a tool you already made, just call create_tool again with the SAME name and "
        "corrected code — it replaces the old one. The tool is verified by a test run before it "
        "registers; if the test fails, fix the code and try again. After it is created, CALL it and "
        "answer ONLY from its real output."
    )

    class Params(BaseModel):
        name: str = Field(..., description="snake_case tool name")
        description: str = Field(..., description="what the tool does / when to use it")
        parameters: dict = Field(default_factory=dict,
                                 description='e.g. {"city": {"type":"string","description":"city name"}}')
        code: str = Field(..., description="Python: def run(args): ... return <string>")
        test_args: dict = Field(default_factory=dict,
                                description='example args to test-run the tool once, e.g. {"city":"Nashville"}')
        sandboxed: Optional[bool] = Field(
            None, description="run this tool in the container sandbox (full stdlib, no calling other "
                              "tools). Default: on when the sandbox is available. Set false if the "
                              "tool must call another Argus tool.")

    def __init__(self, registry: ToolRegistry, allow_network: bool = False,
                 validate_only: bool = False, timeout: float = 15.0,
                 persist_dir: Optional[str] = None, dep_store=None, session_id: str = "",
                 secrets: Optional[dict] = None, created_sink: Optional[list] = None,
                 trust_store=None, allow_trusted: bool = False,
                 reserved_names: Optional[set] = None,
                 approvals=None, run_id: str = "", origin: str = "api",
                 sandbox_runtime=None, sandbox_workspace: str = "default",
                 sandbox_enabled: bool = False):
        self.registry = registry
        # First-class built-ins whose NAMES are protected even when the tool isn't currently
        # registered (e.g. crawl_site/web_search when their dependency URL isn't configured). Without
        # this, a gated-off built-in's name is free for the model to shadow with a sandbox tool.
        self.reserved_names = set(reserved_names or ())
        self.allow_network = allow_network
        self.validate_only = validate_only
        self.timeout = timeout
        self.persist_dir = persist_dir   # if set, created tools survive restarts
        self.dep_store = dep_store       # if set, a disallowed import files an approval request
        self.session_id = session_id
        self.secrets = secrets or {}     # env-var secrets exposed to the tool as SECRETS
        # the engine's live list of created tools: appending here makes a new tool available in
        # LATER turns of this same process without a restart (fixes the "unknown tool" bug).
        self.created_sink = created_sink
        self.trust_store = trust_store       # trusted-tool tier (human-approved unsandboxed code)
        self.allow_trusted = allow_trusted   # master switch: may a restricted-capability tool go trusted
        # interactive-approvals broker: when set, a disallowed third-party import GATES through it
        # (block for a human decision, install + recompile in the SAME turn) instead of the legacy
        # DepStore.request() + return string. None -> legacy behavior (back-compat).
        self.approvals = approvals
        self.run_id = run_id
        self.origin = origin
        self.sandbox_runtime = sandbox_runtime      # SandboxRuntime | None
        self.sandbox_workspace = sandbox_workspace
        self.sandbox_enabled = sandbox_enabled
        if self.secrets:                 # tell the model the exact keys it may use
            keys = sorted(self.secrets)
            self.description = (
                self.description + " Authorized credentials are available to your code as a "
                f"dict named SECRETS (do NOT import os). Available keys: {', '.join(keys)}. "
                f"e.g. SECRETS['{keys[0]}'].")
        self.created: list[dict] = []   # audit log of what the model authored

    def _resolve_sandboxed(self, requested) -> bool:
        """The flag for a new tool. Explicit value wins; else default to the container when it is
        actually usable (enabled AND available) — the stronger boundary is the safe default."""
        if requested is not None:
            return bool(requested)
        return bool(self.sandbox_enabled and self.sandbox_runtime is not None
                    and self.sandbox_runtime.available())

    def _register(self, tool, args) -> None:
        """Register in the current run, persist to disk, AND add to the engine's live created-tools
        list (dedup by name) so the tool is usable in later turns without waiting for a restart."""
        self.registry.register(tool)
        self._persist(args)
        if self.created_sink is not None:
            self.created_sink[:] = [t for t in self.created_sink if t.name != tool.name]
            self.created_sink.append(tool)

    async def _ignores_input(self, tool, params_model, test_args: dict, baseline: str) -> bool:
        """True if the tool returns baseline for TWO clearly-different inputs — the tell-tale sign
        it ignores its arguments (hardcoded data). Conservative: only perturbs date/number args
        (where 'different' is unambiguous); needs both perturbed runs to succeed and match, so a
        legit small-output tool (e.g. day-of-week) that collides on one won't be falsely flagged."""
        variants = _perturbations(test_args or {})
        if len(variants) < 2:                       # nothing safely perturbable — can't conclude
            return False
        for pv in variants:
            try:
                other = await tool.run(params_model(**pv))
            except Exception:
                return False
            if other.strip().startswith(f"{tool.name} error") or other != baseline:
                return False                        # responds to input (or errored) -> not hardcoded
        return True                                 # identical output for 2 different inputs

    def _stdlib_block_message(self, module: str) -> str:
        """A disallowed stdlib import (os/sys/...) — not installable, restricted for safety.
        Steer the model to the sanctioned way to read credentials instead of failing blankly."""
        if self.secrets:
            keys = sorted(self.secrets)
            hint = (f" To read authorized credentials, use the SECRETS dict (available keys: "
                    f"{', '.join(keys)}), e.g. SECRETS['{keys[0]}']. Do NOT import {module}.")
        elif module == "os":
            hint = (" Reading environment variables isn't available to created tools. If you need "
                    "credentials (like a login), tell the user they must set TOOL_SECRET_NAMES and "
                    "the values in the server's .env first — you cannot proceed without that.")
        else:
            hint = f" The '{module}' module is restricted for safety and cannot be enabled."
        return (f"create_tool: '{module}' is part of Python's standard library — it can't be "
                f"installed and is restricted in created tools for safety.{hint}")

    def _file_trust_request(self, args: "CreateToolTool.Params") -> str:
        req = self.trust_store.request(args.name, args.code, self.session_id)
        return (f"create_tool: '{args.name}' needs a RESTRICTED capability the sandbox blocks (files, "
                "os, a database, etc.). Because it's your own code, it can run TRUSTED — but a HUMAN "
                f"must review the code and approve it first (request {req['id']}). Tell the user to open "
                "the dashboard's 'Trusted-tool requests' panel, read the code, and Approve it; then ask "
                "me to create it again. Do NOT fabricate the result in the meantime.")

    def _persist(self, args: "CreateToolTool.Params") -> None:
        if not self.persist_dir:
            return
        from engine.experimental.trust_store import code_hash as _chash
        trusted = bool(self.trust_store and self.trust_store.is_trusted(args.name, _chash(args.code)))
        sandboxed = self._resolve_sandboxed(args.sandboxed)
        safe = re.sub(r"[^a-z0-9_]+", "_", args.name.lower()).strip("_")
        try:
            os.makedirs(self.persist_dir, exist_ok=True)
            with open(os.path.join(self.persist_dir, f"{safe}.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": args.name, "description": args.description,
                           "parameters": args.parameters, "code": args.code,
                           "allow_network": self.allow_network, "trusted": trusted,
                           "sandboxed": sandboxed}, fh, indent=2)
        except Exception:
            log.exception("could not persist created tool %s", args.name)

    async def run(self, args: "CreateToolTool.Params") -> str:
        record = {"name": args.name, "description": args.description,
                  "parameters": args.parameters, "code": args.code,
                  "ok": False, "error": None}
        self.created.append(record)

        existing = self.registry.get(args.name)
        if existing is not None and not isinstance(existing, DynamicTool):
            record["error"] = "name is a built-in"
            return (f"create_tool error: '{args.name}' is a built-in tool and can't be replaced. "
                    "Choose a different name.")
        # A gated-off built-in (dependency not configured) is absent from the registry, so the check
        # above misses it. Protect its name anyway and steer to the real fix — configure the dep —
        # instead of letting the model reimplement it in the sandbox.
        if existing is None and args.name in self.reserved_names:
            record["error"] = "reserved built-in (dependency not configured)"
            return (f"create_tool error: '{args.name}' is a built-in tool that is turned OFF on this "
                    "server because its dependency isn't configured (e.g. SEARXNG_BASE_URL or "
                    "FIRECRAWL_BASE_URL is unset in the server's .env). Do NOT recreate it — tell the "
                    "user to set that URL in the .env, then the real tool becomes available.")
        # A previously-CREATED tool with this name is replaced — this is how you FIX/iterate a
        # tool: just call create_tool again with the same name and corrected code.
        if args.name in ("create_tool",):
            record["error"] = "reserved name"
            return "create_tool error: that name is reserved."

        sandboxed = self._resolve_sandboxed(args.sandboxed)
        if sandboxed:
            # Container path: NO AST scan (full stdlib is the point). Only check it parses and
            # defines run(args); the code executes in the container, never host-side.
            try:
                compile(args.code, "<sandboxed_tool>", "exec")
            except SyntaxError as e:
                record["error"] = str(e)
                return (f"create_tool: '{args.name}' has a syntax error: {e}\n"
                        "Fix the code and call create_tool again with the same name.")
            params_model = build_params_model(args.name, args.parameters)
            return await self._build_and_verify(args, None, params_model, record, sandboxed=True)

        extra = self.dep_store.approved_modules() if self.dep_store else set()
        from engine.experimental.trust_store import code_hash as _chash
        is_trusted = self.trust_store is not None and self.trust_store.is_trusted(args.name, _chash(args.code))
        try:
            if is_trusted:
                run_fn = _compile_trusted(args.code, self.secrets)   # human-approved: no sandbox
            else:
                run_fn = _compile_run(args.code, self.allow_network, extra, self.secrets)
            params_model = build_params_model(args.name, args.parameters)
        except DisallowedImportError as e:
            record["error"] = str(e)
            import sys as _sys
            if e.module in _sys.stdlib_module_names:
                # Stdlib module (os/sqlite3/subprocess/...): not installable, restricted for safety.
                # With the trusted tier on, offer the human-reviewed path; else steer to SECRETS.
                if self.allow_trusted and self.trust_store is not None:
                    return self._file_trust_request(args)
                return self._stdlib_block_message(e.module)
            if self.approvals is not None:
                # Interactive approvals ON: GATE through the broker instead of filing a DepStore
                # request — blocks for a human decision; approved installs + recompiles in THIS
                # same turn; denied/timeout never get here as a "filed a request" string (the bug
                # this replaces). timeout raises TurnPaused, which propagates out of run().
                d = await self.approvals.gate(
                    "dep-install", e.module, self.session_id, self.run_id,
                    prompt=f"Install Python package '{e.module}' for tool '{args.name}'.",
                    origin=self.origin,
                    payload={"module": e.module, "tool_name": args.name, "code": args.code})
                if d.denied:
                    return (f"Dependency '{e.module}' was not approved; "
                            f"'{args.name}' was not created.")
                if not d.one_shot:
                    # Live approval (or an auto-allow) THIS turn: install now. A one-shot decision
                    # means a DEFERRED resume (Engine._resume_dep) already installed the module
                    # before spawning this re-run — don't install it a second time.
                    from engine.experimental import dep_installer
                    ok, version, log_tail = await dep_installer.install(e.module)
                    if not ok:
                        return f"create_tool: install of '{e.module}' failed: {log_tail}"
                    if self.dep_store is not None:
                        # Record it so it lands in the startup allowlist too — otherwise a
                        # persisted tool using this module silently fails to recompile after a
                        # restart (the gate path installs live but, unlike the legacy
                        # request()/mark_approved() flow, had no DepStore record at all).
                        self.dep_store.allow_module(e.module)
                # Dep now present — recompile the tool NOW (retry the build path, in this same
                # turn) instead of returning a "try again later" string. Reuses the exact
                # compile-success tail a clean scan takes (_build_and_verify) so the two paths
                # can't drift.
                retry_extra = extra | {e.module}
                try:
                    if is_trusted:
                        run_fn = _compile_trusted(args.code, self.secrets)
                    else:
                        run_fn = _compile_run(args.code, self.allow_network, retry_extra, self.secrets)
                    params_model = build_params_model(args.name, args.parameters)
                except ToolValidationError as e2:
                    record["error"] = str(e2)
                    return (f"create_tool: '{args.name}' could not be built — the dependency may "
                            f"not be installed, or the code failed to compile: {e2}\n"
                            "Fix the code and call create_tool again with the same name.")
                return await self._build_and_verify(args, run_fn, params_model, record, sandboxed=False)
            if self.dep_store is not None:
                # Legacy (approvals off): file a request a human can approve, then the model retries.
                req = self.dep_store.request(e.module, args.name, self.session_id, args.code)
                return (f"create_tool: '{args.name}' needs the '{e.module}' library, which isn't "
                        "installed yet. I've filed a request to install it — it needs your approval "
                        f"first (request id {req['id']}). Approve it, then ask me to try again. "
                        "Do NOT invent the result in the meantime.")
            return (f"create_tool error: {e}\n"
                    "Use only the allowed standard-library modules (and httpx for web APIs).")
        except RestrictedCapabilityError as e:
            record["error"] = str(e)
            # A forbidden builtin (open/exec/getattr) or dunder — unlockable only via trusted tier.
            if self.allow_trusted and self.trust_store is not None:
                return self._file_trust_request(args)
            return (f"create_tool error: {e}\n"
                    "Fix the code and call create_tool again — use only allowed operations.")
        except ToolValidationError as e:
            record["error"] = str(e)
            return (f"create_tool error: {e}\n"
                    "Fix the code and call create_tool again. Remember: define "
                    "`def run(args): ...` returning a string; args is a dict of your parameters.")

        return await self._build_and_verify(args, run_fn, params_model, record)

    async def _build_and_verify(self, args: "CreateToolTool.Params", run_fn: Callable,
                                params_model: type[BaseModel], record: dict, *,
                                sandboxed: bool = False) -> str:
        """Shared tail: compile succeeded (run_fn/params_model are ready) — validate-only report,
        or build the DynamicTool, test-run it, and register + report (or explain why not). Used by
        BOTH a clean compile and the post-dep-install retry compile (DRY: same success path either
        way, so this only needs to exist once). `sandboxed` additionally covers the new container
        authoring path, which never has a host-side run_fn."""
        if self.validate_only:
            record["ok"] = True
            return (f"create_tool: '{args.name}' validated OK (not registered in this "
                    "validation-only run).")

        if sandboxed:
            tool = DynamicTool(args.name, args.description, params_model, run_fn=None,
                               timeout=self.timeout, sandboxed=True, code=args.code,
                               runtime=self.sandbox_runtime, workspace=self.sandbox_workspace)
        else:
            tool = DynamicTool(args.name, args.description, params_model, run_fn, self.timeout,
                               registry=self.registry)   # registry enables CALL_TOOL composition

        # Auto test-run BEFORE registering. A sandboxed tool authored while the sandbox is OFF
        # cannot be container-tested; save it anyway (the flag is a property of the tool) and say so.
        if sandboxed and (self.sandbox_runtime is None or not self.sandbox_runtime.available()):
            self._register(tool, args)
            record["ok"] = True
            return (f"create_tool: '{args.name}' was created as a sandboxed tool, but the container "
                    "sandbox is off, so it couldn't be test-run yet. It will run once you enable the "
                    "sandbox in Settings.")

        # Auto test-run: execute the new tool once BEFORE registering, so a broken tool
        # is caught in this same turn (and NOT registered — the model can retry the same
        # name after fixing) instead of surfacing after a hallucinated answer.
        can_test = True
        try:
            test_input = params_model(**(args.test_args or {}))
        except Exception:
            can_test = False  # required args without a test value — register optimistically

        if can_test:
            test_result = await tool.run(test_input)
            record["test_result"] = test_result
            if test_result.strip().startswith(f"{args.name} error:"):
                record["error"] = f"test-run failed: {test_result}"
                return (f"create_tool: '{args.name}' was NOT created — its test run failed:\n"
                        f"  {test_result}\n"
                        "Fix the code and call create_tool again with the same name.")
            self._register(tool, args)
            record["ok"] = True
            # Did the code CALL a helper tool (e.g. ascii_chart) but throw away its result? Then the
            # output it produces is missing that piece even though the run didn't error. Warn precisely.
            discarded = _discarded_tool_calls(args.code, set(self.registry.names()) - {args.name})
            disc_warn = ("" if not discarded else
                         "\n⚠️ Your code CALLS " + ", ".join(discarded) + " but DISCARDS the result — "
                         "the return value isn't captured or included in what you return, so it will NOT "
                         "appear in the output. Capture it (e.g. `chart = ascii_chart({...})`) and put "
                         "that string into what you return, then re-create with the same name.")
            # A test run that DIDN'T crash isn't the same as CORRECT. Flag an empty/"no data" result
            # (usually a PARSING bug, not truly-absent data) and/or a discarded helper-tool result.
            no_data = _looks_like_no_data(test_result)
            if no_data or disc_warn:
                msg = (f"create_tool: '{args.name}' was created, but its test run looks WRONG:\n"
                       f"  {test_result.strip()[:300]}{disc_warn}\n")
                if no_data:
                    msg += ("⚠️ A run that doesn't error is NOT proof it's correct. This usually means "
                            "the code parses the response WRONG (wrong keys or nesting), not that the "
                            "data is truly missing. Make a quick probe version that returns the RAW "
                            "response — e.g. `import json` then `return json.dumps(resp)[:2000]` — CALL "
                            "it, read the actual keys, then re-create with correct parsing. Only report "
                            "'no data' once you've confirmed the raw response is genuinely empty.")
                else:
                    msg += ("Fix it and re-create with the same name, then CALL it and confirm the "
                            "output is actually right — not just that it ran.")
                return msg
            # Hardcode detector: re-run with a clearly-different input (a different date/number).
            # Identical output for genuinely different input means the tool IGNORES its arguments —
            # the signature of baked-in / fabricated data (the fake report tool did exactly this).
            if await self._ignores_input(tool, params_model, args.test_args, test_result):
                return (f"create_tool: '{args.name}' was created, but it returned the SAME output for "
                        "two DIFFERENT inputs — a strong sign it IGNORES its arguments and has HARDCODED "
                        "data baked in (a fake tool that returns canned values forever). A tool must FETCH "
                        "live data using its arguments. Rewrite it to actually use its inputs against a real "
                        "source — call an API, or use CALL_TOOL('other_tool', {...}) to reuse an existing "
                        "tool's real data — then re-create with the same name.")
            return (f"create_tool: '{args.name}' created and verified — the test run returned real "
                    f"output:\n  {test_result.strip()[:400]}\n"
                    f"If that answers the user, CALL {args.name} now (or just report that result). "
                    "Base your answer ONLY on the tool's real output — never invent data.")

        self._register(tool, args)
        record["ok"] = True
        return (f"create_tool: '{args.name}' created. Now CALL {args.name} with the right "
                "arguments to get the result. Base your answer ONLY on the tool's real output.")


class InspectToolTool(Tool):
    """Read a created tool's source so the model can REVISE it correctly (copy its working
    auth/library pattern) instead of reinventing from scratch."""
    name = "inspect_tool"
    description = (
        "Show the source code, parameters, and description of a tool you (or a past session) "
        "created. Use this BEFORE revising or extending a tool: read how it fetches its data — "
        "which library, how it authenticates — then call create_tool with the SAME name and code "
        "that keeps that working pattern and adds what you need. Argument: name."
    )

    class Params(BaseModel):
        name: str = Field(..., description="the created tool's name")

    def __init__(self, persist_dir: Optional[str]):
        self.persist_dir = persist_dir

    async def run(self, args: "InspectToolTool.Params") -> str:
        if not self.persist_dir or not os.path.isdir(self.persist_dir):
            return "inspect_tool: no created tools are available."
        safe = re.sub(r"[^a-z0-9_]+", "_", (args.name or "").lower()).strip("_")
        path = os.path.join(self.persist_dir, f"{safe}.json")
        if not os.path.exists(path):
            avail = [f[:-5] for f in sorted(os.listdir(self.persist_dir)) if f.endswith(".json")]
            return f"inspect_tool: no created tool named '{args.name}'. Created tools: {', '.join(avail) or '(none)'}."
        try:
            m = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            return f"inspect_tool: could not read '{args.name}': {e}"
        return (f"Tool '{m['name']}' — {m.get('description', '')}\n"
                f"parameters: {json.dumps(m.get('parameters', {}))}\n"
                f"code:\n{m['code']}")


class DeleteToolTool(Tool):
    """Delete a tool the model created, when the user no longer wants it. Built-ins are protected."""
    name = "delete_tool"
    description = (
        "Delete a CREATED tool by name when the user asks to remove it (or when a tool is broken and "
        "you're replacing it with a different name). Only tools you created can be deleted — built-in "
        "tools (calculator, web_search, weather, …) are protected. Argument: name."
    )

    class Params(BaseModel):
        name: str = Field(..., description="the created tool's name to delete")

    def __init__(self, registry: ToolRegistry, persist_dir: Optional[str] = None,
                 created_sink: Optional[list] = None):
        self.registry = registry
        self.persist_dir = persist_dir
        self.created_sink = created_sink

    async def run(self, args: "DeleteToolTool.Params") -> str:
        existing = self.registry.get(args.name)
        if existing is None:
            return f"delete_tool: no tool named '{args.name}'."
        if not isinstance(existing, DynamicTool):
            return f"delete_tool: '{args.name}' is a built-in tool and can't be deleted."
        self.registry.unregister(args.name)
        if self.created_sink is not None:
            self.created_sink[:] = [t for t in self.created_sink if t.name != args.name]
        if self.persist_dir:
            safe = re.sub(r"[^a-z0-9_]+", "_", args.name.lower()).strip("_")
            path = os.path.join(self.persist_dir, f"{safe}.json")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                log.exception("could not delete persisted tool %s", args.name)
        return f"delete_tool: '{args.name}' deleted."
