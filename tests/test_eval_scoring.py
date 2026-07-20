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
