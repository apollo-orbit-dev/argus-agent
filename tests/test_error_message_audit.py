"""Environment failures must name the CONSTRAINT and the ESCAPE; caller-input failures must not change.

The audit behind this file started from a live loop: a created tool's fetch came back as
"<urlopen error Tunnel connection failed>", and the model — told only that something failed and to
"fix the code" — rewrote WORKING code three times and gave up. Nothing it could see said an egress
proxy existed, what the proxy allowed, or that the tool could be re-authored to run host-side.

So each message here is checked on two axes:

  * ENVIRONMENT (a capability off, missing, blocked, gated, or a format only the implementation
    knows): the text must state what the constraint IS and what to do instead. Both, not one.
  * CALLER-INPUT (a bad table name, an empty field, a missing file): self-evident, and deliberately
    LEFT ALONE. Every file touched by the audit also gets a test pinning one of its caller-input
    messages, so a later sweep can't quietly blur the line the whole audit rests on.
"""
import asyncio

import pytest
from pydantic import BaseModel

from engine.sandbox.runtime import ExecResult, FakeRuntime, SandboxUnavailable


class _P(BaseModel):
    pass


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Sandbox egress — the failure that started this
# ---------------------------------------------------------------------------
TUNNEL = "Error fetching atlantic outlook: <urlopen error Tunnel connection failed>"


def test_egress_hint_names_the_constraint_and_the_escape():
    from engine.sandbox.egress_policy import with_egress_hint
    out = with_egress_hint(TUNNEL, "Do X instead.")
    assert TUNNEL in out                                  # appended, never substituted
    assert "egress proxy" in out and "HTTPS" in out       # constraint: what the proxy allows
    assert "public" in out.lower() and "private" in out.lower()
    assert "Do X instead." in out                         # escape: the caller's way out


@pytest.mark.parametrize("text", [
    "<urlopen error Tunnel connection failed>",
    "argus egress proxy: host 'x' is blocked",
    "socket.gaierror: [Errno -3] Temporary failure in name resolution",
    "urllib.error.URLError: <urlopen error [Errno -2] Name or service not known>",
])
def test_egress_signatures_are_recognised(text):
    from engine.sandbox.egress_policy import looks_like_egress_failure
    assert looks_like_egress_failure(text) is True


@pytest.mark.parametrize("text", [
    "42", "KeyError: 'temperature'", "HTTP 404 for that URL", "(no output)",
])
def test_ordinary_output_gets_no_egress_hint(text):
    """The hint must be rare enough to mean something: a normal result or an ordinary bug in the
    tool's own code is NOT an environment failure and must come back untouched."""
    from engine.sandbox.egress_policy import with_egress_hint
    assert with_egress_hint(text, "escape") == text


def _sandboxed_tool(runtime, name="mytool"):
    from engine.experimental.tool_creation import DynamicTool
    return DynamicTool(name, "d", _P, run_fn=None, timeout=30, sandboxed=True,
                       code="x", runtime=runtime)


def test_created_tool_result_carrying_a_tunnel_failure_is_explained():
    """The observed case: the tool CAUGHT the urlopen error and returned it as its result, so
    ok=True. The explanation has to reach the model through the success path too."""
    import json as _json
    fake = FakeRuntime(result=ExecResult(0, _json.dumps({"ok": True, "result": TUNNEL}), ""))
    out = _run(_sandboxed_tool(fake).run(_P()))
    assert "egress proxy" in out and "HTTPS" in out                 # constraint
    assert "sandboxed=false" in out                                 # escape
    assert "https://" in out


def test_created_tool_error_carrying_a_tunnel_failure_is_explained():
    import json as _json
    fake = FakeRuntime(result=ExecResult(0, _json.dumps(
        {"ok": False, "error": "URLError: <urlopen error Tunnel connection failed>"}), ""))
    out = _run(_sandboxed_tool(fake).run(_P()))
    assert "mytool error:" in out                                   # prefix convention preserved
    assert "egress proxy" in out and "sandboxed=false" in out


def test_a_normal_created_tool_result_is_not_annotated():
    import json as _json
    fake = FakeRuntime(result=ExecResult(0, _json.dumps({"ok": True, "result": "42"}), ""))
    assert _run(_sandboxed_tool(fake).run(_P())) == "42"


def test_exec_python_tunnel_failure_is_explained():
    from engine.tools.code_interpreter import CodeInterpreter
    fake = FakeRuntime(result=ExecResult(1, "", "URLError: <urlopen error Tunnel connection failed>"))
    ci = CodeInterpreter(runtime=fake, workspace="default")
    out = _run(ci.run("s", "import urllib.request"))
    assert "egress proxy" in out and "HTTPS" in out                 # constraint
    assert "outside the sandbox" in out                             # escape
    assert "https://" in out


def test_exec_python_ordinary_output_is_not_annotated():
    from engine.tools.code_interpreter import CodeInterpreter
    fake = FakeRuntime(result=ExecResult(0, "7\n", ""))
    assert _run(CodeInterpreter(runtime=fake).run("s", "3+4")).strip() == "7"


# ---------------------------------------------------------------------------
# 2. schedule_task time format — a format only the parser knows
# ---------------------------------------------------------------------------
def test_unreadable_time_states_the_accepted_form_and_the_multi_time_escape():
    """'every day at 9am, 3pm, 9pm, 3am' used to come back as "couldn't read the time '...'" —
    true, useless. One schedule holds one time, and nothing said so."""
    from engine.scheduler import parse_schedule
    spec, err = parse_schedule("every day at 9am, 3pm, 9pm, 3am")
    assert spec is None
    assert "ONE time" in err                                        # constraint
    assert "8:30am" in err and "20:15" in err                       # constraint: the accepted forms
    assert "once per time" in err                                   # escape: four times, four calls


@pytest.mark.parametrize("text", ["at 25pm", "tomorrow at half past", "every monday at noon"])
def test_every_time_parse_failure_carries_the_same_guidance(text):
    from engine.scheduler import parse_schedule
    _, err = parse_schedule(text)
    assert "ONE time" in err and "once per time" in err


def test_schedule_task_surfaces_it_with_the_prefix_intact(tmp_path):
    from engine.scheduler import Scheduler
    from engine.tools.schedule import ScheduleTaskTool

    async def _noop(*a, **k):
        return ""
    sched = Scheduler(str(tmp_path / "jobs.json"), _noop)
    t = ScheduleTaskTool(sched, "sess")
    out = _run(t.run(t.Params(instruction="check the buoys", when="every day at 9am, 3pm, 9pm")))
    assert out.startswith("schedule_task error:")                   # prefix convention preserved
    assert "ONE time" in out and "once per time" in out


# --- CALLER-INPUT in engine/scheduler.py: unchanged ---
def test_an_unrecognised_schedule_shape_is_untouched():
    from engine.scheduler import parse_schedule
    _, err = parse_schedule("whenever the mood strikes")
    assert err == ("couldn't understand that schedule. Try one of: 'in 30 minutes', "
                   "'every day at 8am', 'every hour', 'every 15 minutes', "
                   "'every monday at 9am', 'tomorrow at 7pm', or 'at 3pm'.")


# ---------------------------------------------------------------------------
# 3. The container sandbox being down
# ---------------------------------------------------------------------------
class _DeadRuntime(FakeRuntime):
    """available() says yes, exec() dies — the sandbox that fails mid-call rather than up front."""
    def exec(self, name, argv, *, stdin="", timeout=120.0, run_id=""):
        raise SandboxUnavailable("container exited")


def test_exec_python_sandbox_unavailable_says_retrying_wont_help_and_what_to_do():
    from engine.tools.code_interpreter import CodeInterpreter
    out = _run(CodeInterpreter(runtime=_DeadRuntime()).run("s", "1+1"))
    assert out.startswith("exec_python error:")                     # prefix convention preserved
    assert "runs nowhere else" in out and "retrying won't help" in out   # constraint
    assert "Settings > Sandbox" in out and "calculator" in out           # escape


def test_created_tool_sandbox_unavailable_offers_the_host_side_escape():
    out = _run(_sandboxed_tool(_DeadRuntime()).run(_P()))
    assert "container sandbox is unavailable" in out                # constraint
    assert "retrying won't help" in out
    assert "Settings > Sandbox" in out and "sandboxed=false" in out  # escape


# ---------------------------------------------------------------------------
# 4. Tool composition switched off for a reloaded tool
# ---------------------------------------------------------------------------
def test_call_tool_without_a_registry_says_it_is_permanent_and_how_to_restore_it():
    from engine.experimental.tool_creation import _make_call_tool
    out = _make_call_tool(None, None)("weather", {})
    assert out.startswith("CALL_TOOL error:")                       # prefix convention preserved
    assert "no tool registry" in out and "no matter how the call is written" in out   # constraint
    assert "directly in your turn" in out and "create_tool" in out                    # escape


# --- CALLER-INPUT in engine/experimental/tool_creation.py: unchanged ---
def test_calling_a_tool_that_does_not_exist_is_untouched():
    from engine.experimental.tool_creation import _make_call_tool
    from engine.tools.base import ToolRegistry
    out = _make_call_tool(ToolRegistry(), None)("nope", {})
    assert out == "CALL_TOOL error: no tool named 'nope'. Available: "


# ---------------------------------------------------------------------------
# 5. ask_data with no model behind it
# ---------------------------------------------------------------------------
def _store(tmp_path):
    from engine.tools.tables import TableStore
    s = TableStore(str(tmp_path / "t.db"))
    s.create_table("sales", ["date:date:key", "revenue:int"])
    s.insert("sales", {"date": "2026-07-01", "revenue": 90})
    return s


def _ask(store, aux, q="what is my total revenue?"):
    from engine.tools.tables import AskDataTool
    t = AskDataTool(store, aux)
    return _run(t.run(t.Params(question=q)))


def test_ask_data_with_no_model_points_at_the_deterministic_alternative(tmp_path):
    def _boom():
        raise RuntimeError("no chat role configured")
    out = _ask(_store(tmp_path), _boom)
    assert out.startswith("ask_data error:")                        # prefix convention preserved
    assert "needs a model" in out                                   # constraint
    assert "query_table" in out and "list_tables" in out            # escape


def test_ask_data_with_an_unreachable_model_points_at_the_same_alternative(tmp_path):
    class _Dead:
        async def chat(self, *a, **k):
            raise ConnectionError("connection refused")
    out = _ask(_store(tmp_path), lambda: _Dead())
    assert "could not reach the model" in out and "needs it to" in out   # constraint
    assert "query_table" in out and "SELECT" in out                      # escape


# --- CALLER-INPUT in engine/tools/tables.py: unchanged ---
def test_querying_a_table_that_does_not_exist_is_untouched(tmp_path):
    from engine.tools.tables import QueryTableTool
    t = QueryTableTool(_store(tmp_path))
    out = _run(t.run(t.Params(sql="SELECT * FROM nope")))
    assert out.startswith("query_table error:")
    assert out.endswith("(check the table/column names — see list_tables)")


# ---------------------------------------------------------------------------
# 6. The PDF renderer: a config flag that is ON with its optional library absent
# ---------------------------------------------------------------------------
def _no_renderer(monkeypatch):
    import engine.tools.pdf as pdf

    def _raise():
        raise pdf.RendererMissing("No module named 'weasyprint'")
    monkeypatch.setattr(pdf, "_weasy_html", _raise)


def test_make_pdf_without_weasyprint_says_the_html_is_not_the_problem(tmp_path, monkeypatch):
    from engine.tools.files import FileWorkspace
    from engine.tools.pdf import MakePdfTool
    _no_renderer(monkeypatch)
    t = MakePdfTool(FileWorkspace(str(tmp_path / "ws")))
    out = _run(t.run(t.Params(title="R", html="<h1>fine html</h1>")))
    assert out.startswith("make_pdf error:")                        # prefix convention preserved
    assert "not installed" in out and "isn't the problem" in out    # constraint
    assert "write_file" in out and "argus[pdf]" in out              # escape


def test_convert_to_pdf_without_weasyprint_says_the_same(tmp_path, monkeypatch):
    from engine.tools.files import FileWorkspace
    from engine.tools.pdf import ConvertToPdfTool
    ws = FileWorkspace(str(tmp_path / "ws"))
    ws.write_text("note.md", "# hi")
    _no_renderer(monkeypatch)
    t = ConvertToPdfTool(ws)
    out = _run(t.run(t.Params(name="note.md")))
    assert out.startswith("convert_to_pdf error:")
    assert "not installed" in out                                   # constraint
    assert "write_file" in out and "argus[pdf]" in out              # escape


# --- CALLER-INPUT in engine/tools/pdf.py: unchanged ---
def test_empty_html_is_untouched(tmp_path):
    from engine.tools.files import FileWorkspace
    from engine.tools.pdf import MakePdfTool
    t = MakePdfTool(FileWorkspace(str(tmp_path / "ws")))
    out = _run(t.run(t.Params(title="R", html="   ")))
    assert out == "make_pdf error: html is empty — write the document content."


# ---------------------------------------------------------------------------
# 7. OCR: advertised by read_document, but an opt-in extra
# ---------------------------------------------------------------------------
def test_a_scanned_page_without_ocr_installed_says_so_and_what_to_ask_for():
    from engine.tools.documents import _ocr_note
    note = _ocr_note(ModuleNotFoundError("No module named 'pytesseract'"))
    assert "OCR is NOT installed" in note and "cannot be extracted here at all" in note  # constraint
    assert "text version" in note and "argus[ocr]" in note                               # escape


def test_a_scanned_pdf_page_actually_carries_the_explanation(tmp_path, monkeypatch):
    """End-to-end through _read_pdf, not just the helper: the note has to be WIRED to the page
    that failed, or the model still only sees the raw ImportError."""
    fitz = pytest.importorskip("fitz")
    import engine.tools.documents as docs
    p = str(tmp_path / "scan.pdf")
    doc = fitz.open()
    doc.new_page()                                  # an image-only page: no extractable text
    doc.save(p)
    doc.close()
    monkeypatch.setattr(docs, "_ocr_image",
                        lambda _png: (_ for _ in ()).throw(
                            ModuleNotFoundError("No module named 'pytesseract'")))
    out = docs.extract_document(p)
    assert "OCR is NOT installed" in out and "argus[ocr]" in out


def test_a_missing_tesseract_binary_is_treated_as_the_same_gap():
    from engine.tools.documents import _ocr_note
    note = _ocr_note(RuntimeError("tesseract is not installed or it's not in your PATH"))
    assert "OCR is NOT installed" in note and "argus[ocr]" in note


# --- CALLER-INPUT in engine/tools/documents.py: unchanged ---
def test_a_genuinely_broken_page_is_untouched():
    from engine.tools.documents import _ocr_note
    assert _ocr_note(ValueError("bad pixmap")) == "no text and OCR failed: bad pixmap"


def test_reading_a_file_that_is_not_in_the_workspace_is_untouched(tmp_path):
    from engine.tools.documents import ReadDocumentTool
    from engine.tools.files import FileWorkspace
    t = ReadDocumentTool(FileWorkspace(str(tmp_path / "ws")))
    out = _run(t.run(t.Params(name="ghost.pdf")))
    assert out == "read_document: no file 'ghost.pdf'. Files in workspace: (empty)."


# --- CALLER-INPUT in engine/sandbox/egress_policy.py: unchanged ---
def test_a_non_http_url_is_still_refused_with_the_same_plain_reason():
    from engine.sandbox.egress_policy import url_allowed
    ok, reason = url_allowed("ftp://example.com/x")
    assert ok is False and reason == "scheme 'ftp' is not http(s)"
