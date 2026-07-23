import asyncio

import pytest

from pydantic import BaseModel

from engine.experimental.tool_creation import (
    CreateToolTool, DynamicTool, ToolValidationError, _compile_run, _perturbations,
    build_params_model, scan_ast,
)
from engine.tools.base import Tool, ToolRegistry


# ---- tool composition (CALL_TOOL) ----

def test_call_tool_composition():
    """A created tool can call an existing registered tool via CALL_TOOL and use its result."""
    reg = ToolRegistry()

    class Echo(Tool):
        name = "echo"
        description = "echo"

        class Params(BaseModel):
            x: str

        async def run(self, args):
            return "ECHO:" + args.x

    reg.register(Echo())
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'got ' + CALL_TOOL('echo', {'x': args['v']})\n"
    out = asyncio.run(ct.run(ct.Params(name="wrap", description="d",
        parameters={"v": {"type": "string"}}, code=code, test_args={"v": "hi"})))
    assert "verified" in out.lower()
    tool = reg.get("wrap")
    assert asyncio.run(tool.run(tool.Params(v="hi"))) == "got ECHO:hi"


def test_structured_input_types():
    """array/object params must accept real list/dict values (they used to fall back to str,
    breaking data-transform tools that take list-of-dicts input)."""
    M = build_params_model("t", {"rows": {"type": "array"}, "opts": {"type": "object"}})
    assert M.model_fields["rows"].annotation is list
    assert M.model_fields["opts"].annotation is dict
    inst = M(rows=[{"a": 1}, {"a": 3}], opts={"k": "v"})
    assert inst.rows[0]["a"] == 1 and inst.opts["k"] == "v"


def test_create_tool_with_list_of_dicts():
    """End-to-end: a data-transform tool taking a list of dicts test-runs instead of failing."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = ("def run(args):\n"
            "    from collections import defaultdict\n"
            "    sums = defaultdict(list)\n"
            "    for r in args['data']:\n"
            "        sums[r['k']].append(r['v'])\n"
            "    return str({k: sum(v)/len(v) for k, v in sums.items()})\n")
    out = asyncio.run(ct.run(ct.Params(
        name="avg_by_key", description="average v per k",
        parameters={"data": {"type": "array"}}, code=code,
        test_args={"data": [{"k": "A", "v": 90}, {"k": "A", "v": 92}, {"k": "B", "v": 88}]})))
    assert "verified" in out.lower()
    tool = reg.get("avg_by_key")
    res = asyncio.run(tool.run(tool.Params(data=[{"k": "A", "v": 10}, {"k": "A", "v": 20}])))
    assert "15.0" in res


def test_bare_tool_name_composition():
    """The intuitive form the model actually writes: call an existing tool BY NAME as a function."""
    reg = ToolRegistry()

    class Echo(Tool):
        name = "echo"
        description = "echo"

        class Params(BaseModel):
            x: str

        async def run(self, args):
            return "ECHO:" + args.x

    reg.register(Echo())
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'got ' + echo({'x': args['v']})\n"   # bare name, not CALL_TOOL
    out = asyncio.run(ct.run(ct.Params(name="wrap2", description="d",
        parameters={"v": {"type": "string"}}, code=code, test_args={"v": "hi"})))
    assert "verified" in out.lower()
    tool = reg.get("wrap2")
    assert asyncio.run(tool.run(tool.Params(v="hi"))) == "got ECHO:hi"


def test_recreate_replaces_created_tool():
    """Re-creating a CREATED tool with the same name replaces it — so the model can FIX its tool."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    asyncio.run(ct.run(ct.Params(name="v", description="d", parameters={"n": {"type": "integer"}},
        code="def run(args):\n    return str(args['n'])", test_args={"n": 1})))
    out = asyncio.run(ct.run(ct.Params(name="v", description="d2", parameters={"n": {"type": "integer"}},
        code="def run(args):\n    return 'now ' + str(args['n'])", test_args={"n": 1})))
    assert "verified" in out.lower()
    assert asyncio.run(reg.get("v").run(reg.get("v").Params(n=5))) == "now 5"


def test_cannot_replace_builtin_tool():
    reg = ToolRegistry()

    class Base(Tool):
        name = "calculator"
        description = "c"

        class Params(BaseModel):
            x: int = 0

        async def run(self, args):
            return "base"

    reg.register(Base())   # a non-created (built-in) tool
    ct = CreateToolTool(reg, allow_network=False)
    out = asyncio.run(ct.run(ct.Params(name="calculator", description="d", parameters={},
        code="def run(args):\n    return 'hijacked'", test_args={})))
    assert "built-in" in out.lower() and "can't be replaced" in out.lower()
    assert asyncio.run(reg.get("calculator").run(reg.get("calculator").Params())) == "base"


def test_cannot_recreate_gated_off_builtin():
    # crawl_site is a built-in that is absent from the registry when Firecrawl isn't configured.
    # Its name is still reserved, so the model can't shadow it with a sandbox reimplementation —
    # it's steered to configure the dependency instead.
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False,
                        reserved_names={"crawl_site", "web_search"})
    out = asyncio.run(ct.run(ct.Params(name="crawl_site", description="d", parameters={},
        code="def run(args):\n    return 'fake'", test_args={})))
    assert "built-in" in out.lower()
    assert reg.get("crawl_site") is None   # nothing was registered


def test_cannot_recreate_exec_python_when_fail_closed():
    """Finding 2: in the fail-closed state (sandbox on, runtime down), exec_python is deliberately
    absent from the registry — create_tool must not treat that absence as "free to take" and hand
    the model a host-side, AST-scanned replacement under the same name. That would be the exact
    silent downgrade fail-closed exists to prevent, just triggered by the model itself via
    create_tool instead of by Engine. GATED_BUILTIN_NAMES (engine/engine.py) is what reserves it."""
    from engine.engine import GATED_BUILTIN_NAMES

    assert "exec_python" in GATED_BUILTIN_NAMES

    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False, reserved_names=GATED_BUILTIN_NAMES)
    out = asyncio.run(ct.run(ct.Params(name="exec_python", description="d", parameters={},
        code="def run(args):\n    return 'fake sandbox'", test_args={})))
    assert "built-in" in out.lower()
    assert reg.get("exec_python") is None   # nothing was registered


def test_reserved_name_allowed_once_dependency_registers_it():
    # If the real built-in IS registered (dependency configured), the normal built-in guard applies
    # and reserved_names doesn't double-fire or change the message.
    reg = ToolRegistry()

    class Crawl(Tool):
        name = "crawl_site"
        description = "real"

        class Params(BaseModel):
            url: str = ""

        async def run(self, args):
            return "real"

    reg.register(Crawl())
    ct = CreateToolTool(reg, allow_network=False, reserved_names={"crawl_site"})
    out = asyncio.run(ct.run(ct.Params(name="crawl_site", description="d", parameters={},
        code="def run(args):\n    return 'fake'", test_args={})))
    assert "can't be replaced" in out.lower()


def test_call_tool_unknown_tool_returns_error_string():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return CALL_TOOL('nope', {})\n"
    out = asyncio.run(ct.run(ct.Params(name="t", description="d", parameters={},
                                       code=code, test_args={})))
    # test run returns a CALL_TOOL error string (not a crash); flagged as no-data is fine
    assert reg.get("t") is not None


# ---- hardcode / ignores-input detector ----

def test_perturbations():
    v = _perturbations({"date": "2026-07-11"})
    assert len(v) == 2 and {x["date"] for x in v} == {"2001-09-09", "2001-09-11"}   # 2 distinct
    assert _perturbations({"n": 5})[0]["n"] == 105
    assert _perturbations({"city": "Paris"}) == []   # no date/number -> nothing safe to perturb
    assert _perturbations({}) == []


def test_delete_tool(tmp_path):
    """delete_tool removes a created tool from the registry, disk, and the live sink; built-ins
    are protected (the 'youtube tools can't be deleted' gap)."""
    from engine.experimental.tool_creation import DeleteToolTool
    sink = []
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False, persist_dir=str(tmp_path), created_sink=sink)
    asyncio.run(ct.run(ct.Params(name="temp", description="d", parameters={},
                                 code="def run(args):\n    return 'x'", test_args={})))
    assert reg.get("temp") is not None and (tmp_path / "temp.json").exists() and sink

    dt = DeleteToolTool(reg, persist_dir=str(tmp_path), created_sink=sink)
    out = asyncio.run(dt.run(dt.Params(name="temp")))
    assert "deleted" in out.lower()
    assert reg.get("temp") is None and not (tmp_path / "temp.json").exists() and sink == []
    assert "no tool" in asyncio.run(dt.run(dt.Params(name="temp"))).lower()   # already gone

    class Base(Tool):
        name = "calculator"
        description = "c"

        class Params(BaseModel):
            pass

        async def run(self, args):
            return "base"
    reg.register(Base())
    assert "built-in" in asyncio.run(dt.run(dt.Params(name="calculator"))).lower()   # protected


def test_inspect_tool_returns_source(tmp_path):
    """The model can read a created tool's code before revising it (so it copies the working
    auth/library pattern instead of reinventing)."""
    from engine.experimental.tool_creation import InspectToolTool
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False, persist_dir=str(tmp_path))
    asyncio.run(ct.run(ct.Params(name="mytool", description="does a thing",
        parameters={"n": {"type": "integer"}},
        code="def run(args):\n    return str(args['n'])", test_args={"n": 1})))
    it = InspectToolTool(str(tmp_path))
    out = asyncio.run(it.run(it.Params(name="mytool")))
    assert "mytool" in out and "def run(args)" in out and "does a thing" in out
    assert "no created tool" in asyncio.run(it.run(it.Params(name="nope"))).lower()


def test_created_tool_added_to_sink_for_later_turns():
    """The 'unknown tool' fix: a new tool is added to the engine's live list so a LATER turn's
    registry includes it — no restart needed."""
    sink = []
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False, created_sink=sink)
    code = "def run(args):\n    return str(args['a'] + args['b'])"
    out = asyncio.run(ct.run(ct.Params(name="adder", description="d",
        parameters={"a": {"type": "integer"}, "b": {"type": "integer"}},
        code=code, test_args={"a": 2, "b": 3})))
    assert "verified" in out.lower()
    assert [t.name for t in sink] == ["adder"]      # now visible to future turns


def test_hardcode_detector_flags_ignored_date():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'Score: 62, Duration: 6.5h'\n"   # ignores the date arg
    out = asyncio.run(ct.run(ct.Params(name="fake_report", description="d",
        parameters={"date": {"type": "string"}}, code=code, test_args={"date": "2026-07-11"})))
    assert "hardcoded" in out.lower() or "ignores" in out.lower()
    assert "verified" not in out.lower()


def test_hardcode_detector_passes_tool_that_uses_input():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'Report for ' + args['date']\n"
    out = asyncio.run(ct.run(ct.Params(name="real_report", description="d",
        parameters={"date": {"type": "string"}}, code=code, test_args={"date": "2026-07-11"})))
    assert "verified" in out.lower() and "hardcoded" not in out.lower()


# ---- AST safety scan ----

def test_scan_allows_safe_code():
    scan_ast("import math\ndef run(args):\n    return str(math.sqrt(args['x']))", allow_network=False)


@pytest.mark.parametrize("code", [
    "import os\ndef run(args): return os.listdir('.')",
    "def run(args): return open('/etc/passwd').read()",
    "def run(args): return eval('1+1')",
    "def run(args): return __import__('os').system('x')",
    "def run(args): return (1).__class__.__bases__",
    "def run(args): return getattr(args, '__class__')",
    "import subprocess\ndef run(args): return subprocess.run(['ls'])",
])
def test_scan_rejects_dangerous(code):
    with pytest.raises(ToolValidationError):
        scan_ast(code, allow_network=False)


def test_scan_network_gated():
    src = "import httpx\ndef run(args): return httpx.get(args['url']).text"
    with pytest.raises(ToolValidationError):
        scan_ast(src, allow_network=False)
    scan_ast(src, allow_network=True)  # allowed when network enabled


# ---- compile + params ----

def test_compile_and_run():
    fn = _compile_run("def run(args):\n    return args['a'] + args['b']", allow_network=False)
    assert fn({"a": 2, "b": 3}) == 5


def test_compile_requires_run():
    with pytest.raises(ToolValidationError):
        _compile_run("def other(args): return 1", allow_network=False)


def test_rejects_async_run():
    # observed real failure: small model wrote `async def run` + `await httpx.get`
    with pytest.raises(ToolValidationError):
        _compile_run("async def run(args):\n    return 'x'", allow_network=True)


def test_build_params_model():
    M = build_params_model("t", {"city": {"type": "string", "description": "c"},
                                 "n": {"type": "integer", "required": False, "default": 5}})
    m = M(city="NYC")
    assert m.city == "NYC" and m.n == 5


def test_build_params_model_accepts_json_schema():
    # small models sometimes pass a full JSON Schema instead of the flat map
    schema = {"type": "object",
              "properties": {"year": {"type": "integer"}, "month": {"type": "integer"},
                             "day": {"type": "integer"}},
              "required": ["year", "month", "day"]}
    M = build_params_model("d", schema)
    m = M(year=1776, month=7, day=4)
    assert m.year == 1776 and m.month == 7 and m.day == 4
    # fields are the real params, NOT the schema keys
    assert "type" not in M.model_fields and "properties" not in M.model_fields


# ---- DynamicTool ----

def test_dynamic_tool_runs():
    fn = _compile_run("def run(args):\n    return f\"sum={args['a']+args['b']}\"", False)
    M = build_params_model("adder", {"a": {"type": "integer"}, "b": {"type": "integer"}})
    tool = DynamicTool("adder", "adds", M, fn)
    assert asyncio.run(tool.run(M(a=4, b=5))) == "sum=9"


def test_dynamic_tool_catches_exceptions():
    fn = _compile_run("def run(args):\n    return 1/0", False)
    M = build_params_model("boom", {})
    tool = DynamicTool("boom", "x", M, fn)
    out = asyncio.run(tool.run(M()))
    assert "error" in out.lower() and "zerodivision" in out.lower()


# ---- CreateToolTool end-to-end ----

def test_create_tool_registers_and_is_callable():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    import math\n    return str(math.factorial(args['n']))"
    out = asyncio.run(ct.run(ct.Params(
        name="factorial", description="n!",
        parameters={"n": {"type": "integer", "description": "the number"}}, code=code)))
    assert "created" in out.lower()
    # now the new tool exists in the registry and works
    new = reg.get("factorial")
    assert new is not None
    assert asyncio.run(new.run(new.Params(n=5))) == "120"


def test_create_tool_reports_error_and_records():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    # a forbidden builtin -> generic validation error the model can act on
    out = asyncio.run(ct.run(ct.Params(
        name="bad", description="x", parameters={},
        code="def run(args): return open('/etc/passwd').read()")))
    assert "error" in out.lower() and "open" in out.lower()
    assert ct.created[-1]["ok"] is False and ct.created[-1]["error"]
    assert reg.get("bad") is None


def test_test_run_flags_empty_result_instead_of_verifying():
    """A no-data return that doesn't crash must NOT pass as 'verified' — it should be flagged
    as a likely parsing bug (a created-tool bug: graceful 'no data' masked wrong parsing)."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'No data found for the specified date range.'"
    out = asyncio.run(ct.run(ct.Params(name="empty_tool", description="d", parameters={},
                                       code=code, test_args={})))
    assert "verified" not in out.lower()          # NOT falsely verified
    assert ("no data" in out.lower() or "empty" in out.lower())
    assert "raw" in out.lower() and "pars" in out.lower()   # steered to inspect raw + fix parsing
    assert reg.get("empty_tool") is not None      # still registered (works structurally)


def test_test_run_flags_error_shaped_output():
    """A tool that caught an exception and RETURNED an error string still isn't working — it
    must not pass as 'verified' (a broken data tool did exactly this)."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'Error fetching data for 2026-07-11: Expecting value'\n"
    out = asyncio.run(ct.run(ct.Params(name="brk", description="d", parameters={},
                                       code=code, test_args={})))
    assert "verified" not in out.lower()


def test_test_run_verifies_on_real_output():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return 'Report: 6.5 units, sub 1.1, score 62'"
    out = asyncio.run(ct.run(ct.Params(name="real_tool", description="d", parameters={},
                                       code=code, test_args={})))
    assert "verified" in out.lower()
    assert "no data" not in out.lower()
    assert reg.get("real_tool") is not None


def test_created_tool_can_introspect_with_dir():
    """A tool can DISCOVER a library's API surface (dir/hasattr) so the model can find the right
    methods instead of guessing — the enabler for self-correcting a data gap."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = ("import math\n"
            "def run(args):\n"
            "    methods = [m for m in dir(math) if not m.startswith('_')]\n"
            "    return 'has sqrt: ' + str('sqrt' in methods) + ', hasattr: ' + str(hasattr(math, 'pi'))\n")
    out = asyncio.run(ct.run(ct.Params(name="probe", description="d", parameters={},
                                       code=code, test_args={})))
    assert "verified" in out.lower()
    assert asyncio.run(reg.get("probe").run(reg.get("probe").Params())) == "has sqrt: True, hasattr: True"


def test_created_tool_can_use_strptime():
    """datetime.strptime lazily imports the private _strptime module — the sandbox must permit
    that internal import (regression: it silently broke all date parsing in created tools,
    which is what made a created tool fail on historical dates)."""
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = ("from datetime import datetime\n"
            "def run(args):\n"
            "    return datetime.strptime(args['d'], '%Y-%m-%d').strftime('%A')\n")
    out = asyncio.run(ct.run(ct.Params(name="dow", description="day of week",
        parameters={"d": {"type": "string"}}, code=code, test_args={"d": "2026-02-01"})))
    assert "verified" in out.lower()
    tool = reg.get("dow")
    assert asyncio.run(tool.run(tool.Params(d="2026-02-01"))) == "Sunday"   # 2026-02-01 is a Sunday


def test_guarded_import_permits_safe_stdlib_blocks_dangerous():
    from engine.experimental.tool_creation import _make_guarded_import
    imp = _make_guarded_import({"math"})
    assert imp("math") is not None            # tool's allowed module
    assert imp("_strptime") is not None       # safe internal helper now permitted
    for bad in ("os", "sys", "subprocess", "socket", "_socket", "importlib"):
        with pytest.raises(ImportError):
            imp(bad)                          # dangerous modules still blocked at runtime


def test_create_tool_stdlib_import_blocked_and_recorded():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    out = asyncio.run(ct.run(ct.Params(
        name="bad", description="x", parameters={},
        code="import os\ndef run(args): return os.getcwd()")))
    assert "standard library" in out.lower()      # clear steer, not a blank reject
    assert ct.created[-1]["ok"] is False and ct.created[-1]["error"]
    assert reg.get("bad") is None


def test_auto_testrun_registers_working_tool():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return str(args['a'] * args['b'])"
    out = asyncio.run(ct.run(ct.Params(
        name="mult", description="multiply", parameters={"a": {"type": "integer"}, "b": {"type": "integer"}},
        code=code, test_args={"a": 6, "b": 7})))
    assert "verified" in out.lower() and "42" in out
    assert reg.get("mult") is not None


def test_auto_testrun_rejects_broken_tool_and_leaves_name_free():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False)
    code = "def run(args):\n    return str(args['x'] + undefined_name)"
    out = asyncio.run(ct.run(ct.Params(
        name="broken", description="x", parameters={"x": {"type": "integer"}},
        code=code, test_args={"x": 1})))
    assert "not created" in out.lower() and "test run failed" in out.lower()
    assert reg.get("broken") is None  # name stays free for a corrected retry
    # a corrected version with the SAME name now registers
    out2 = asyncio.run(ct.run(ct.Params(
        name="broken", description="x", parameters={"x": {"type": "integer"}},
        code="def run(args):\n    return str(args['x'] + 1)", test_args={"x": 1})))
    assert "verified" in out2.lower() and reg.get("broken") is not None


def test_created_tool_persists_and_reloads(tmp_path):
    from engine.experimental.tool_creation import load_persisted_tools
    reg = ToolRegistry()
    ct = CreateToolTool(reg, allow_network=False, persist_dir=str(tmp_path))
    asyncio.run(ct.run(ct.Params(
        name="doubler", description="double n", parameters={"n": {"type": "integer"}},
        code="def run(args):\n    return str(args['n'] * 2)", test_args={"n": 5})))
    assert (tmp_path / "doubler.json").exists()
    # reload from disk into fresh DynamicTools
    reloaded = load_persisted_tools(str(tmp_path))
    tool = next(t for t in reloaded if t.name == "doubler")
    assert asyncio.run(tool.run(tool.Params(n=21))) == "42"


def test_validate_only_does_not_register():
    reg = ToolRegistry()
    ct = CreateToolTool(reg, validate_only=True)
    out = asyncio.run(ct.run(ct.Params(
        name="v", description="x", parameters={}, code="def run(args):\n    return 'ok'")))
    assert "validated ok" in out.lower()
    assert reg.get("v") is None


# ---- stronger output-verification (data-tool rewrite bugs) ----
from engine.experimental.tool_creation import _looks_like_no_data, _discarded_tool_calls


def test_no_data_catches_placeholder_report():
    broken = "Score 79\n- Duration: 0h\n- Sub: 0 (0%)\n- Rate: TBD\nNote: Unable to parse the data."
    assert _looks_like_no_data(broken)


def test_no_data_ignores_legit_output_with_one_zero():
    assert not _looks_like_no_data("Nashville: 82F, sunny. Rain chance 0% today.")
    assert not _looks_like_no_data("The answer is 42.")


def test_discarded_tool_call_detected():
    code = "def run(args):\n    ascii_chart({'chart_type':'composition','data':d})\n    return 'r'"
    assert _discarded_tool_calls(code, {"ascii_chart"}) == ["ascii_chart"]


def test_discarded_call_tool_detected():
    code = "def run(args):\n    CALL_TOOL('make_chart', {})\n    return 'r'"
    assert _discarded_tool_calls(code, set()) == ["make_chart"]


def test_captured_tool_result_not_flagged():
    code = "def run(args):\n    c = ascii_chart({'chart_type':'line','data':d})\n    return 'r\\n' + c"
    assert _discarded_tool_calls(code, {"ascii_chart"}) == []


async def test_created_tool_can_compose_a_builtin(tmp_path):
    """The whole point of the geocode built-in: a created tool must be able to CALL it rather than
    re-implement geocoding against the raw API. Every registered tool name is injected as a plain
    callable into created-tool globals at call time, and the tool's JSON output must survive
    json.loads() in the caller -- the two halves that make composition actually usable.

    Live session evidence for why this is pinned: with a prose-returning geocode, the model wrote
    `json.loads(geocode(...))`, it raised, the tool returned "Could not find location", and the
    model concluded composition was impossible and hardcoded a latitude instead.
    """
    import json as _json

    import httpx
    from pydantic import BaseModel

    from engine.experimental.tool_creation import DynamicTool, _compile_run
    from engine.tools.base import ToolRegistry
    from engine.tools.geocode import GeocodeTool

    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, json={"results": [
            {"name": "Milton", "admin1": "Florida", "country": "United States",
             "latitude": 30.63241, "longitude": -87.03969, "timezone": "America/Chicago"}]}))
        real_init(self, *a, **kw)

    httpx.AsyncClient.__init__ = fake_init
    try:
        reg = ToolRegistry()
        reg.register(GeocodeTool())

        class P(BaseModel):
            location: str

        code = ("def run(args):\n"
                "    import json\n"
                "    place = json.loads(geocode({'location': args['location']}))\n"
                "    return f\"{place['latitude']},{place['longitude']}\"\n")
        t = DynamicTool("compose_probe", "probe", P, _compile_run(code, allow_network=True),
                        timeout=30)
        t.registry = reg
        assert await t.run(P(location="Milton, FL")) == "30.63241,-87.03969"
    finally:
        httpx.AsyncClient.__init__ = real_init


# ---- sandboxed DynamicTool (stage 2b: container execution path) ----

import json as _json

from engine.sandbox.runtime import ExecResult, FakeRuntime


class _P(BaseModel):
    a: int = 0


async def test_sandboxed_tool_runs_via_the_runtime():
    """A sandboxed tool ships {code, args} to runner.py in the container and parses the JSON back."""
    fake = FakeRuntime(result=ExecResult(0, _json.dumps({"ok": True, "result": "42"}), ""))
    t = DynamicTool("mytool", "d", _P, run_fn=None, timeout=30, sandboxed=True,
                    code="def run(args): return '42'", runtime=fake, workspace="default")
    out = await t.run(_P(a=1))
    assert out == "42"
    # it called exec on the right workspace, running runner.py, with the code+args on stdin
    name, argv = fake.calls[0]
    assert name == "default"
    assert argv == ["python", "/opt/argus/runner.py"]
    stdin = fake.exec_stdin[0]
    payload = _json.loads(stdin)
    assert payload["code"] == "def run(args): return '42'" and payload["args"] == {"a": 1}


async def test_sandboxed_tool_surfaces_a_container_error():
    fake = FakeRuntime(result=ExecResult(0, _json.dumps(
        {"ok": False, "error": "ValueError: boom", "traceback": "..."}), ""))
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=fake)
    out = await t.run(_P())
    assert "mytool error" in out and "ValueError: boom" in out


async def test_sandboxed_tool_refuses_when_the_sandbox_is_unavailable():
    """Fail closed: authored assuming the full stdlib, so it must NOT run host-side when off."""
    fake = FakeRuntime(available_=False)
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=fake)
    out = await t.run(_P())
    assert "sandbox" in out.lower() and "mytool" in out
    # and it did NOT execute anything
    assert fake.calls == []


async def test_sandboxed_tool_refuses_when_runtime_is_none():
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=None)
    out = await t.run(_P())
    assert "sandbox" in out.lower()


async def test_sandboxed_tool_reports_a_container_timeout():
    fake = FakeRuntime(result=ExecResult(124, "", "", timed_out=True))
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=fake, timeout=30)
    out = await t.run(_P())
    assert "timed out" in out.lower()


async def test_sandboxed_tool_errors_cleanly_on_unserialisable_args():
    """json.dumps({"code", "args"}) must be inside the guarded region: a Params model whose
    model_dump() produces a non-JSON-serialisable value must return a clean tool-error string,
    not raise a TypeError into the loop."""
    fake = FakeRuntime(result=ExecResult(0, _json.dumps({"ok": True, "result": "42"}), ""))
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=fake)
    bad_args = _P()
    object.__setattr__(bad_args, "model_dump", lambda *a, **k: {"x": object()})
    out = await t.run(bad_args)
    assert out == "mytool error: TypeError: Object of type object is not JSON serializable"
    # and it never got as far as calling exec
    assert fake.calls == []


async def test_sandboxed_tool_surfaces_stderr_when_stdout_is_empty():
    """When the container dies and produces no stdout, the tool-error must include stderr
    (the real reason) rather than silently falling back to a generic 'unknown error'."""
    fake = FakeRuntime(result=ExecResult(1, "", "Traceback: container OOM-killed"))
    t = DynamicTool("mytool", "d", _P, sandboxed=True, code="x", runtime=fake)
    out = await t.run(_P())
    assert "mytool error" in out
    assert "container OOM-killed" in out


# ---- create_tool: sandboxed authoring (Task 3) ----

import tempfile

from engine.experimental.tool_creation import CreateToolTool, load_persisted_tools
from engine.sandbox.runtime import ExecResult, FakeRuntime
from engine.tools.base import ToolRegistry


def _ct(**kw):
    return CreateToolTool(ToolRegistry(), **kw)


def test_flag_defaults_true_when_sandbox_enabled_and_available():
    ct = _ct(sandbox_enabled=True, sandbox_runtime=FakeRuntime(available_=True))
    assert ct._resolve_sandboxed(None) is True


def test_flag_defaults_false_when_sandbox_off():
    ct = _ct(sandbox_enabled=False, sandbox_runtime=FakeRuntime(available_=True))
    assert ct._resolve_sandboxed(None) is False


def test_flag_defaults_false_when_sandbox_enabled_but_unavailable():
    ct = _ct(sandbox_enabled=True, sandbox_runtime=FakeRuntime(available_=False))
    assert ct._resolve_sandboxed(None) is False


def test_explicit_flag_is_honoured():
    ct = _ct(sandbox_enabled=True, sandbox_runtime=FakeRuntime(available_=True))
    assert ct._resolve_sandboxed(False) is False
    ct2 = _ct(sandbox_enabled=False, sandbox_runtime=None)
    assert ct2._resolve_sandboxed(True) is True


async def test_creating_a_sandboxed_tool_test_runs_in_the_container_and_persists_the_flag():
    persist = tempfile.mkdtemp()
    fake = FakeRuntime(result=ExecResult(0, '{"ok": true, "result": "ok"}', ""))
    ct = _ct(persist_dir=persist, sandbox_enabled=True, sandbox_runtime=fake,
             timeout=30)
    out = await ct.run(CreateToolTool.Params(
        name="fulltool", description="d", parameters={"a": {"type": "integer"}},
        code="import os\ndef run(args):\n    return 'ok'", test_args={"a": 1}, sandboxed=True))
    assert "created" in out.lower()
    # the test-run went through the container (runner.py), not a host-side AST compile
    assert fake.calls and fake.calls[0][1] == ["python", "/opt/argus/runner.py"]
    # persisted with sandboxed: true
    import json
    import os
    m = json.load(open(os.path.join(persist, "fulltool.json")))
    assert m["sandboxed"] is True and "import os" in m["code"]


async def test_sandboxed_authoring_skips_the_ast_scan():
    """`import os` would be rejected host-side; under sandboxed=true it must be allowed (the point)."""
    fake = FakeRuntime(result=ExecResult(0, '{"ok": true, "result": "ok"}', ""))
    ct = _ct(sandbox_enabled=True, sandbox_runtime=fake, timeout=30)
    out = await ct.run(CreateToolTool.Params(
        name="ostool", description="d", parameters={},
        code="import os\ndef run(args):\n    return os.getcwd()", test_args={}, sandboxed=True))
    assert "error" not in out.lower() or "created" in out.lower()


async def test_sandboxed_authoring_rejects_code_with_no_run_function():
    """A sandboxed tool with required params + no test_args is never test-run in the container, so
    authoring is the only gate. Code that never defines `run` must be rejected, not persisted."""
    persist = tempfile.mkdtemp()
    fake = FakeRuntime(result=ExecResult(0, '{"ok": true, "result": "ok"}', ""))
    ct = _ct(persist_dir=persist, sandbox_enabled=True, sandbox_runtime=fake, timeout=30)
    out = await ct.run(CreateToolTool.Params(
        name="norun", description="d", parameters={"a": {"type": "integer"}},
        code="import os\nx = 1", sandboxed=True))   # no test_args -> not test-run in container
    assert "run(args)" in out and "must define" in out.lower()
    import os
    assert not os.path.exists(os.path.join(persist, "norun.json"))   # not persisted
    assert not fake.calls   # never shipped to the container


async def test_sandboxed_authoring_rejects_async_run():
    """`async def run` returns an un-awaited coroutine in the container; reject it at authoring,
    matching the host-side path's `async def` guard."""
    persist = tempfile.mkdtemp()
    fake = FakeRuntime(result=ExecResult(0, '{"ok": true, "result": "ok"}', ""))
    ct = _ct(persist_dir=persist, sandbox_enabled=True, sandbox_runtime=fake, timeout=30)
    out = await ct.run(CreateToolTool.Params(
        name="asyncrun", description="d", parameters={},
        code="async def run(args):\n    return 'x'", test_args={}, sandboxed=True))
    assert "async" in out.lower() and "regular" in out.lower()
    import os
    assert not os.path.exists(os.path.join(persist, "asyncrun.json"))
    assert not fake.calls


async def test_sandboxed_true_but_sandbox_off_saves_but_skips_the_container_test_run():
    persist = tempfile.mkdtemp()
    ct = _ct(persist_dir=persist, sandbox_enabled=False, sandbox_runtime=None)
    out = await ct.run(CreateToolTool.Params(
        name="later", description="d", parameters={}, code="def run(args):\n    return 'x'",
        test_args={}, sandboxed=True))
    import json
    import os
    m = json.load(open(os.path.join(persist, "later.json")))
    assert m["sandboxed"] is True
    assert "sandbox" in out.lower()   # a note that it couldn't be verified until the sandbox is on


def test_load_persisted_tools_reads_the_flag_and_builds_a_sandboxed_tool():
    import json
    import os
    persist = tempfile.mkdtemp()
    with open(os.path.join(persist, "s.json"), "w") as fh:
        json.dump({"name": "s", "description": "d", "parameters": {},
                   "code": "def run(args): return 'x'", "sandboxed": True}, fh)
    fake = FakeRuntime()
    tools = load_persisted_tools(persist, sandbox_runtime=fake, sandbox_workspace="default")
    assert len(tools) == 1 and tools[0].sandboxed is True and tools[0].runtime is fake


def test_load_persisted_tool_without_flag_is_host_side():
    import json
    import os
    persist = tempfile.mkdtemp()
    with open(os.path.join(persist, "h.json"), "w") as fh:
        json.dump({"name": "h", "description": "d", "parameters": {},
                   "code": "def run(args): return 'x'"}, fh)   # no sandboxed key
    tools = load_persisted_tools(persist)
    assert len(tools) == 1 and tools[0].sandboxed is False
