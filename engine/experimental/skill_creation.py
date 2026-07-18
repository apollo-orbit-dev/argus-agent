"""EXPERIMENTAL — model-authored skills.

A skill is procedural knowledge as DATA (name + description + declared tools +
markdown procedure) — no code execution, so `create_skill` is far safer than
`create_tool`. This is the natural place for a small model (or a larger one) to
capture a reusable procedure it worked out.

Guardrails: the declared tools must be real (validated against the tool registry),
the name is sanitized (no path traversal), and description/procedure must be
non-empty. Created skills are written to a markdown file (loaded data with a stable
schema — the same forward-compat principle as shipped skills) and registered.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from pydantic import BaseModel, Field

from engine.skills.base import Skill, SkillRegistry
from engine.tools.base import Tool, ToolRegistry


def sanitize_skill_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")


def render_skill_markdown(name: str, description: str, tools: list[str], procedure: str) -> str:
    tools_line = "[" + ", ".join(tools) + "]"
    return (f"---\nname: {name}\ndescription: {description}\ntools: {tools_line}\n---\n"
            f"{procedure.strip()}\n")


class CreateSkillTool(Tool):
    name = "create_skill"
    description = (
        "Create a reusable SKILL — a named, step-by-step procedure for a multi-step task "
        "that orchestrates existing tools. Provide: name (snake_case); description (what it's "
        "for / when to use it); tools (a list of EXISTING tool names it uses); and procedure "
        "(clear numbered steps a small model can follow). The skill is saved for future use. "
        "A skill is NOT a tool you call — it AUTO-ACTIVATES on relevant requests and injects its "
        "steps for you to follow. To FIX/update a skill, just call create_skill again with the SAME "
        "name and corrected content — it replaces the old one (don't make a new name)."
    )

    class Params(BaseModel):
        name: str = Field(..., description="snake_case skill name")
        description: str = Field(..., description="what the skill is for / when to use it")
        tools: list[str] = Field(default_factory=list, description="existing tool names it uses")
        procedure: str = Field(..., description="numbered step-by-step instructions")

    def __init__(self, skill_registry: SkillRegistry, tool_registry: ToolRegistry,
                 library_dir: str):
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        self.library_dir = library_dir
        self.created: list[dict] = []

    async def run(self, args: "CreateSkillTool.Params") -> str:
        record = {"name": args.name, "description": args.description, "tools": args.tools,
                  "procedure": args.procedure, "ok": False, "error": None}
        self.created.append(record)

        safe = sanitize_skill_name(args.name)
        if not safe:
            record["error"] = "invalid name"
            return "create_skill error: name must contain letters/numbers."
        replacing = self.skill_registry.get(safe) is not None   # same name -> update in place
        if not args.description.strip() or not args.procedure.strip():
            record["error"] = "empty description/procedure"
            return "create_skill error: both a description and a procedure body are required."

        unknown = [t for t in args.tools if self.tool_registry.get(t) is None]
        if unknown:
            known = ", ".join(self.tool_registry.names())
            record["error"] = f"unknown tools: {unknown}"
            return (f"create_skill error: these declared tools don't exist: {', '.join(unknown)}. "
                    f"Use only existing tools: {known}.")

        md = render_skill_markdown(safe, args.description.strip(), args.tools, args.procedure)
        try:
            os.makedirs(self.library_dir, exist_ok=True)
            with open(os.path.join(self.library_dir, f"{safe}.md"), "w", encoding="utf-8") as fh:
                fh.write(md)
        except Exception as e:
            record["error"] = f"write failed: {e}"
            return f"create_skill error: could not save the skill ({e})."

        self.skill_registry.register(Skill(name=safe, description=args.description.strip(),
                                           tools=list(args.tools), procedure=args.procedure.strip(),
                                           path=os.path.join(self.library_dir, f"{safe}.md")))
        record["ok"] = True
        verb = "updated" if replacing else "created"
        return (f"create_skill: '{safe}' {verb} and saved. NOTE: a skill is NOT a tool — do NOT try "
                "to call it. It auto-activates on relevant requests. To use it right now, just FOLLOW "
                f"its steps yourself with the tools it names ({', '.join(args.tools) or 'none'}).")


class InspectSkillTool(Tool):
    name = "inspect_skill"
    description = (
        "Show a skill's full definition — its description, the tools it declares, and its "
        "numbered procedure. Use this to READ a skill before you revise it (call create_skill "
        "with the same name to update). Argument: name."
    )

    class Params(BaseModel):
        name: str = Field(..., description="skill name to inspect")

    def __init__(self, skill_registry: SkillRegistry):
        self.skill_registry = skill_registry

    async def run(self, args: "InspectSkillTool.Params") -> str:
        sk = self.skill_registry.get(sanitize_skill_name(args.name)) or self.skill_registry.get(args.name)
        if sk is None:
            names = ", ".join(s.name for s in self.skill_registry.list()) or "(none)"
            return f"inspect_skill: no skill named '{args.name}'. Existing skills: {names}."
        tools = ", ".join(sk.tools) or "(none)"
        return (f"Skill '{sk.name}'\ndescription: {sk.description}\ntools: {tools}\n"
                f"procedure:\n{sk.procedure}")


class DeleteSkillTool(Tool):
    name = "delete_skill"
    description = (
        "Delete a skill you created, by exact name, when the user no longer wants it. Only "
        "skills YOU created can be deleted (built-in library skills are protected). To delete "
        "several, call this once per skill with each exact name. Argument: name."
    )

    class Params(BaseModel):
        name: str = Field(..., description="exact skill name to delete")

    def __init__(self, skill_registry: SkillRegistry, created_skills_dir: str):
        self.skill_registry = skill_registry
        self.created_skills_dir = created_skills_dir

    async def run(self, args: "DeleteSkillTool.Params") -> str:
        safe = sanitize_skill_name(args.name)
        sk = self.skill_registry.get(safe) or self.skill_registry.get(args.name)
        if sk is None:
            names = ", ".join(s.name for s in self.skill_registry.list()) or "(none)"
            return f"delete_skill: no skill named '{args.name}'. Existing skills: {names}."
        path = os.path.join(self.created_skills_dir, f"{sk.name}.md")
        if not os.path.exists(path):
            return (f"delete_skill: '{sk.name}' is a built-in skill and can't be deleted "
                    "(only skills you created can be removed).")
        self.skill_registry.unregister(sk.name)
        try:
            os.remove(path)
        except Exception as e:
            return f"delete_skill: removed '{sk.name}' from the registry but could not delete its file ({e})."
        return f"delete_skill: '{sk.name}' deleted (registry + disk)."
