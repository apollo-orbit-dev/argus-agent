"""ask_user — let the agent ask the user a clarifying question instead of guessing.

It is a TERMINAL tool: calling it ends the turn and returns the question to the
user; the user's next message is the answer. High-leverage for small models, which
otherwise tend to guess or hallucinate when a request is ambiguous.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.tools.base import Tool


class AskUserTool(Tool):
    name = "ask_user"
    description = ("Ask the user a clarifying question ONLY when the request is genuinely too "
                  "broad or underspecified to answer usefully — it has several DISTINCT "
                  "interpretations that would lead to materially different answers, so picking "
                  "one would likely be wrong (e.g. 'tell me about the wall', 'find info about "
                  "Titan'). If the request is specific enough to give a useful answer — even a "
                  "general overview — just ANSWER IT; do NOT ask. A clear question that only "
                  "lacks a minor detail is still answerable: answer with a sensible default and "
                  "note the assumption. Default to answering; ask only when you truly cannot. "
                  "Optionally provide a few choices. This ends your turn — the user's reply "
                  "comes next.")
    terminal = True

    class Params(BaseModel):
        question: str = Field(..., description="the clarifying question to ask the user")
        options: list[str] = Field(default_factory=list,
                                   description="optional list of choices to offer")

    async def run(self, args: "AskUserTool.Params") -> str:
        if args.options:
            opts = "\n".join(f"  {i}. {o}" for i, o in enumerate(args.options, 1))
            return f"{args.question}\n{opts}"
        return args.question
