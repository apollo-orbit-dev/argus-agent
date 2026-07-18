"""Memory tools — explicit remember / recall / forget, session-bound to the user.

Explicit-first (deterministic) is the reliable path for small models; relevant
memories are also auto-injected into context at the start of each run by the engine.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.memory.manager import Memory
from engine.tools.base import Tool


class RememberTool(Tool):
    name = "remember"
    description = ("Save a fact to remember about the user for future conversations (a "
                  "preference, a personal detail, an ongoing project, etc.). Use when the "
                  "user shares something worth remembering, or asks you to remember it.")

    class Params(BaseModel):
        fact: str = Field(..., description="the fact to remember, as a short statement")

    def __init__(self, memory: Memory, user_id: str):
        self.memory = memory
        self.user_id = user_id

    async def run(self, args: "RememberTool.Params") -> str:
        rec = await self.memory.remember(self.user_id, args.fact)
        return f"Remembered (id {rec['id']}): {rec['text']}"


class RecallTool(Tool):
    name = "recall"
    description = ("Search your saved memories about the user for anything relevant to a "
                  "query. Use when you might have previously stored relevant info.")

    class Params(BaseModel):
        query: str = Field(..., description="what to look up in memory")

    def __init__(self, memory: Memory, user_id: str):
        self.memory = memory
        self.user_id = user_id

    async def run(self, args: "RecallTool.Params") -> str:
        hits = await self.memory.recall(self.user_id, args.query, k=5)
        if not hits:
            return "No relevant memories found."
        return "Relevant memories:\n" + "\n".join(f"- [{h['id']}] {h['text']}" for h in hits)


class ForgetTool(Tool):
    name = "forget"
    description = ("Delete a saved memory. Give either its id (from recall) OR a short "
                  "description of what to forget (e.g. 'that I like coffee') and the best "
                  "match is removed. Use when the user asks you to forget or drop something, "
                  "or corrects a fact you stored.")

    class Params(BaseModel):
        memory_id: int = Field(default=0, description="id of the memory to delete (0 if using description)")
        description: str = Field(default="", description="what to forget, e.g. 'that I like coffee'")

    def __init__(self, memory: Memory, user_id: str):
        self.memory = memory
        self.user_id = user_id

    async def run(self, args: "ForgetTool.Params") -> str:
        if args.memory_id:
            if self.memory.forget(self.user_id, args.memory_id):
                return f"Forgot memory {args.memory_id}."
            return f"No memory with id {args.memory_id} found for you."
        if args.description.strip():
            deleted = await self.memory.forget_by_query(self.user_id, args.description)
            if deleted:
                return f"Forgot: {deleted['text']}"
            return f"Couldn't find a saved memory matching {args.description!r}."
        return "forget error: provide a memory_id or a description of what to forget."
