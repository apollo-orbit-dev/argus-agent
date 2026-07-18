"""Persisted state for the trusted-tool tier — model-authored tools a HUMAN reviewed and
approved to run OUTSIDE the sandbox (full builtins, unrestricted imports).

Pure state only (no compile/exec). When a created tool needs a restricted capability (os,
sqlite3, open, subprocess, dunder, …) and `enable_trusted_tools` is on, a *trust request*
carrying the full code is filed here; a human reads the code in the dashboard and approves.
Approval records a hash of the exact code — if the code later changes, the hash won't match
and it must be re-approved (a trusted tool can't be silently swapped for different code).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

log = logging.getLogger("argus.trust")


def code_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TrustStore:
    def __init__(self, path: str):
        self.path = path
        self.trusted: dict[str, dict] = {}      # tool_name -> {code_hash, approved_at}
        self.requests: list[dict] = []          # newest last
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                data = json.load(open(self.path, encoding="utf-8"))
                self.trusted = data.get("trusted", {}) or {}
                self.requests = data.get("requests", []) or []
        except Exception:
            log.exception("could not load trust store from %s", self.path)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"trusted": self.trusted, "requests": self.requests}, fh, indent=2)
        except Exception:
            log.exception("could not save trust store")

    # ---- queries ----
    def is_trusted(self, tool_name: str, ch: str) -> bool:
        """True only if this tool is approved AND its code hash matches (no silent code swap)."""
        t = self.trusted.get(tool_name)
        return bool(t and t.get("code_hash") == ch)

    def get(self, req_id: str) -> Optional[dict]:
        return next((r for r in self.requests if r["id"] == req_id), None)

    def list(self, status: Optional[str] = None) -> list[dict]:
        return [r for r in self.requests if status is None or r["status"] == status]

    # ---- mutations ----
    def request(self, tool_name: str, code: str, session_id: str) -> dict:
        ch = code_hash(code)
        existing = next((r for r in self.requests if r["tool_name"] == tool_name
                         and r["code_hash"] == ch and r["status"] == "pending"), None)
        if existing:
            return existing
        req = {"id": "trust_" + uuid.uuid4().hex[:8], "tool_name": tool_name, "code": code,
               "code_hash": ch, "session_id": session_id, "status": "pending", "created_at": _now()}
        self.requests.append(req)
        self._save()
        return req

    def approve(self, req_id: str) -> Optional[dict]:
        r = self.get(req_id)
        if not r or r["status"] != "pending":
            return None
        r["status"] = "approved"
        r["resolved_at"] = _now()
        self.trusted[r["tool_name"]] = {"code_hash": r["code_hash"], "approved_at": r["resolved_at"]}
        self._save()
        return r

    def deny(self, req_id: str) -> Optional[dict]:
        r = self.get(req_id)
        if not r or r["status"] != "pending":
            return None
        r["status"] = "denied"
        r["resolved_at"] = _now()
        self._save()
        return r

    def revoke(self, tool_name: str) -> bool:
        if tool_name in self.trusted:
            del self.trusted[tool_name]
            self._save()
            return True
        return False
