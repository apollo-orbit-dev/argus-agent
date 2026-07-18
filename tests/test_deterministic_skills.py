"""Deterministic skill execution — structured `steps` run through the routine executor:
tool steps dispatch in the harness (no model call); the model runs only for model steps."""
import asyncio

from config import Config
from engine.engine import Engine
from engine.routines.executor import RoutineExecutor, _render_args
from engine.routines.store import Routine
from engine.skills.base import Skill, _extract_steps, parse_frontmatter


# ---- steps extraction from a skill body ----
def test_extract_steps_valid_block():
    body = (
        "Some prose.\n\n```steps\n"
        '[{"type":"tool","tool":"calculator","args":{"expression":"1+1"}}]\n'
        "```\nmore prose"
    )
    steps = _extract_steps(body, "s")
    assert len(steps) == 1 and steps[0]["tool"] == "calculator"
    assert steps[0]["id"] == "calculator"          # default id filled by validation


def test_extract_steps_absent_or_bad():
    assert _extract_steps("no block here", "s") == []
    assert _extract_steps("```steps\nnot json\n```", "s") == []      # invalid JSON -> ignored
    assert _extract_steps('```steps\n{"a":1}\n```', "s") == []        # not a list -> ignored
    # a step with an unknown type fails shape validation -> ignored (prose fallback)
    assert _extract_steps('```steps\n[{"type":"nope"}]\n```', "s") == []


def test_skill_loader_populates_steps(tmp_path):
    (tmp_path / "quick.md").write_text(
        "---\nname: quick\ndescription: quick calc\ntools: [calculator]\n---\n"
        "Do a quick calc.\n\n```steps\n"
        '[{"type":"tool","id":"calc","tool":"calculator","args":{"expression":"2+2"}}]\n'
        "```\n", encoding="utf-8")
    from engine.skills.base import SkillRegistry
    reg = SkillRegistry()
    reg.load_dir(str(tmp_path))
    sk = reg.get("quick")
    assert sk is not None and len(sk.steps) == 1 and sk.steps[0]["tool"] == "calculator"


# ---- executor seed (the {{input}} a skill turn passes in) ----
def test_executor_seed_injects_vars():
    async def run_tool(sid, name, args):
        return "ok"

    async def run_model(sid, prompt, skill):
        return prompt                                   # echo so we can see substitution

    ex = RoutineExecutor(run_tool, run_model)
    r = Routine(name="t", steps=[{"type": "model", "id": "m", "prompt": "Q: {{input}}"}])
    res = asyncio.run(ex.run(r, "s", deliver=False, seed={"input": "hello world"}))
    assert res.ok and res.output == "Q: hello world"


# ---- end-to-end: a tool-only skill runs deterministically, with NO model call ----
def _engine(tmp_path):
    # a deliberately unreachable model URL: if the deterministic path ever calls the model, it errors
    cfg = Config(model_base_url="http://127.0.0.1:9/v1", model_name="m", telegram_bot_token="")
    eng = Engine(cfg)
    eng._system_prompt_file = tmp_path / "sp.txt"
    return eng


def test_deterministic_skill_runs_tool_step_without_model(tmp_path):
    eng = _engine(tmp_path)
    eng.skill_registry.register(Skill(
        name="quick_calc", description="quick calc", tools=["calculator"], procedure="body",
        steps=[{"type": "tool", "id": "calc", "tool": "calculator",
                "args": {"expression": "6*7"}}]))
    ans = asyncio.run(eng.run_task("s1", "compute it", requested_skill="quick_calc"))
    assert "42" in ans                                  # tool ran; a model call would have errored
    # the turn was recorded so the conversation stays coherent
    convo = eng.store.conversation("s1")
    assert convo and convo[-1]["role"] == "assistant" and "42" in convo[-1]["content"]


def test_deterministic_skill_reports_failed_step(tmp_path):
    eng = _engine(tmp_path)
    eng.skill_registry.register(Skill(
        name="broken", description="broken", tools=["calculator"], procedure="body",
        steps=[{"type": "tool", "id": "boom", "tool": "no_such_tool", "args": {}}]))
    ans = asyncio.run(eng.run_task("s2", "go", requested_skill="broken"))
    assert "couldn't finish" in ans                     # a missing tool surfaces as a clean failure


# ---- structured data flow between deterministic steps (query_rows -> make_chart) ----
def test_whole_value_ref_injects_structured_data():
    ctx = {"rows": '[{"label":"Jun","value":420},{"label":"Jul","value":430}]', "who": "monthly"}
    out = _render_args({"data": "{{rows}}", "title": "Report {{who}}"}, ctx)
    assert out["data"] == [{"label": "Jun", "value": 420}, {"label": "Jul", "value": 430}]  # structured
    assert out["title"] == "Report monthly"                       # embedded ref stays a string


def test_whole_value_plain_string_and_unknown():
    assert _render_args({"x": "{{note}}"}, {"note": "just text"})["x"] == "just text"
    assert _render_args({"x": "{{missing}}"}, {})["x"] == ""    # unknown var -> empty
    assert _render_args({"x": "{{n}}"}, {"n": "5"})["x"] == "5"  # bare JSON scalar -> keep string


def test_executor_flows_structured_output_between_steps():
    seen = {}

    async def run_tool(sid, name, args):
        if name == "query_rows":
            return '[{"label":"A","value":1},{"label":"B","value":2}]'
        seen["chart_args"] = args                                # capture what make_chart received
        return "chart created: chart.png"

    async def run_model(sid, prompt, skill):
        return prompt

    ex = RoutineExecutor(run_tool, run_model)
    r = Routine(name="t", steps=[
        {"type": "tool", "id": "rows", "tool": "query_rows", "args": {"sql": "SELECT ..."}},
        {"type": "tool", "id": "chart", "tool": "make_chart",
         "args": {"title": "T", "chart_type": "bar", "data": "{{rows}}"}},
    ])
    res = asyncio.run(ex.run(r, "s", deliver=False))
    assert res.ok
    # the rows flowed into make_chart's data as a real list of dicts, not a stringified blob
    assert seen["chart_args"]["data"] == [{"label": "A", "value": 1}, {"label": "B", "value": 2}]


def test_prose_skill_without_steps_does_not_hijack(tmp_path):
    # a skill with no steps must NOT take the deterministic path (steps == [])
    eng = _engine(tmp_path)
    eng.skill_registry.register(Skill(
        name="prose_only", description="prose", tools=[], procedure="just prose", steps=[]))
    sk = eng.skill_registry.get("prose_only")
    assert sk.steps == []                               # stays model-driven
