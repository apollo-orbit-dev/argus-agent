"""Table store — structured tables with a SAFE read-only query/aggregate surface.

The datastore is key/value: fetch-one-by-key, enumerate-some. This is the thing KV can't do —
filter, SUM/AVG/COUNT, GROUP BY, date ranges — over many rows. Contacts, expense/health logs, any
tabular data live here, and results feed make_chart.

Safety: WRITES go through explicit tools (create_table / insert_row / drop_table) that validate
identifiers and parameterize values — the model never assembles a write from raw SQL. READS use
`query_table`, run on a SEPARATE read-ONLY SQLite connection (mode=ro, so DROP/UPDATE/INSERT simply
fail) with an authorizer that denies ATTACH/DETACH, single-statement + SELECT-only enforced, and a
row cap. So the worst a bad query can do is read the agent's own tables.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading

from pydantic import BaseModel, Field

from engine.tools.base import Tool

log = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPES = {"text": "TEXT", "string": "TEXT", "str": "TEXT", "integer": "INTEGER", "int": "INTEGER",
          "real": "REAL", "number": "REAL", "float": "REAL", "date": "TEXT",
          # A list/nested value: stored as JSON text (insert coerces a list/dict automatically) and
          # queryable with json_extract()/json_each(). Aliases so a schema can self-document the intent.
          "json": "TEXT", "list": "TEXT", "array": "TEXT", "object": "TEXT"}
_MAX_ROWS = 500


class TableError(Exception):
    pass


def _ident(name: str) -> str:
    name = (name or "").strip()
    if not _IDENT.match(name):
        raise TableError(f"invalid name '{name}' — use letters, digits, underscores; start with a letter")
    return name


def _coerce(v):
    """SQLite can only bind str/int/float/bytes/None. A dict/list value — e.g. a nested API
    payload the model (or a tool) forwarded verbatim into a cell — would raise a cryptic bind
    error that callers routinely ignore, so the row silently vanishes. Store it as JSON text
    instead: the insert succeeds and the data is preserved rather than lost."""
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return v


class TableStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._rw = sqlite3.connect(path, check_same_thread=False)
        self._rw.row_factory = sqlite3.Row
        self._rw.execute("PRAGMA user_version=1")     # ensure the file is a valid db for the ro conn
        self._rw.commit()
        self._ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self._ro.row_factory = sqlite3.Row
        self._ro.set_authorizer(self._authorizer)

    @staticmethod
    def _authorizer(action, a1, a2, dbname, source):
        # deny attaching/detaching other databases; the ro connection blocks all writes already
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def create_table(self, name: str, columns: list) -> str:
        t = _ident(name)
        specs, keys = [], 0
        for c in columns or []:
            parts = str(c).split(":")
            cn = parts[0]
            ct = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else "text"
            flag = parts[2].strip().lower() if len(parts) > 2 else ""
            spec = f"{_ident(cn)} {_TYPES.get(ct, 'TEXT')}"
            if flag in ("key", "pk", "primary", "primary_key"):
                spec += " PRIMARY KEY"    # one row per value of this column; re-insert updates it (upsert)
                keys += 1
            specs.append(spec)
        if not specs:
            raise TableError("a table needs at least one column (e.g. ['date:date:key', 'amount:real'])")
        if keys > 1:
            raise TableError("only one column may be the key (mark it with ':key', e.g. 'date:date:key')")
        with self._lock:
            self._rw.execute(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(specs)})")
            self._rw.commit()
        return t

    def _pk_col(self, t: str):
        """The name of the table's primary-key column, or None."""
        for r in self._rw.execute(f"PRAGMA table_info({t})"):
            if r["pk"]:
                return r["name"]
        return None

    def insert(self, name: str, values: dict) -> None:
        t = _ident(name)
        if not values:
            raise TableError("no values to insert")
        cols = [_ident(k) for k in values]
        ph = ", ".join("?" for _ in cols)
        vals = [_coerce(v) for v in values.values()]   # dict/list -> JSON text (never a bind error)
        with self._lock:
            pk = self._pk_col(t)
            if pk and pk in cols:
                # UPSERT: re-inserting an existing key updates that row (idempotent re-fetch/backfill),
                # only overwriting the columns actually provided.
                updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
                tail = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
                self._rw.execute(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT({pk}) {tail}", vals)
            else:
                self._rw.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({ph})", vals)
            self._rw.commit()

    def delete_rows(self, name: str, match: dict) -> int:
        """Delete rows matching ALL of the given column=value pairs. Refuses an empty match
        (that's what drop_table is for) so a bad row can be removed without nuking the table."""
        t = _ident(name)
        if not match:
            raise TableError("delete_row needs at least one column=value to match "
                             "(use drop_table to remove the whole table)")
        cols = [_ident(k) for k in match]
        clause = " AND ".join(f"{c}=?" for c in cols)
        with self._lock:
            cur = self._rw.execute(f"DELETE FROM {t} WHERE {clause}", [_coerce(v) for v in match.values()])
            self._rw.commit()
            return cur.rowcount

    def query(self, sql: str) -> list[dict]:
        s = (sql or "").strip().rstrip(";").strip()
        low = s.lower()
        if not (low.startswith("select") or low.startswith("with")):
            raise TableError("only read-only SELECT queries are allowed here")
        if ";" in s:
            raise TableError("only a single SELECT statement is allowed")
        rows = self._ro.execute(s).fetchmany(_MAX_ROWS)   # ro connection: any write would error
        return [dict(r) for r in rows]

    def tables(self) -> list[dict]:
        rows = self._rw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        out = []
        for r in rows:
            n = r["name"]
            cols = [f"{c['name']} {c['type']}".strip() + (" PK" if c["pk"] else "")
                    for c in self._rw.execute(f"PRAGMA table_info({n})")]
            cnt = self._rw.execute(f"SELECT COUNT(*) c FROM {n}").fetchone()["c"]
            out.append({"name": n, "columns": cols, "rows": cnt})
        return out

    def rows(self, name: str, limit: int = 50, offset: int = 0) -> dict:
        """A page of a table's rows for the dashboard viewer. Read-only, identifier-validated,
        with a bounded limit. Returns column order + rows + total count for pagination."""
        t = _ident(name)                        # validates the identifier (no injection via name)
        exists = self._ro.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not exists:
            raise TableError(f"no table '{t}'")
        try:
            limit = max(1, min(int(limit), _MAX_ROWS))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            limit, offset = 50, 0
        total = self._ro.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        cols = [c["name"] for c in self._ro.execute(f"PRAGMA table_info({t})")]
        cur = self._ro.execute(f"SELECT * FROM {t} LIMIT ? OFFSET ?", (limit, offset))
        return {"name": t, "columns": cols, "rows": [dict(r) for r in cur.fetchall()],
                "total": total, "limit": limit, "offset": offset}

    def drop(self, name: str) -> bool:
        t = _ident(name)
        with self._lock:
            exists = self._rw.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not exists:
                return False
            self._rw.execute(f"DROP TABLE {t}")
            self._rw.commit()
        return True


def _fmt_rows(rows: list[dict], limit: int = 50) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    lines = [" | ".join(cols)]
    for r in rows[:limit]:
        lines.append(" | ".join("" if r[c] is None else str(r[c]) for c in cols))
    if len(rows) > limit:
        lines.append(f"… ({len(rows)} rows total)")
    return "\n".join(lines)


class CreateTableTool(Tool):
    name = "create_table"
    description = (
        "Create a table for structured, QUERYABLE data — anything with rows and columns you'll filter, "
        "sort, or aggregate over time: daily metrics (weight, steps, expenses), logs, contacts, "
        "records. This is the RIGHT tool (not datastore) whenever you'll want date-range queries, "
        "AVG/SUM/COUNT, or GROUP BY — datastore is only a single key→value stash and can't do any of "
        "that. Args: name (identifier), and columns — a list where each item is 'colname' or "
        "'colname:type' (type: text, integer, real, date, or json for a list/nested value — pass a real "
        "list or dict to insert_row and query it with json_extract). Add a third part ':key' to make a "
        "column the PRIMARY KEY — that enforces ONE row per value and makes re-inserting the same key "
        "UPDATE that row instead of duplicating it (use it for a daily log so a re-fetch/backfill is "
        "idempotent). Example: create_table('daily_sales', ['date:date:key', 'revenue:integer', "
        "'units:integer', 'region:text', 'notes:text']); a list field goes in a json column, e.g. "
        "create_table('recipes', ['name:text:key', 'ingredients:json', 'steps:json']). Then insert_row "
        "rows and query_table to filter/aggregate (e.g. AVG(revenue) WHERE date BETWEEN …)."
    )

    class Params(BaseModel):
        name: str = Field(..., description="table name")
        columns: list[str] = Field(..., description="e.g. ['date:date:key', 'category:text', 'amount:real'] — add ':key' to make the primary key")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "CreateTableTool.Params") -> str:
        try:
            t = self.store.create_table(args.name, args.columns)
        except TableError as e:
            return f"create_table error: {e}"
        return f"create_table: table '{t}' is ready with columns {args.columns}. Add rows with insert_row."


class InsertRowTool(Tool):
    name = "insert_row"
    description = ("Insert one row into a table. Args: table, and values (a dict of column -> value). "
                  "Example: insert_row('expenses', {'date':'2026-07-13','category':'food','amount':24.5}). "
                  "If the table has a ':key' column, inserting an existing key UPDATES that row (upsert) "
                  "and only overwrites the columns you pass — so re-logging a date is safe and idempotent.")

    class Params(BaseModel):
        table: str = Field(..., description="table name")
        values: dict = Field(..., description="column -> value")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "InsertRowTool.Params") -> str:
        try:
            self.store.insert(args.table, args.values)
        except (TableError, sqlite3.Error) as e:
            return f"insert_row error: {e}"
        return f"insert_row: added a row to '{args.table}'."


class QueryTableTool(Tool):
    name = "query_table"
    description = (
        "Run a READ-ONLY SQL SELECT over your tables — this is how you FILTER and AGGREGATE "
        "(WHERE, SUM/AVG/COUNT, GROUP BY, ORDER BY, date ranges). Only SELECT is allowed. "
        "Example: query_table(\"SELECT category, SUM(amount) AS total FROM expenses WHERE date >= "
        "'2026-07-01' GROUP BY category ORDER BY total DESC\"). Use the results to answer or to "
        "feed make_chart. Arg: sql."
    )

    class Params(BaseModel):
        sql: str = Field(..., description="a single read-only SELECT statement")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "QueryTableTool.Params") -> str:
        try:
            rows = self.store.query(args.sql)
        except TableError as e:
            return f"query_table error: {e}"
        except sqlite3.Error as e:
            return f"query_table error: {e} (check the table/column names — see list_tables)"
        return _fmt_rows(rows)


class QueryRowsTool(Tool):
    name = "query_rows"
    description = (
        "Run a READ-ONLY SQL SELECT and return the rows as JSON (a list of {column: value} objects). "
        "Use this in a routine/skill STEP when a LATER step needs the data as structured input — e.g. "
        "select `... AS label, ... AS value` and feed the result straight into make_chart's `data`. "
        "For a human-readable answer, use query_table instead. Arg: sql."
    )

    class Params(BaseModel):
        sql: str = Field(..., description="a single read-only SELECT statement")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "QueryRowsTool.Params") -> str:
        try:
            rows = self.store.query(args.sql)
        except TableError as e:
            return f"query_rows error: {e}"
        except sqlite3.Error as e:
            return f"query_rows error: {e} (check the table/column names — see list_tables)"
        return json.dumps(rows, default=str)


class ListTablesTool(Tool):
    name = "list_tables"
    description = "List your tables with their columns and row counts. No arguments."

    class Params(BaseModel):
        pass

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "ListTablesTool.Params") -> str:
        ts = self.store.tables()
        if not ts:
            return "You have no tables yet. Create one with create_table."
        return "Your tables:\n" + "\n".join(
            f"  {t['name']} ({t['rows']} rows): {', '.join(t['columns'])}" for t in ts)


class DropTableTool(Tool):
    name = "drop_table"
    description = "Delete an entire table and all its rows, by name. Arg: name."

    class Params(BaseModel):
        name: str = Field(..., description="table name to drop")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "DropTableTool.Params") -> str:
        try:
            ok = self.store.drop(args.name)
        except TableError as e:
            return f"drop_table error: {e}"
        return f"drop_table: dropped '{args.name}'." if ok else f"drop_table: no table '{args.name}'."


class DeleteRowTool(Tool):
    name = "delete_row"
    description = (
        "Delete row(s) from a table by matching column values — use this to remove a wrong or "
        "test row without dropping the whole table. Args: table, and match (a dict of "
        "column -> value; a row is deleted only if it matches ALL of them). "
        "Example: delete_row('daily_sales', {'date':'2026-06-20'}). An empty match is refused "
        "(use drop_table to remove an entire table)."
    )

    class Params(BaseModel):
        table: str = Field(..., description="table name")
        match: dict = Field(..., description="column -> value; rows matching ALL pairs are deleted")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "DeleteRowTool.Params") -> str:
        try:
            n = self.store.delete_rows(args.table, args.match)
        except (TableError, sqlite3.Error) as e:
            return f"delete_row error: {e}"
        return f"delete_row: removed {n} row(s) from '{args.table}'."


# ---- ask_data: natural-language question -> SQL -> answer (with schema grounding + self-repair) ----

_ASK_DATA_MAX_ATTEMPTS = 3          # first try + up to 2 error-driven repairs

_ASK_DATA_SYSTEM = (
    "You translate a question into ONE SQLite SELECT over the user's tables.\n"
    "TABLES (name(column TYPE, ...)):\n{schema}\n\n"
    "Rules:\n"
    "- Output ONLY the SQL. No prose, no explanation, no markdown fences.\n"
    "- A single read-only SELECT (or WITH ... SELECT). Never write or modify data.\n"
    "- Use ONLY the tables and columns listed above — never invent a name.\n"
    "- Dates are stored as TEXT in ISO form (YYYY-MM-DD); use date()/strftime() for date math, "
    "and date('now') for today.\n"
    "- If the question cannot be answered from these tables, output exactly: CANNOT"
)


def _schema_text(tables: list[dict]) -> str:
    return "\n".join(f"{t['name']}({', '.join(t['columns'])})  -- {t['rows']} rows" for t in tables)


def _clean_sql(text: str) -> str:
    """Pull a bare SQL statement out of a model reply — strip markdown fences and any prose before
    the first SELECT/WITH. store.query() still enforces read-only/single-statement afterwards."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:sql)?", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"```$", "", s).strip()
    m = re.search(r"\b(select|with)\b", s, re.IGNORECASE)
    if m:
        s = s[m.start():]
    return s.strip().rstrip(";").strip()


class AskDataTool(Tool):
    name = "ask_data"
    description = (
        "Answer a natural-language question about your TABLES — it reads the table schemas, writes a "
        "read-only SQL SELECT for you, runs it, and self-corrects if the query errors. Use this instead "
        "of hand-writing SQL when you just want the answer to a data question, e.g. "
        "ask_data(\"What was my average score last week?\"). Arg: question."
    )

    class Params(BaseModel):
        question: str = Field(..., description="a natural-language question about your tabular data")

    def __init__(self, store: TableStore, aux_client):
        self.store = store
        self._aux = aux_client        # zero-arg factory returning a ModelClient (the utility/chat model)

    async def run(self, args: "AskDataTool.Params") -> str:
        tables = self.store.tables()
        if not tables:
            return "ask_data: you have no tables yet — create one with create_table and add rows first."
        messages = [
            {"role": "system", "content": _ASK_DATA_SYSTEM.format(schema=_schema_text(tables))},
            {"role": "user", "content": (args.question or "").strip()},
        ]
        try:
            client = self._aux()
        except Exception as e:                                   # pragma: no cover - defensive
            return f"ask_data error: no model available ({e})"

        sql, last_err = "", ""
        for attempt in range(1, _ASK_DATA_MAX_ATTEMPTS + 1):
            try:
                # think=False: SQL generation is a background call — with thinking on, a reasoning
                # model can burn the token budget and return empty content (the compaction failure).
                resp = await client.chat(messages, max_tokens=300, think=False)
            except Exception as e:
                return f"ask_data error: could not reach the model ({e})"
            raw = (resp.content or "").strip()
            if not raw:
                last_err = "the model returned an empty query"
                messages.append({"role": "user", "content": "You replied with nothing. Output ONLY a SELECT."})
                continue
            if raw.upper().lstrip().startswith("CANNOT"):
                cols = "\n".join(f"  {t['name']}: {', '.join(t['columns'])}" for t in tables)
                return ("ask_data: I can't answer that from your tables. What you have:\n" + cols)
            sql = _clean_sql(raw)
            try:
                rows = self.store.query(sql)
            except (TableError, sqlite3.Error) as e:
                last_err = str(e)
                log.info("ask_data attempt %d failed: %s | sql=%s", attempt, last_err, sql)
                messages.append({"role": "assistant", "content": sql})
                messages.append({"role": "user", "content":
                                 f"That query failed with error: {last_err}\n"
                                 "Fix it and output ONLY the corrected SELECT (no prose)."})
                continue
            log.info("ask_data ok on attempt %d/%d: %s", attempt, _ASK_DATA_MAX_ATTEMPTS, sql)
            return f"Query: {sql}\n\n{_fmt_rows(rows)}"

        return (f"ask_data: couldn't produce a working query after {_ASK_DATA_MAX_ATTEMPTS} attempts. "
                f"Last error: {last_err}. Last attempt: {sql or '(none)'}")
