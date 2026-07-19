"""Auto-detect standing behavioral rules from owner corrections.

A cheap lexical pre-gate (has_rule_cue) decides whether an owner message is worth an
aux-model pass; the model then drafts standalone imperative rules (or NONE). Mirrors the
memory autoextract flow but scoped to how-to-behave directives, not facts.
"""
from __future__ import annotations

import re

# Word-boundary cues that mark a correction / standing directive. Corrections almost always
# carry one of these, so gating on them keeps the (metered) aux model off ordinary chat.
_CUES = [
    r"don'?t", r"do not", r"never", r"always", r"stop", r"quit", r"no more",
    r"from now on", r"going forward", r"instead", r"make sure", r"\bagain\b",
]
_CUE_RE = re.compile("|".join(_CUES), re.IGNORECASE)


def has_rule_cue(text: str) -> bool:
    return bool(_CUE_RE.search(text or ""))


RULE_EXTRACT_PROMPT = (
    "You extract STANDING BEHAVIORAL RULES from the owner's latest message: durable "
    "instructions about how the assistant should behave from now on (e.g. corrections like "
    "'don't do that again', or directives like 'always confirm before deleting', 'never use "
    "emoji', 'from now on use metric units').\n"
    "Use the recent conversation to resolve references — turn 'don't do that again' into a "
    "concrete standalone imperative describing the behavior to change.\n"
    "Rules:\n"
    "- Output ONLY genuine standing directives about the assistant's behavior.\n"
    "- Do NOT output facts about the user (those are handled elsewhere), one-off task "
    "requests, or comments about tone/personality.\n"
    "- Do NOT repeat a rule that already exists (a list of current rules is provided).\n"
    "- Write each rule as a short imperative sentence, one per line, no numbering.\n"
    "- If there is nothing to add, output exactly: NONE"
)
