"""Explicit skill selection — direct invoke OR a cheap keyword pre-match.

Less magical, far more reliable at small model size, and closer to how the
eventual non-technical user picks "plan a trip" from a list. The chosen
procedure is injected up front so the model just follows the recipe.
"""
from __future__ import annotations

import re
from typing import Optional

from engine.skills.base import Skill, SkillContext, SkillSelector

_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
         "use", "using", "when", "what", "how", "a", "an", "to", "of", "in", "on",
         "it", "is", "are", "then", "by", "or", "as", "at", "be"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


_URL_RE = re.compile(r"https?://\S+")


def render_skill(skill: Skill) -> str:
    tools = ", ".join(skill.tools) if skill.tools else "(none declared)"
    return (f"## ACTIVE SKILL: {skill.name}\n"
            f"A proven procedure has been selected for this request. You MUST follow it "
            f"step by step, calling the tools it names in order — do NOT answer from memory "
            f"or skip the tool calls. If a step's tool fails, follow the procedure's guidance "
            f"rather than inventing a result.\n\n"
            f"{skill.procedure}\n\n"
            f"(Tools this skill uses: {tools}.)")


def _score(skill: Skill, text_l: str, toks: set[str]) -> int:
    """Higher = better match. Triggers are the strong, precise signal."""
    score = 0
    trigger_hit = False
    for trig in skill.triggers:
        if trig.lower() in text_l:
            score += 5
            trigger_hit = True
    if skill.name.lower().replace("_", " ") in text_l:
        score += 5
    # weak descriptive overlap, capped so it can't outweigh a real trigger by itself
    score += min(2, len(toks & _tokens(skill.description)))
    # summarize_url: a URL REINFORCES the skill, but only when the user also expressed
    # summarize intent (a trigger hit). A bare URL must NOT activate it on its own — e.g.
    # "build a tool using this library: <url>" is a create request, not a summarize request.
    if skill.name == "summarize_url" and trigger_hit and _URL_RE.search(text_l):
        score += 3
    return score


class ExplicitSelector(SkillSelector):
    name = "explicit"

    def prepare(self, session_id: str, user_text: str,
                requested_skill: Optional[str]) -> SkillContext:
        # 1) Direct invocation always wins.
        if requested_skill:
            skill = self.registry.get(requested_skill)
            if skill:
                return SkillContext(system_additions=render_skill(skill),
                                    active_skill=skill.name)
        # 2) Trigger-based pre-match. A trigger hit (score >= 5) activates the skill;
        #    ties break toward the more specific (higher) score.
        text_l = user_text.lower()
        toks = _tokens(user_text)
        best, best_score = None, 0
        for skill in self.registry.list():
            s = _score(skill, text_l, toks)
            if s > best_score:
                best, best_score = skill, s
        if best and best_score >= 5:
            return SkillContext(system_additions=render_skill(best), active_skill=best.name)
        return SkillContext()
