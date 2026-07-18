"""Custom slash-command aliases: `/name` expands to text that's then run as if the user typed it.

Stored as a flat YAML map (name: expansion) so it's editable from the command line; the file is
reloaded whenever it changes on disk, so edits take effect WITHOUT restarting the server. Custom
commands are deliberately NOT registered in the Telegram command menu, so they don't clutter the
`/` suggestions — they simply work when typed.
"""
from __future__ import annotations

import os
import re
import threading

import yaml

# Built-in Telegram commands a custom alias must not shadow (built-ins are handled first anyway).
RESERVED_COMMANDS = {
    "start", "help", "new", "reset", "usage", "compact", "mode", "model", "models", "reasoning",
    "roles", "role", "reembed", "skills", "tools", "cron", "retry", "memories", "forget", "status",
    "stop", "restart", "verbose", "pending", "approve", "deny",
}


def sanitize_command_name(name: str) -> str:
    """Lowercase, strip a leading '/', and reduce to [a-z0-9_] (Telegram command charset)."""
    name = (name or "").strip().lstrip("/").lower()
    return re.sub(r"[^a-z0-9_]+", "_", name).strip("_")


class CustomCommandStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._items: dict = {}
        self._mtime = None
        self._reload()

    def _reload(self) -> None:
        """Re-read the file if it changed on disk (picks up CLI edits without a restart)."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._items, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                self._items = {sanitize_command_name(k): str(v).strip()
                               for k, v in data.items() if k and v}
                self._mtime = mtime
        except Exception:
            pass   # keep the last good copy on a parse error

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._items, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
        os.replace(tmp, self.path)
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            pass

    def list(self) -> dict:
        self._reload()
        return dict(self._items)

    def get(self, name: str):
        self._reload()
        return self._items.get(sanitize_command_name(name))

    def set(self, name: str, expansion: str) -> str:
        n = sanitize_command_name(name)
        if not n:
            raise ValueError("command name must contain letters or digits")
        if n in RESERVED_COMMANDS:
            raise ValueError(f"'{n}' is a built-in command — pick another name")
        expansion = str(expansion or "").strip()
        if not expansion:
            raise ValueError("expansion cannot be empty")
        with self._lock:
            self._reload()
            self._items[n] = expansion
            self._save()
        return n

    def remove(self, name: str) -> bool:
        n = sanitize_command_name(name)
        with self._lock:
            self._reload()
            if n in self._items:
                del self._items[n]
                self._save()
                return True
            return False
