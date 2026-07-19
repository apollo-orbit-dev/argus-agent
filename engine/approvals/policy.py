"""PermissionStore — the Allow/Ask/Deny toggle per gate. JSON, atomic."""
from __future__ import annotations
import json, os
from engine.approvals.types import GATES


class PermissionStore:
    def __init__(self, path: str):
        self.path = path
        self.states: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.loads(open(self.path, encoding="utf-8").read())
                if isinstance(data, dict):
                    self.states = {k: v for k, v in data.items()
                                   if k in GATES and v in GATES[k].states}
            except Exception:
                self.states = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.states, fh, indent=1)
        os.replace(tmp, self.path)

    def get(self, kind: str) -> str:
        return self.states.get(kind, GATES[kind].default)

    def set(self, kind: str, state: str) -> None:
        if kind not in GATES or state not in GATES[kind].states:
            raise ValueError(f"invalid policy {kind}={state}")
        self.states[kind] = state
        self._save()

    def list(self) -> list[dict]:
        return [{"kind": k, "label": g.label, "state": self.get(k), "states": list(g.states)}
                for k, g in GATES.items()]
