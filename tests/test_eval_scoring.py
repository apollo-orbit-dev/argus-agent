"""Deterministic scorer for the skill-eval harness — pure function, hand-built captured dicts."""
from engine.eval.scoring import score_case


def test_tools_in_order_subsequence_passes():
    cap = {"tools": ["list_tables", "read_file", "create_table", "insert_row", "insert_row"],
           "activated_skill": "extract_to_table"}
    r = score_case({"tools_in_order": ["read_file", "create_table", "insert_row"],
                    "min_counts": {"insert_row": 1}, "activates": "extract_to_table"}, cap)
    assert r["chain_correct"] is True


def test_tools_out_of_order_fails():
    cap = {"tools": ["create_table", "read_file"], "activated_skill": "extract_to_table"}
    r = score_case({"tools_in_order": ["read_file", "create_table"]}, cap)
    assert r["chain_correct"] is False


def test_min_counts_not_met_fails():
    cap = {"tools": ["create_table", "insert_row"], "activated_skill": "extract_to_table"}
    r = score_case({"min_counts": {"insert_row": 3}}, cap)
    assert r["chain_correct"] is False


def test_skill_not_overfire_fails():
    cap = {"tools": [], "activated_skill": "design_table"}
    r = score_case({"skill_not": ["design_table", "extract_to_table"]}, cap)
    assert r["chain_correct"] is False


def test_skill_not_clean_passes():
    cap = {"tools": ["web_search"], "activated_skill": "research"}
    r = score_case({"skill_not": ["design_table", "extract_to_table"]}, cap)
    assert r["chain_correct"] is True


def test_activates_mismatch_fails():
    cap = {"tools": ["create_table"], "activated_skill": None}
    r = score_case({"activates": "design_table"}, cap)
    assert r["chain_correct"] is False


def test_empty_expectation_is_not_a_pass():
    # a predicate with no recognized keys proves nothing
    r = score_case({}, {"tools": ["create_table"], "activated_skill": "design_table"})
    assert r["chain_correct"] is False


def test_schema_has_json_column_passes():
    cap = {"tools": ["create_table"], "activated_skill": "design_table",
           "create_table_args": [{"name": "recipes",
                                  "columns": ["name:text:key", "ingredients:json", "steps:json"]}]}
    r = score_case({"tools_in_order": ["create_table"], "schema_has": ["json"]}, cap)
    assert r["chain_correct"] is True


def test_schema_has_json_column_fails_on_all_text():
    cap = {"tools": ["create_table"], "activated_skill": "design_table",
           "create_table_args": [{"name": "recipes",
                                  "columns": ["name:text", "ingredients:text", "steps:text"]}]}
    r = score_case({"schema_has": ["json"]}, cap)
    assert r["chain_correct"] is False


# ---- absence-of-pathology predicates (for batteries of deliberately unanswerable tasks) ----

def test_max_counts_passes_when_under_the_ceiling():
    cap = {"tools": ["query_rows", "query_rows"], "activated_skill": None}
    assert score_case({"max_counts": {"query_rows": 2}}, cap)["chain_correct"] is True


def test_max_counts_fails_when_a_tool_is_repeated_too_often():
    cap = {"tools": ["query_rows", "query_rows", "query_rows"], "activated_skill": None}
    r = score_case({"max_counts": {"query_rows": 2}}, cap)
    assert r["chain_correct"] is False
    assert "max_counts" in r["reasons"][0]


def test_max_counts_ignores_tools_it_does_not_name():
    cap = {"tools": ["calculator"] * 9 + ["query_rows"], "activated_skill": None}
    assert score_case({"max_counts": {"query_rows": 2}}, cap)["chain_correct"] is True


def test_no_observer_passes_when_the_issue_never_fired():
    cap = {"tools": ["query_rows"], "activated_skill": None, "observer": ["repeat_nudge"]}
    assert score_case({"no_observer": ["stuck_repeating"]}, cap)["chain_correct"] is True


def test_no_observer_fails_when_the_loop_gave_up():
    cap = {"tools": ["query_rows"] * 3, "activated_skill": None,
           "observer": ["repeat_nudge", "stuck_repeating"]}
    r = score_case({"no_observer": ["stuck_repeating"]}, cap)
    assert r["chain_correct"] is False
    assert "stuck_repeating" in r["reasons"][0]


def test_no_observer_treats_a_missing_capture_as_clean():
    """A run with no observer key (older capture, or a run that errored) must not be scored as
    having fired something it never recorded."""
    cap = {"tools": ["query_rows"], "activated_skill": None}
    assert score_case({"no_observer": ["stuck_repeating"]}, cap)["chain_correct"] is True
