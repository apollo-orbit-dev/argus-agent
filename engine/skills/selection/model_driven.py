"""Model-driven skill selection — progressive disclosure.

Inject skill names + descriptions and let the model decide when to load a skill's
full procedure (via the load_skill tool). Faithful to how skills "should" work,
but leans on judgment small models are weak at — which is exactly what the A/B
against explicit selection measures.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from engine.skills.base import SkillContext, SkillRegistry, SkillSelector
from engine.tools.base import Tool


class LoadSkillTool(Tool):
    name = "load_skill"
    description = ("Load the step-by-step procedure for a named skill. Call this when a "
                   "skill listed as available fits the user's request, then follow the "
                   "returned procedure.")

    class Params(BaseModel):
        name: str = Field(..., description="The exact name of the skill to load")

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def run(self, args: "LoadSkillTool.Params") -> str:
        skill = self.registry.get(args.name)
        if skill is None:
            available = ", ".join(s.name for s in self.registry.list()) or "(none)"
            return f"load_skill error: no skill named {args.name!r}. Available: {available}"
        tools = ", ".join(skill.tools) if skill.tools else "(none)"
        return (f"Procedure for skill '{skill.name}' (tools: {tools}):\n\n{skill.procedure}\n\n"
                f"Now follow this procedure to answer the user, using the tools as directed.")


class ModelDrivenSelector(SkillSelector):
    name = "model_driven"

    def prepare(self, session_id: str, user_text: str,
                requested_skill: Optional[str]) -> SkillContext:
        skills = self.registry.list()
        if not skills:
            return SkillContext()
        listing = "\n".join(f"- {s.name}: {s.description}" for s in skills)
        additions = (
            "## Available skills\n"
            "These skills provide proven step-by-step procedures for common multi-step "
            "tasks:\n\n"
            f"{listing}\n\n"
            "If one of these skills fits the user's request, call the load_skill tool with "
            "its exact name to get the procedure, then follow it. If none fit, just answer "
            "normally."
        )
        return SkillContext(system_additions=additions,
                            extra_tools=[LoadSkillTool(self.registry)],
                            active_skill=None)
