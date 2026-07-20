"""Pure prompt-builder + reply-parser for the model-graded skill-eval judge. No I/O, no model call.

The judge scores a run's OUTPUT QUALITY (0-3) against a case's rubric criteria — the dimension the
deterministic chain-scorer can't see (e.g. schema soundness, or a correct clarifying question that
produces no tool call). The prompt is BLIND: it never reveals which arm produced the output, nor the
skill name/procedure, so treatment and baseline are graded on the same neutral footing.
"""
from __future__ import annotations

import json
import re

_SYSTEM = (
    "You are a strict evaluator of an AI assistant that works with structured data (tables). "
    "You are given the user's REQUEST, a neutral summary of WHAT THE ASSISTANT DID (its tool actions "
    "and any tables it created), its FINAL REPLY, and CRITERIA for a good result. Score how well it "
    "satisfied the CRITERIA on this scale:\n"
    "  0 = ignored or failed the request\n"
    "  1 = attempted but poor (wrong structure, lost or unqueryable data)\n"
    "  2 = acceptable\n"
    "  3 = fully meets the criteria\n"
    "A focused clarifying question, when the request is genuinely ambiguous, is GOOD — not a failure. "
    "Judge only the result against the criteria; do not reward verbosity. "
    'Output ONLY strict JSON: {"score": <0-3 integer>, "why": "<one short sentence>"}. '
    "No prose, no markdown, no code fence."
)


def _outcome(captured: dict) -> str:
    tools = list(captured.get("tools") or [])
    schemas = []
    for a in captured.get("create_table_args") or []:
        a = a or {}
        schemas.append(f"{a.get('name', '?')}({', '.join(a.get('columns') or [])})")
    inserts = tools.count("insert_row")
    return "\n".join([
        f"- tools called, in order: {tools or '(none)'}",
        f"- tables created: {'; '.join(schemas) if schemas else '(none)'}",
        f"- rows inserted: {inserts}",
    ])


def build_judge_prompt(case: dict, captured: dict) -> list[dict]:
    """Neutral, BLIND grading prompt — no arm, no skill name/procedure."""
    rubric = "\n".join(f"- {c}" for c in (case.get("rubric") or []))
    user = (f"REQUEST:\n{case.get('prompt', '')}\n\n"
            f"WHAT THE ASSISTANT DID:\n{_outcome(captured)}\n\n"
            f"FINAL REPLY:\n{captured.get('final', '')}\n\n"
            f"CRITERIA:\n{rubric}\n\n"
            'Score 0-3 as strict JSON {"score": N, "why": "..."}.')
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def parse_judge_reply(text: str) -> dict:
    """-> {"score": int|None, "why": str}. Tolerates fenced JSON, out-of-range (clamped 0..3), a bare
    integer, and garbage (score None). Never raises."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?", "", s, flags=re.IGNORECASE).strip().rstrip("`").strip()
    score, why = None, ""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "score" in obj:
            score = int(obj["score"])
            why = str(obj.get("why", ""))
    except Exception:
        pass
    if score is None:
        # Strict JSON failed — fall back to a STANDALONE single digit 0-3 (\b…\b), so a date/year in
        # the reasoning ("dated 2026-07-01 … a 2") can't be mistaken for the score and clamped up.
        m = re.search(r"\b[0-3]\b", s)
        score = int(m.group()) if m else None
        why = s[:200]
    if score is not None:
        score = max(0, min(3, score))
    return {"score": score, "why": why}
