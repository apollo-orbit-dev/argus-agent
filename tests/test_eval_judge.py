"""Model-graded judge — pure prompt-builder + reply-parser, hand-built inputs, no model."""
from engine.eval.judge import build_judge_prompt, parse_judge_reply


def test_parse_clean_json():
    assert parse_judge_reply('{"score": 3, "why": "good"}') == {"score": 3, "why": "good"}


def test_parse_fenced_json():
    r = parse_judge_reply('```json\n{"score": 2, "why": "ok"}\n```')
    assert r["score"] == 2


def test_parse_clamps_out_of_range():
    assert parse_judge_reply('{"score": 7, "why": "x"}')["score"] == 3
    assert parse_judge_reply('{"score": -2, "why": "x"}')["score"] == 0


def test_parse_bare_integer_fallback():
    assert parse_judge_reply("I would say 2 out of 3.")["score"] == 2


def test_parse_garbage_is_none():
    assert parse_judge_reply("no number here at all")["score"] is None


def test_parse_ignores_dates_in_prose():
    # regression: a date/year in the reasoning must NOT be read as the score and clamped up to 3
    assert parse_judge_reply("Looking at the request dated 2026-07-01, I rate this a 2.")["score"] == 2
    assert parse_judge_reply("The table covers 2026 data. Score: 1/3, missing typed columns.")["score"] == 1


def test_parse_large_number_only_is_none():
    # a stray multi-digit number with no standalone 0-3 is unparseable, not a 3
    assert parse_judge_reply("rated 2026 overall")["score"] is None


def test_prompt_includes_request_outcome_rubric():
    case = {"prompt": "make a recipes table", "skill": "design_table",
            "rubric": ["json column for ingredients", "one row per recipe"]}
    cap = {"tools": ["create_table", "insert_row"],
           "create_table_args": [{"name": "recipes", "columns": ["name:text:key", "ingredients:json"]}],
           "final": "Created the recipes table with an ingredients json column."}
    msgs = build_judge_prompt(case, cap)
    blob = " ".join(m["content"] for m in msgs)
    assert "make a recipes table" in blob            # request
    assert "ingredients:json" in blob                # outcome (schema)
    assert "json column for ingredients" in blob     # rubric
    assert "one row per recipe" in blob


def test_prompt_is_blind_to_arm_and_skill():
    case = {"prompt": "x", "skill": "design_table", "rubric": ["r1"]}
    cap = {"tools": [], "create_table_args": [], "final": "done"}
    blob = " ".join(m["content"] for m in build_judge_prompt(case, cap)).lower()
    assert "design_table" not in blob and "treatment" not in blob and "baseline" not in blob
