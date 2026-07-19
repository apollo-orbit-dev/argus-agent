"""tools_overview() must enumerate every per-run/conditional tool run_task actually registers.

Regression: update_soul moved off the base registry onto a per-run, approval-aware registration
and silently dropped out of the dashboard's tool library. tools_overview()'s hand-maintained
`conditional_enabled` list must be extended alongside any future per-run registration change.
"""
from config import Config
from engine.engine import Engine


def test_update_soul_listed_when_enabled(tmp_path):
    e = Engine(Config(enable_soul_editing=True), data_dir=str(tmp_path))
    names = {t["name"] for t in e.tools_overview()["conditional_enabled"]}
    assert "update_soul" in names                 # regressed out when moved per-run; must be back


def test_update_soul_absent_when_disabled(tmp_path):
    e = Engine(Config(enable_soul_editing=False), data_dir=str(tmp_path))
    names = {t["name"] for t in e.tools_overview()["conditional_enabled"]}
    assert "update_soul" not in names


def test_enumeration_covers_flagged_groups(tmp_path):
    e = Engine(Config(enable_soul_editing=True, enable_rules=True, enable_memory=True),
               data_dir=str(tmp_path))
    names = {t["name"] for t in e.tools_overview()["conditional_enabled"]}
    assert {"update_soul", "save_rule", "remember", "forget"} <= names


def test_entries_are_uniform_dicts_with_name_and_description(tmp_path):
    e = Engine(Config(enable_soul_editing=True), data_dir=str(tmp_path))
    cond = e.tools_overview()["conditional_enabled"]
    assert cond, "expected at least one conditional entry"
    for entry in cond:
        assert set(entry) >= {"name", "description"}
        assert isinstance(entry["name"], str) and entry["name"]
        assert isinstance(entry["description"], str) and entry["description"]


def test_read_soul_not_duplicated_in_conditional(tmp_path):
    # read_soul is registered onto the base registry at Engine construction (gated by
    # enable_soul_editing there too) — it belongs in `builtin`, not `conditional_enabled`.
    e = Engine(Config(enable_soul_editing=True), data_dir=str(tmp_path))
    overview = e.tools_overview()
    cond_names = {t["name"] for t in overview["conditional_enabled"]}
    builtin_names = {t["name"] for t in overview["builtin"]}
    assert "read_soul" not in cond_names
    assert "read_soul" in builtin_names


def test_watch_charts_notify_routines_code_interpreter_enumerated(tmp_path):
    e = Engine(Config(enable_watch=True, enable_charts=True, enable_notify=True,
                       enable_routines=True, enable_code_interpreter=True),
               data_dir=str(tmp_path))
    names = {t["name"] for t in e.tools_overview()["conditional_enabled"]}
    assert {"watch", "list_watches", "unwatch", "make_chart", "notify",
            "run_routine", "list_routines", "exec_python"} <= names


def test_tool_creation_requires_native_mode(tmp_path):
    # create_tool et al. are only ever registered per-run when tool_calling_mode == "native"
    # (manual mode can't carry code/multiline payloads) — the enumeration must mirror that AND.
    e = Engine(Config(enable_tool_creation=True, tool_calling_mode="manual"),
               data_dir=str(tmp_path))
    names = {t["name"] for t in e.tools_overview()["conditional_enabled"]}
    assert "create_tool" not in names

    e2 = Engine(Config(enable_tool_creation=True, tool_calling_mode="native"),
                data_dir=str(tmp_path))
    names2 = {t["name"] for t in e2.tools_overview()["conditional_enabled"]}
    assert {"create_tool", "inspect_tool", "delete_tool"} <= names2
