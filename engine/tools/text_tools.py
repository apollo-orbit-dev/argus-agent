"""Text utilities: count, case transforms, reverse, base64. Network-free."""
from __future__ import annotations

import base64
import binascii

from pydantic import BaseModel, Field

from engine.tools.base import Tool

_ACTIONS = {
    "count", "upper", "lower", "reverse", "title",
    "base64_encode", "base64_decode",
}


class TextTool(Tool):
    name = "text_tools"
    description = (
        "Transform or inspect text. action='count' summarizes words/chars/lines; "
        "'upper'/'lower'/'title' change case; 'reverse' reverses the string; "
        "'base64_encode'/'base64_decode' convert to/from base64. "
        "Use for quick text manipulation."
    )

    class Params(BaseModel):
        action: str = Field(
            ...,
            description=(
                "One of: 'count', 'upper', 'lower', 'reverse', 'title', "
                "'base64_encode', 'base64_decode'."
            ),
        )
        text: str = Field(..., description="The input text to operate on.")

    async def run(self, args: "TextTool.Params") -> str:
        try:
            action = args.action.strip().lower()
            text = args.text
            if action == "count":
                words = len(text.split())
                chars = len(text)
                lines = len(text.splitlines()) or (1 if text else 0)
                return f"{words} words, {chars} characters, {lines} lines"
            if action == "upper":
                return text.upper()
            if action == "lower":
                return text.lower()
            if action == "reverse":
                return text[::-1]
            if action == "title":
                return text.title()
            if action == "base64_encode":
                return base64.b64encode(text.encode("utf-8")).decode("ascii")
            if action == "base64_decode":
                try:
                    decoded = base64.b64decode(text, validate=True)
                    return decoded.decode("utf-8")
                except (binascii.Error, ValueError):
                    return "text_tools error: input is not valid base64."
                except UnicodeDecodeError:
                    return "text_tools error: decoded bytes are not valid UTF-8 text."
            return (
                f"text_tools error: unknown action '{args.action}'. "
                f"Valid actions: {', '.join(sorted(_ACTIONS))}."
            )
        except Exception as e:  # defensive: never crash the loop
            return f"text_tools error: {e}"
