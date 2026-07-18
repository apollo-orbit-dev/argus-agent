"""dictionary — English word definitions via dictionaryapi.dev (keyless).

Returns part(s) of speech and the first one or two definitions per meaning.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/"
_DEF_CAP = 200


class DictionaryTool(Tool):
    name = "dictionary"
    description = ("Define an English word: parts of speech and short definitions. "
                   "Use when the user asks what a word means.")

    class Params(BaseModel):
        word: str = Field(..., description="The English word to define")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def run(self, args: "DictionaryTool.Params") -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(_URL + quote(args.word.strip(), safe=""))
        except httpx.HTTPError as e:
            return f"dictionary error: could not reach dictionary service ({e})"
        if r.status_code == 404:
            return f"dictionary: no definition found for {args.word!r}."
        if r.status_code != 200:
            return f"dictionary error: HTTP {r.status_code}"
        try:
            data = r.json()
        except Exception as e:
            return f"dictionary error: could not parse response ({e})"
        if not isinstance(data, list) or not data:
            return f"dictionary: no definition found for {args.word!r}."
        entry = data[0]
        word = entry.get("word", args.word)
        lines = [f"{word}:"]
        for meaning in entry.get("meanings", []):
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])[:2]
            for d in defs:
                text = (d.get("definition") or "").strip()
                if len(text) > _DEF_CAP:
                    text = text[:_DEF_CAP].rstrip() + "…"
                lines.append(f"  ({pos}) {text}")
        if len(lines) == 1:
            return f"dictionary: no definition found for {args.word!r}."
        return "\n".join(lines)
