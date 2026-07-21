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


def _parse_col_spec(spec) -> tuple[str, str, bool]:
    """Parse a 'name[:type[:flag]]' column spec into (validated_name, sqltype, is_key). Shared by
    create_table and add_column so both agree on types and the ':key' flag."""
    parts = str(spec).split(":")
    name = _ident(parts[0])
    ct = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else "text"
    sqltype = _TYPES.get(ct, "TEXT")
    is_key = len(parts) > 2 and parts[2].strip().lower() in ("key", "pk", "primary", "primary_key")
    return name, sqltype, is_key


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
            cn, sqltype, is_key = _parse_col_spec(c)
            spec = f"{cn} {sqltype}"
            if is_key:
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

    def _table_exists(self, t: str) -> bool:
        return self._rw.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None

    def _columns(self, t: str) -> list[str]:
        return [r["name"] for r in self._rw.execute(f"PRAGMA table_info({t})")]

    def add_column(self, table: str, column: str) -> str:
        t = _ident(table)
        if not self._table_exists(t):
            raise TableError(f"no table '{t}'")
        name, sqltype, is_key = _parse_col_spec(column)
        if is_key:
            raise TableError("cannot add a PRIMARY KEY column to an existing table; "
                             "create the table with the ':key' column instead")
        with self._lock:
            self._rw.execute(f"ALTER TABLE {t} ADD COLUMN {name} {sqltype}")
            self._rw.commit()
        return f"added column '{name}' ({sqltype}) to {t}"

    def rename_column(self, table: str, old: str, new: str) -> str:
        t = _ident(table); o = _ident(old); n = _ident(new)
        if not self._table_exists(t):
            raise TableError(f"no table '{t}'")
        if o not in self._columns(t):
            raise TableError(f"no column '{o}' on {t}")
        with self._lock:
            self._rw.execute(f"ALTER TABLE {t} RENAME COLUMN {o} TO {n}")
            self._rw.commit()
        return f"renamed column '{o}' → '{n}' on {t}"

    def drop_column(self, table: str, column: str) -> str:
        t = _ident(table); c = _ident(column)
        if not self._table_exists(t):
            raise TableError(f"no table '{t}'")
        if c not in self._columns(t):
            raise TableError(f"no column '{c}' on {t}")
        with self._lock:
            try:
                self._rw.execute(f"ALTER TABLE {t} DROP COLUMN {c}")
            except sqlite3.OperationalError as e:
                raise TableError(f"cannot drop column '{c}': {e}")     # PK/indexed/unique column
            self._rw.commit()
        return f"dropped column '{c}' from {t}"

    def rename_table(self, old: str, new: str) -> str:
        o = _ident(old); n = _ident(new)
        if not self._table_exists(o):
            raise TableError(f"no table '{o}'")
        if self._table_exists(n):
            raise TableError(f"a table named '{n}' already exists")
        with self._lock:
            self._rw.execute(f"ALTER TABLE {o} RENAME TO {n}")
            self._rw.commit()
        return f"renamed table {o} → {n}"

    def _create_like(self, source: str, dest: str) -> None:
        """Create dest mirroring source's columns, types, and PK. (CREATE TABLE AS SELECT is NOT
        used because it drops declared types and the primary key.) Caller holds self._lock."""
        specs = []
        for r in self._rw.execute(f"PRAGMA table_info({source})"):
            spec = f"{_ident(r['name'])} {r['type'] or 'TEXT'}"
            if r["pk"]:
                spec += " PRIMARY KEY"
            specs.append(spec)
        self._rw.execute(f"CREATE TABLE {dest} ({', '.join(specs)})")

    def copy_table(self, source: str, dest: str, where: str | None = None) -> str:
        src = _ident(source); dst = _ident(dest)
        if not self._table_exists(src):
            raise TableError(f"no table '{src}'")
        if src == dst:
            # a self-copy on a PK-less table would run INSERT INTO src SELECT … FROM src and silently
            # double every row (no conflict to skip it); reject before any write work
            raise TableError("source and dest must be different tables")
        with self._lock:
            created = not self._table_exists(dst)
            if created:
                self._create_like(src, dst)
            # copy only columns present in BOTH tables, preserving source order
            dst_cols = set(self._columns(dst))
            cols = [c for c in self._columns(src) if c in dst_cols]
            if not cols:
                raise TableError(f"{src} and {dst} share no columns to copy")
            collist = ", ".join(cols)
            pk = self._pk_col(dst)
            conflict = f" ON CONFLICT({pk}) DO NOTHING" if pk else ""
            if where is None:
                # whole-table copy: one server-side statement, no row cap, no materialization.
                # "WHERE 1=1" is required when an ON CONFLICT tail follows: SQLite's parser would
                # otherwise read a bare "FROM src ON CONFLICT(...)" as a join's ON-clause and choke
                # on the following DO — a real grammar ambiguity, not a style choice.
                tail = f" WHERE 1=1{conflict}" if conflict else ""
                cur = self._rw.execute(
                    f"INSERT INTO {dst} ({collist}) SELECT {collist} FROM {src}{tail}")
                n = cur.rowcount
            else:
                w = (where or "").strip()
                if not w or ";" in w:
                    raise TableError("invalid where filter: a single boolean expression, no ';'")
                # Read matching rows on the READ-ONLY connection (mode=ro + single-statement rule
                # make injection a non-issue), then parameterized bulk-insert. No SQL fragment ever
                # reaches a write connection. Batched so a huge result set is not held all at once.
                read = self._ro.execute(f"SELECT {collist} FROM {src} WHERE {w}")
                ph = ", ".join("?" for _ in cols)
                insert_sql = f"INSERT INTO {dst} ({collist}) VALUES ({ph}){conflict}"
                n = 0
                while True:
                    batch = read.fetchmany(1000)
                    if not batch:
                        break
                    cur = self._rw.executemany(insert_sql, [tuple(r[c] for c in cols) for r in batch])
                    n += cur.rowcount     # actual inserts (rows a DO NOTHING conflict skipped are excluded)
            self._rw.commit()
        verb = "created dest and copied" if created else "copied"
        return f"{verb} {n} row(s) from {src} → {dst} (columns: {collist})"

    def update_rows(self, table: str, set_values: dict, match: dict) -> int:
        """Set columns on all rows matching ALL of `match`. Refuses an empty match (that would
        hit every row) and an empty set. Fully parameterized — no SQL fragment from the caller."""
        t = _ident(table)
        if not self._table_exists(t):
            raise TableError(f"no table '{t}'")
        if not set_values:
            raise TableError("update_rows needs at least one column=value to set")
        if not match:
            raise TableError("update_rows needs at least one column=value to match "
                             "(an empty match would update every row)")
        set_cols = [_ident(k) for k in set_values]
        match_cols = [_ident(k) for k in match]
        set_clause = ", ".join(f"{c}=?" for c in set_cols)
        where_clause = " AND ".join(f"{c}=?" for c in match_cols)
        params = [_coerce(v) for v in set_values.values()] + [_coerce(v) for v in match.values()]
        with self._lock:
            cur = self._rw.execute(f"UPDATE {t} SET {set_clause} WHERE {where_clause}", params)
            self._rw.commit()
            return cur.rowcount

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


class AddColumnTool(Tool):
    name = "add_column"
    description = (
        "Add a new column to an EXISTING table WITHOUT recreating it or copying data. Use this "
        "instead of making a new table when someone wants extra fields on a table they already have. "
        "Args: table, and column — a 'name:type' spec like 'sleep_start:text' or 'score:integer' "
        "(type: text, integer, real, date, or json). Existing rows get NULL in the new column. "
        "You cannot add a primary-key column this way. Example: add_column('sleep_log', 'sleep_start:text')."
    )

    class Params(BaseModel):
        table: str = Field(..., description="the existing table to alter")
        column: str = Field(..., description="a 'name:type' column spec, e.g. 'sleep_start:text'")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "AddColumnTool.Params") -> str:
        try:
            return self.store.add_column(args.table, args.column)
        except (TableError, sqlite3.Error) as e:
            return f"add_column error: {e}"


class RenameColumnTool(Tool):
    name = "rename_column"
    description = ("Rename a column on an existing table (data preserved). Args: table, old, new. "
                  "Example: rename_column('sleep_log', 'score', 'restful_score').")

    class Params(BaseModel):
        table: str = Field(..., description="the table")
        old: str = Field(..., description="current column name")
        new: str = Field(..., description="new column name")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "RenameColumnTool.Params") -> str:
        try:
            return self.store.rename_column(args.table, args.old, args.new)
        except (TableError, sqlite3.Error) as e:
            return f"rename_column error: {e}"


class DropColumnTool(Tool):
    name = "drop_column"
    description = ("Delete a column and its data from a table. Args: table, column. This is "
                  "irreversible. A primary-key or indexed column cannot be dropped. "
                  "Example: drop_column('sleep_log', 'old_notes').")

    class Params(BaseModel):
        table: str = Field(..., description="the table")
        column: str = Field(..., description="column to delete")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "DropColumnTool.Params") -> str:
        try:
            return self.store.drop_column(args.table, args.column)
        except (TableError, sqlite3.Error) as e:
            return f"drop_column error: {e}"


class RenameTableTool(Tool):
    name = "rename_table"
    description = ("Rename a whole table (its rows are kept). Args: old, new. The new name must "
                  "not already be in use. Example: rename_table('sleep_log', 'sleep_archive').")

    class Params(BaseModel):
        old: str = Field(..., description="current table name")
        new: str = Field(..., description="new table name")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "RenameTableTool.Params") -> str:
        try:
            return self.store.rename_table(args.old, args.new)
        except (TableError, sqlite3.Error) as e:
            return f"rename_table error: {e}"


class CopyTableTool(Tool):
    name = "copy_table"
    description = (
        "Copy rows from one table into another IN ONE CALL — use this instead of reading rows and "
        "insert_row'ing them one at a time. If dest doesn't exist it is created with the same "
        "columns, types, and primary key as source. If dest already exists, only columns shared by "
        "both are copied (extra dest columns stay empty). Args: source, dest, and optional where — "
        "a boolean filter to copy just some rows, e.g. \"date >= '2026-07-01'\" (a single expression, "
        "no semicolons). Example: copy_table('sleep_log', 'sleep_backup')."
    )

    class Params(BaseModel):
        source: str = Field(..., description="table to copy from")
        dest: str = Field(..., description="table to copy into (created if missing)")
        where: str | None = Field(None, description="optional boolean filter, e.g. \"date >= '2026-07-01'\"")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "CopyTableTool.Params") -> str:
        try:
            return self.store.copy_table(args.source, args.dest, args.where)
        except (TableError, sqlite3.Error) as e:
            return f"copy_table error: {e}"


class UpdateRowsTool(Tool):
    name = "update_rows"
    description = (
        "Change column values on EXISTING rows that match a filter — updates every row matching "
        "ALL of `match` at once. Args: table, set (a dict of column -> new value), and match (a "
        "dict of column -> value; only rows matching ALL pairs are changed). An empty match is "
        "refused (it would rewrite every row). Example: "
        "update_rows('tasks', {'status':'archived'}, {'year':2025})."
    )

    class Params(BaseModel):
        table: str = Field(..., description="the table")
        set: dict = Field(..., description="column -> new value")
        match: dict = Field(..., description="column -> value; rows matching ALL pairs are updated")

    def __init__(self, store: TableStore):
        self.store = store

    async def run(self, args: "UpdateRowsTool.Params") -> str:
        try:
            n = self.store.update_rows(args.table, args.set, args.match)
        except (TableError, sqlite3.Error) as e:
            return f"update_rows error: {e}"
        return f"update_rows: updated {n} row(s) in '{args.table}'."


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
