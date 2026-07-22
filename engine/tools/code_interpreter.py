"""exec_python — a sandboxed Python REPL for quick, exploratory computation.

This is the *ephemeral computation* companion to create_tool (which builds *persistent* capabilities):
run a short snippet, get stdout/stderr/last-value back, keep variables around within a session.

It reuses create_tool's exact sandbox gates (engine.experimental.tool_creation): the AST scan, the
whitelisted builtins, the guarded __import__, and the SSRF-guarded httpx stand-in. What it adds on
top is (a) stdout/stderr capture (the create_tool runner returns only a value), (b) REPL-style
"show the last expression's value", and (c) a per-session namespace so variables persist between
calls. Same soft (language-level) sandbox as create_tool — no seccomp/rlimits/container — so it is
gated behind ENABLE_CODE_INTERPRETER and runs under a wall-clock timeout on a worker thread.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import io
import traceback
from contextlib import redirect_stderr, redirect_stdout

from pydantic import BaseModel, Field

from engine.experimental.tool_creation import (ALLOWED_MODULES, NETWORK_MODULES, SAFE_BUILTINS,
                                               ToolValidationError, _make_guarded_import,
                                               _SafeHTTPX, scan_ast)
from engine.tools.base import Tool

_MAX_OUTPUT = 4000        # cap returned text so a runaway print() can't flood the model's context


class CodeInterpreter:
    """Owns the per-session REPL namespaces and runs snippets in the shared sandbox. Held once on
    the engine (like TableStore); the per-turn ExecPythonTool delegates here with its session id, so
    variables survive across turns even though the tool instance is rebuilt each run."""

    def __init__(self, allow_network: bool = False, timeout: float = 10.0,
                 runtime=None, workspace: str = "default"):
        self.allow_network = allow_network
        self.timeout = timeout
        self.runtime = runtime            # SandboxRuntime | None. None = the in-process AST sandbox.
        self.workspace = workspace
        self._sessions: dict[str, dict] = {}

    def _new_namespace(self) -> dict:
        """A fresh restricted namespace — same construction as tool_creation._compile_run, minus the
        exec of a `def run`. Whitelisted builtins, guarded import, allowed modules pre-imported."""
        mods = ALLOWED_MODULES | (NETWORK_MODULES if self.allow_network else set())
        builtins_ns = dict(SAFE_BUILTINS)
        builtins_ns["__import__"] = _make_guarded_import(mods)
        ns: dict = {"__builtins__": builtins_ns}
        for m in mods:
            top = m.split(".")[0]
            try:
                mod = importlib.import_module(top)
                ns[top] = _SafeHTTPX(mod) if top == "httpx" else mod
            except Exception:
                pass
        return ns

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def run_sandboxed(self, code: str) -> str:
        """Run `code` inside the container. STATELESS by design in stage 1 — each call is a fresh
        `python -c`, so variables do NOT persist the way they do in the in-process REPL. The
        workspace filesystem is the way to carry state between calls; a persistent in-container
        REPL is deferred. Never raises: a container problem is reported to the model as text."""
        from engine.sandbox.runtime import SandboxUnavailable
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                None, lambda: self.runtime.exec(
                    self.workspace, ["python", "-c", code], timeout=self.timeout))
        except SandboxUnavailable as e:
            return f"exec_python error: the sandbox is unavailable ({e})."
        except Exception as e:                       # noqa: BLE001 - never kill the turn
            return f"exec_python error: {type(e).__name__}: {e}"
        if r.timed_out:
            return f"exec_python: timed out after {self.timeout}s."
        parts = []
        if r.stdout.strip():
            parts.append(r.stdout[:_MAX_OUTPUT])
        if r.stderr.strip():
            parts.append("stderr:\n" + r.stderr[:_MAX_OUTPUT])
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)

    async def run(self, session_id: str, code: str, reset: bool = False) -> str:
        if self.runtime is not None:
            return await self.run_sandboxed(code)
        code = code or ""
        try:
            scan_ast(code, self.allow_network)          # same static gate as create_tool
        except ToolValidationError as e:
            return f"exec_python blocked: {e}"
        if reset or session_id not in self._sessions:
            self._sessions[session_id] = self._new_namespace()
        ns = self._sessions[session_id]

        def _exec():
            out, err = io.StringIO(), io.StringIO()
            val = None
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    val = _exec_show_last(code, ns)
            except SyntaxError as e:
                err.write(f"SyntaxError: {e.msg} (line {e.lineno})")
            except BaseException as e:                  # surface the traceback so the model can self-correct
                err.write(_clean_traceback(e))
            return out.getvalue(), err.getvalue(), val

        try:
            out, err, val = await asyncio.wait_for(asyncio.to_thread(_exec), timeout=self.timeout)
        except asyncio.TimeoutError:
            return f"exec_python: timed out after {self.timeout:.0f}s (the code ran too long)"
        return _format_output(out, err, val)


def _exec_show_last(code: str, ns: dict):
    """Exec the snippet; if the final statement is a bare expression, eval it and return its value
    (REPL behaviour: the last line's value is shown).

    SECURITY: the exec()/eval()/compile() below are the deliberate sandbox-execution primitive of
    this tool — running model/user code is the whole point, and the feature is off unless the
    operator sets ENABLE_CODE_INTERPRETER. They are NOT a data-parsing shortcut (ast.literal_eval /
    json would be wrong here). The security boundary is `ns`, not the absence of exec: `ns` carries
    the whitelisted SAFE_BUILTINS + a guarded __import__ (no open/os/subprocess/socket/file access),
    and the caller (CodeInterpreter.run) has already run scan_ast(), which statically rejects
    disallowed imports, dunder access, and the forbidden builtins (open/exec/eval/compile/__import__/
    getattr/…) in the user code. This mirrors create_tool's accepted sandbox exactly; execution runs
    on a worker thread under a wall-clock timeout. The soft (language-level) nature of the sandbox is
    documented in the module docstring."""
    tree = ast.parse(code)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        if tree.body:
            exec(compile(tree, "<exec_python>", "exec"), ns)          # noqa: S102 - restricted ns
        return eval(compile(ast.Expression(last.value), "<exec_python>", "eval"), ns)  # noqa: S307
    exec(compile(tree, "<exec_python>", "exec"), ns)                  # noqa: S102 - restricted ns
    return None


def _clean_traceback(exc: BaseException) -> str:
    """Traceback trimmed to the user's frames (filename '<exec_python>') plus the exception line —
    hides the host runner frames so the model sees only its own error."""
    tb = traceback.extract_tb(exc.__traceback__)
    user = [f for f in tb if f.filename == "<exec_python>"]
    head = "Traceback (most recent call last):\n" + "".join(traceback.format_list(user)) if user else ""
    return head + "".join(traceback.format_exception_only(type(exc), exc)).rstrip()


def _format_output(out: str, err: str, val) -> str:
    parts = []
    if out.strip():
        parts.append(out.rstrip())
    if err.strip():
        parts.append(err.rstrip())
    if val is not None:
        parts.append(repr(val))
    text = "\n".join(parts) if parts else "(no output)"
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT] + f"\n… (output truncated at {_MAX_OUTPUT} chars)"
    return text


class ExecPythonTool(Tool):
    name = "exec_python"
    description = (
        "Run a short Python snippet in a sandbox for quick one-off computation — math, parsing, "
        "reshaping data, checking a calculation. Returns stdout, any error/traceback, and the value "
        "of the last expression (like a REPL). Variables persist between calls in the same "
        "conversation (pass reset=true to start fresh). Sandboxed: no file/OS access; a curated "
        "stdlib only (math, statistics, datetime, json, re, itertools, collections, …). For building "
        "a REUSABLE tool use create_tool instead. Args: code, and optional reset."
    )

    class Params(BaseModel):
        code: str = Field(..., description="Python source to execute; the last expression's value is shown")
        reset: bool = Field(False, description="clear this conversation's variables before running")

    def __init__(self, interp: CodeInterpreter, session_id: str):
        self.interp = interp
        self.session_id = session_id

    async def run(self, args: "ExecPythonTool.Params") -> str:
        return await self.interp.run(self.session_id, args.code, reset=args.reset)
