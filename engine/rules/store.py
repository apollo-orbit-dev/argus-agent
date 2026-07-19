"""RulesStore — durable standing behavioral rules ("don't do X", "always Y").

JSON-backed, small, fully loaded every turn. Mirrors engine/tools/watch.py:WatchStore
(atomic temp+replace, no locking). Not SQLite: no recall/query/rollups needed.
"""
from __future__ import annotations

import json
import os
import time
import uuid


def _well_formed(r: object) -> bool:
    """A usable rule record: dict with a string id/text and a numeric created_at.
    Read/mutate methods index these keys directly, so a malformed row is dropped at load."""
    return (isinstance(r, dict) and isinstance(r.get("id"), str)
            and isinstance(r.get("text"), str) and r.get("text").strip() != ""
            and isinstance(r.get("created_at"), (int, float)))


class RulesStore:
    def __init__(self, path: str):
        self.path = path
        self.rules: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.loads(open(self.path, encoding="utf-8").read())
                # Drop any malformed record at load so a hand-edited rules.json can't KeyError
                # the every-turn injection path (enabled_rules) — one bad row shouldn't brick a turn.
                self.rules = [r for r in data if _well_formed(r)] if isinstance(data, list) else []
            except Exception:
                self.rules = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.rules, fh, indent=1)
        os.replace(tmp, self.path)

    def add(self, text: str, source: str = "user", now: float | None = None) -> dict | None:
        text = (text or "").strip()
        if not text:
            return None
        for r in self.rules:                       # dedup (case-insensitive) -> re-enable
            if r["text"].lower() == text.lower():
                r["enabled"] = True
                self._save()
                return dict(r)
        rec = {"id": uuid.uuid4().hex[:8], "text": text, "source": source,
               "enabled": True, "created_at": time.time() if now is None else now}
        self.rules.append(rec)
        self._save()
        return dict(rec)

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        if len(self.rules) != before:
            self._save()
            return True
        return False

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        for r in self.rules:
            if r["id"] == rule_id:
                r["enabled"] = bool(enabled)
                self._save()
                return True
        return False

    def list(self) -> list[dict]:                  # newest-first (dashboard)
        return [dict(r) for r in sorted(self.rules, key=lambda r: r["created_at"], reverse=True)]

    def enabled_rules(self) -> list[dict]:         # oldest-first (stable prompt order)
        return [dict(r) for r in sorted(self.rules, key=lambda r: r["created_at"])
                if r.get("enabled")]
