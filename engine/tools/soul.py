"""update_soul / read_soul — the agent editing its OWN persona (SOUL).

Self-modification, deliberately scoped: this touches only the SOUL (voice/personality prepended to
the system prompt), NOT the operational system prompt (the real behavioral rules). So the worst a
self-edit can do is change how Argus *sounds*, not what it's allowed to do — and it's fully
recoverable (the previous persona is backed up; revert from the dashboard). Changes take effect on
the next turn (the engine updates its live soul) and persist to SOUL.md.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.tools.base import Tool

_MAX_SOUL = 4000       # keep the persona from bloating the system prompt


class ReadSoulTool(Tool):
    name = "read_soul"
    description = ("Read your CURRENT persona/voice (your SOUL) — the personality prepended to your "
                  "instructions. Use it before revising your persona. No arguments.")

    class Params(BaseModel):
        pass

    def __init__(self, get_soul):
        self.get_soul = get_soul

    async def run(self, args: "ReadSoulTool.Params") -> str:
        s = (self.get_soul() or "").strip()
        return f"Your current persona (SOUL):\n\n{s}" if s else "You have no custom persona set (default voice)."


class UpdateSoulTool(Tool):
    name = "update_soul"
    description = (
        "Change your OWN persona/voice — the SOUL that shapes how you speak. Use this when the user "
        "asks you to change your personality, tone, or style ('be more concise', 'drop the wizard "
        "voice', 'be warmer', 'match how I write'). Write the COMPLETE new persona in `soul` — you "
        "already carry your current persona, so REVISE it, keeping what should stay and changing what "
        "they asked. It takes effect immediately and persists. It changes only your VOICE, not your "
        "abilities, and your previous persona is backed up so it can be reverted."
    )

    class Params(BaseModel):
        soul: str = Field(..., description="the complete new persona/voice text (revise your current one)")

    def __init__(self, get_soul, set_soul, max_len: int = _MAX_SOUL):
        self.get_soul = get_soul
        self.set_soul = set_soul
        self.max_len = max_len

    async def run(self, args: "UpdateSoulTool.Params") -> str:
        text = (args.soul or "").strip()
        if not text:
            return "update_soul error: the persona can't be empty. Write the full new persona."
        if len(text) > self.max_len:
            return (f"update_soul error: that's too long ({len(text)} chars; max {self.max_len}). "
                    "Keep the persona concise.")
        self.set_soul(text)
        return ("update_soul: my persona is updated and in effect from now on. My previous persona is "
                "backed up, so this can be reverted from the dashboard's Soul panel if needed.")
