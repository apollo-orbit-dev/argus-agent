"""ApprovalStore — durable log of approval requests. JSON, atomic, mirrors RulesStore hardening."""
from __future__ import annotations
import copy, json, os, time, uuid


def _well_formed(r: object) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("id"), str)
            and isinstance(r.get("kind"), str) and isinstance(r.get("created_at"), (int, float)))


class ApprovalStore:
    def __init__(self, path: str):
        self.path = path
        self.reqs: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.loads(open(self.path, encoding="utf-8").read())
                self.reqs = [r for r in data if _well_formed(r)] if isinstance(data, list) else []
            except Exception:
                self.reqs = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.reqs, fh, indent=1)
        os.replace(tmp, self.path)

    def create(self, kind, target, session_id, prompt, origin, payload=None, now=None,
               run_id: str = "", step: int = 0) -> dict:
        rec = {"id": uuid.uuid4().hex[:8], "kind": kind, "target": target,
               "session_id": session_id, "prompt": prompt, "origin": origin,
               "payload": payload or {}, "status": "pending",
               "created_at": time.time() if now is None else now,
               "resolved_at": None, "decision": None, "actor": None,
               "run_id": run_id, "step": step}
        self.reqs.append(rec)
        self._save()
        return copy.deepcopy(rec)

    def resolve(self, req_id, status, decision, actor, now=None) -> bool:
        for r in self.reqs:
            if r["id"] == req_id:
                r["status"] = status
                r["decision"] = decision
                r["actor"] = actor
                r["resolved_at"] = time.time() if now is None else now
                self._save()
                return True
        return False

    def get(self, req_id) -> dict | None:
        return next((copy.deepcopy(r) for r in self.reqs if r["id"] == req_id), None)

    def pending(self) -> list[dict]:
        return [copy.deepcopy(r) for r in self.reqs if r.get("status") == "pending"]

    def list(self, limit: int = 50) -> list[dict]:
        return [copy.deepcopy(r) for r in sorted(self.reqs, key=lambda r: r["created_at"], reverse=True)][:limit]
