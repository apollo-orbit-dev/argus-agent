"""Randomness helpers: dice, coin, number, choice. Network-free (uses random)."""
from __future__ import annotations

import random

from pydantic import BaseModel, Field

from engine.tools.base import Tool

_ACTIONS = {"dice", "coin", "number", "choice"}


class RandomTool(Tool):
    name = "random_tool"
    description = (
        "Generate randomness. action='dice' rolls one die with `sides` faces; "
        "'coin' flips a coin; 'number' picks a random integer in [min, max]; "
        "'choice' picks one item from `options`. Use for random selection or games."
    )

    class Params(BaseModel):
        action: str = Field(
            ..., description="One of: 'dice', 'coin', 'number', 'choice'."
        )
        sides: int = Field(6, description="Number of die faces for action='dice'.")
        min: int = Field(1, description="Lower bound (inclusive) for action='number'.")
        max: int = Field(100, description="Upper bound (inclusive) for action='number'.")
        options: list[str] = Field(
            default_factory=list, description="Items to pick from for action='choice'."
        )

    async def run(self, args: "RandomTool.Params") -> str:
        try:
            action = args.action.strip().lower()
            if action == "dice":
                if args.sides < 1:
                    return "random_tool error: sides must be at least 1."
                roll = random.randint(1, args.sides)
                return f"Rolled a d{args.sides}: {roll}"
            if action == "coin":
                return f"Coin flip: {random.choice(['heads', 'tails'])}"
            if action == "number":
                if args.min > args.max:
                    return (
                        f"random_tool error: min ({args.min}) must not exceed "
                        f"max ({args.max})."
                    )
                return f"Random number in [{args.min}, {args.max}]: {random.randint(args.min, args.max)}"
            if action == "choice":
                if not args.options:
                    return "random_tool error: options list is empty for action='choice'."
                return f"Chose: {random.choice(args.options)}"
            return (
                f"random_tool error: unknown action '{args.action}'. "
                f"Valid actions: {', '.join(sorted(_ACTIONS))}."
            )
        except Exception as e:  # defensive: never crash the loop
            return f"random_tool error: {e}"
