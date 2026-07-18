"""Hybrid skill selection — explicit trigger match first, model-driven fallback.

Best of both for small models: the precise, zero-overhead trigger match handles the
common phrasings; when nothing triggers, we fall back to progressive disclosure so
the model can still choose a skill for novel phrasings. The loop consumes the same
SkillContext either way.
"""
from __future__ import annotations

from typing import Optional

from engine.skills.base import SkillContext, SkillRegistry, SkillSelector
from engine.skills.selection.explicit import ExplicitSelector
from engine.skills.selection.model_driven import ModelDrivenSelector


class HybridSelector(SkillSelector):
    name = "hybrid"

    def __init__(self, registry: SkillRegistry):
        super().__init__(registry)
        self._explicit = ExplicitSelector(registry)
        self._model_driven = ModelDrivenSelector(registry)

    def prepare(self, session_id: str, user_text: str,
                requested_skill: Optional[str]) -> SkillContext:
        ctx = self._explicit.prepare(session_id, user_text, requested_skill)
        if ctx.active_skill:            # a trigger fired (or a skill was requested) -> use it
            return ctx
        return self._model_driven.prepare(session_id, user_text, requested_skill)
