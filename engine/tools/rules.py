"""Model-facing tools for standing behavioral rules. Mirrors engine/tools/memory.py."""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.rules.store import RulesStore
from engine.tools.base import Tool


class SaveRuleTool(Tool):
    name = "save_rule"
    description = (
        "Save a STANDING behavioral rule the owner wants you to follow from now on "
        "(e.g. 'always confirm before deleting', 'never use emoji'). Use this for durable "
        "how-to-behave directives, NOT for facts about the user (use 'remember' for those)."
    )

    class Params(BaseModel):
        rule: str = Field(..., description="the standing instruction, as a short imperative")

    def __init__(self, rules: RulesStore):
        self.rules = rules

    async def run(self, args: "SaveRuleTool.Params") -> str:
        rec = self.rules.add(args.rule, source="user")
        if rec is None:
            return "Cannot save an empty rule."
        return f"Saved standing rule (id {rec['id']}): {rec['text']}"


class ListRulesTool(Tool):
    name = "list_rules"
    description = "List the owner's active standing behavioral rules with their ids."

    class Params(BaseModel):
        pass

    def __init__(self, rules: RulesStore):
        self.rules = rules

    async def run(self, args: "ListRulesTool.Params") -> str:
        rows = self.rules.enabled_rules()
        if not rows:
            return "No standing rules."
        return "\n".join(f"- ({r['id']}) {r['text']}" for r in rows)


class RemoveRuleTool(Tool):
    name = "remove_rule"
    description = "Remove a standing behavioral rule by its id (get ids from list_rules)."

    class Params(BaseModel):
        rule_id: str = Field(..., description="the id of the rule to remove")

    def __init__(self, rules: RulesStore):
        self.rules = rules

    async def run(self, args: "RemoveRuleTool.Params") -> str:
        if self.rules.remove(args.rule_id):
            return f"Removed rule {args.rule_id}."
        return f"No rule with id {args.rule_id}."
