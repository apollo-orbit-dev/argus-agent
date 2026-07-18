"""Persisted state for approval-gated dependency installs.

When a created tool needs a non-stdlib import, the sandbox files a *request* here
instead of hard-failing. A human approves/denies it (dashboard or Telegram); on
approval the module is pip-installed and added to `approved`, which the sandbox
unions into its import allowlist. Pure state only — no subprocess, no pip (that
lives in dep_installer.py) so this stays trivially testable.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

log = logging.getLogger("argus.deps")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DepStore:
    def __init__(self, path: str):
        self.path = path
        self.approved: dict[str, dict] = {}      # module -> {version, approved_at}
        self.requests: list[dict] = []           # newest last
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                data = json.load(open(self.path, encoding="utf-8"))
                self.approved = data.get("approved", {}) or {}
                self.requests = data.get("requests", []) or []
        except Exception:
            log.exception("could not load dep store from %s", self.path)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"approved": self.approved, "requests": self.requests}, fh, indent=2)
        except Exception:
            log.exception("could not save dep store")

    # ---- queries ----
    def approved_modules(self) -> set[str]:
        return set(self.approved)

    def is_approved(self, module: str) -> bool:
        return module in self.approved

    def get(self, req_id: str) -> Optional[dict]:
        return next((r for r in self.requests if r["id"] == req_id), None)

    def list(self, status: Optional[str] = None) -> list[dict]:
        return [r for r in self.requests if status is None or r["status"] == status]

    def pending_for(self, module: str) -> Optional[dict]:
        return next((r for r in self.requests
                     if r["module"] == module and r["status"] == "pending"), None)

    # ---- mutations ----
    def request(self, module: str, tool_name: str, session_id: str, code: str = "") -> dict:
        """File a pending request. If one is already pending for this module, reuse it
        (a re-run of create_tool for the same lib shouldn't spam duplicates)."""
        existing = self.pending_for(module)
        if existing:
            return existing
        req = {"id": "dep_" + uuid.uuid4().hex[:8], "module": module,
               "tool_name": tool_name, "session_id": session_id, "code": code,
               "status": "pending", "created_at": _now()}
        self.requests.append(req)
        self._save()
        return req

    def mark_approved(self, req_id: str, version: str = "") -> Optional[dict]:
        r = self.get(req_id)
        if not r or r["status"] != "pending":
            return None
        r["status"] = "approved"
        r["resolved_at"] = _now()
        self.approved[r["module"]] = {"version": version, "approved_at": r["resolved_at"]}
        self._save()
        return r

    def mark_failed(self, req_id: str, error: str) -> Optional[dict]:
        r = self.get(req_id)
        if not r:
            return None
        r["status"] = "pending"          # stays actionable so the user can retry /approve
        r["last_error"] = error[:500]
        self._save()
        return r

    def deny(self, req_id: str) -> Optional[dict]:
        r = self.get(req_id)
        if not r or r["status"] not in ("pending",):
            return None
        r["status"] = "denied"
        r["resolved_at"] = _now()
        self._save()
        return r
