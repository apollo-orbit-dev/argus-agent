"""A vetted, first-class key/value datastore the agent can use to persist structured
data across turns and sessions — e.g. daily metrics, logs, preferences.

This is the answer to "can the agent store data in SQLite?" WITHOUT loosening the
create_tool sandbox: the store is shipped and trusted, so it runs with normal
privileges and the model never writes file/DB code itself. The model just calls
`datastore` with save/get/list/delete. Data lives in named collections; for
time-series data the convention is to use a date (or timestamp) as the key.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from engine.tools.base import Tool


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DataStore:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS records (
                   collection TEXT NOT NULL,
                   key        TEXT NOT NULL,
                   value      TEXT NOT NULL,
                   created_at TEXT,
                   updated_at TEXT,
                   PRIMARY KEY (collection, key))""")
        self._db.commit()

    def put(self, collection: str, key: str, value: str) -> dict:
        now = _now()
        prior = self._db.execute(
            "SELECT created_at FROM records WHERE collection=? AND key=?",
            (collection, key)).fetchone()
        created = prior[0] if prior else now
        self._db.execute(
            "INSERT OR REPLACE INTO records (collection, key, value, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (collection, key, value, created, now))
        self._db.commit()
        return {"collection": collection, "key": key, "updated": bool(prior)}

    def get(self, collection: str, key: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT value, updated_at FROM records WHERE collection=? AND key=?",
            (collection, key)).fetchone()
        return {"value": row[0], "updated_at": row[1]} if row else None

    def list(self, collection: str, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT key, value, updated_at FROM records WHERE collection=? "
            "ORDER BY key DESC LIMIT ?", (collection, limit)).fetchall()
        return [{"key": r[0], "value": r[1], "updated_at": r[2]} for r in rows]

    def collections(self) -> list[str]:
        return [r[0] for r in self._db.execute(
            "SELECT DISTINCT collection FROM records ORDER BY collection").fetchall()]

    def delete(self, collection: str, key: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM records WHERE collection=? AND key=?", (collection, key))
        self._db.commit()
        return cur.rowcount > 0


class DataStoreTool(Tool):
    name = "datastore"
    description = (
        "Stash a SINGLE named value that should persist across turns/sessions but is NOT a fact "
        "about the user (so it doesn't belong in memory) — e.g. a padlock combination, a "
        "confirmation code, an API token, a saved link, a one-off preference. action='save' stores a "
        "value under a key in a named collection; 'get' reads one key; 'list' shows recent keys; "
        "'delete' removes a key. "
        "This is a simple key→value stash — it CANNOT filter, sort, or aggregate. Do NOT use it for "
        "records you'll query or analyze over time (daily metrics, readings, logs, "
        "expenses, contacts): for anything with rows/columns you'll run date ranges, AVG/SUM/COUNT, or "
        "GROUP BY on, use create_table instead."
    )

    class Params(BaseModel):
        action: str = Field(..., description="one of: save, get, list, delete")
        collection: str = Field(..., description="the collection name, e.g. 'readings'")
        key: str = Field("", description="the record key (e.g. a date); required for save/get/delete")
        value: str = Field("", description="the value to store (text or a JSON string); required for save")

    def __init__(self, store: DataStore):
        self.store = store

    async def run(self, args: "DataStoreTool.Params") -> str:
        action = (args.action or "").strip().lower()
        coll = (args.collection or "").strip()
        key = (args.key or "").strip()
        if not coll:
            return "datastore error: 'collection' is required."

        if action == "save":
            if not key or not args.value:
                return "datastore error: 'save' needs both a key and a value."
            r = self.store.put(coll, key, args.value)
            return f"saved {coll}[{key}] ({'updated' if r['updated'] else 'new record'})."
        if action == "get":
            if not key:
                return "datastore error: 'get' needs a key."
            r = self.store.get(coll, key)
            return f"{coll}[{key}] = {r['value']}" if r else f"no record found at {coll}[{key}]."
        if action == "list":
            rows = self.store.list(coll)
            if not rows:
                return f"collection '{coll}' is empty."
            body = "\n".join(f"- {x['key']}: {x['value']}" for x in rows)
            return f"{len(rows)} entr{'y' if len(rows)==1 else 'ies'} in '{coll}':\n{body}"
        if action == "delete":
            if not key:
                return "datastore error: 'delete' needs a key."
            ok = self.store.delete(coll, key)
            return f"deleted {coll}[{key}]." if ok else f"no record at {coll}[{key}] to delete."
        return "datastore error: action must be one of: save, get, list, delete."
