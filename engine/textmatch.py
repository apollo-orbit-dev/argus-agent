"""Neutral lexical helpers — the one cheap tokenizer shared by every ranker in the engine.

This module exists for LAYERING, not for novelty. `engine/skills/base.py` declares the order
"loop at the bottom, tools above it, skills orchestrating tools": skills may import tools, tools
may not import skills. The skill selector (engine/skills/selection/explicit.py) grew a private
`_tokens`/`_STOP` pair first, and the tool-disclosure ranker (engine/tools/disclosure.py) needs the
exact same tokenization to score the same way. Importing the skill layer's private helper down into
engine/tools/ would invert the declared direction, so the helper moved HERE — below both, importing
nothing from either.
"""
from __future__ import annotations

import re

# Function words carry no selection signal, and a small model's prompt is mostly them.
STOP_WORDS = {"the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
              "use", "using", "when", "what", "how", "a", "an", "to", "of", "in", "on",
              "it", "is", "are", "then", "by", "or", "as", "at", "be"}


def tokens(text: str) -> set[str]:
    """Lowercased alphanumeric word tokens, minus stop words and 1-2 character noise."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in STOP_WORDS}
