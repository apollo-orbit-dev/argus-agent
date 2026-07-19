"""PermissionStore — the Allow/Ask/Deny toggle per tool. JSON, atomic."""
from __future__ import annotations
import json, os
from engine.approvals.types import states_for, default_for


class PermissionStore:
    def __init__(self, path: str):
        self.path = path
        self.states_map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.loads(open(self.path, encoding="utf-8").read())
                if isinstance(data, dict):
                    self.states_map = {k: v for k, v in data.items()
                                       if isinstance(k, str) and v in states_for(k)}
            except Exception:
                self.states_map = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.states_map, fh, indent=1)
        os.replace(tmp, self.path)

    def get(self, key: str) -> str:
        return self.states_map.get(key, default_for(key))

    def set(self, key: str, state: str) -> None:
        if state not in states_for(key):
            raise ValueError(f"invalid policy {key}={state}")
        self.states_map[key] = state
        self._save()

    def states(self, keys: list[str]) -> list[dict]:
        allkeys = list(dict.fromkeys([*keys, "dep-install"]))    # dedup, always include dep-install
        out = []
        for k in allkeys:
            st = self.get(k)
            out.append({"key": k, "state": st, "states": states_for(k),
                        "is_default": k not in self.states_map})
        return out
