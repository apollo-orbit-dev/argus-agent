"""RulesStore — durable standing behavioral rules ("don't do X", "always Y").

JSON-backed, small, fully loaded every turn. Mirrors engine/tools/watch.py:WatchStore
(atomic temp+replace, no locking). Not SQLite: no recall/query/rollups needed.
"""
from __future__ import annotations

import json
import os
import time
import uuid


class RulesStore:
    def __init__(self, path: str):
        self.path = path
        self.rules: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.loads(open(self.path, encoding="utf-8").read())
                self.rules = data if isinstance(data, list) else []
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
