"""Table store: create/insert/query/aggregate + the read-only safety guarantees."""
import asyncio

import pytest

from engine.tools.tables import (AddColumnTool, CopyTableTool, CreateTableTool, DropColumnTool,
                                  DropTableTool, InsertRowTool, ListTablesTool, QueryTableTool,
                                  RenameColumnTool, RenameTableTool, TableError, TableStore,
                                  UpdateRowsTool)


def _store(tmp_path):
    return TableStore(str(tmp_path / "t.db"))


def test_create_insert_query_aggregate(tmp_path):
    s = _store(tmp_path)
    s.create_table("expenses", ["date:date", "category:text", "amount:real"])
    for d, c, a in [("2026-07-01", "food", 20), ("2026-07-02", "food", 30), ("2026-07-03", "gas", 45)]:
        s.insert("expenses", {"date": d, "category": c, "amount": a})
    rows = s.query("SELECT category, SUM(amount) AS total FROM expenses GROUP BY category ORDER BY total DESC")
    assert rows[0]["category"] == "food" and rows[0]["total"] == 50
    assert {r["category"] for r in rows} == {"food", "gas"}
    # WHERE + date range
    r2 = s.query("SELECT COUNT(*) AS n FROM expenses WHERE date >= '2026-07-02'")
    assert r2[0]["n"] == 2


def test_rows_pagination_and_total(tmp_path):
    s = _store(tmp_path)
    s.create_table("log", ["n:integer"])
    for i in range(120):
        s.insert("log", {"n": i})
    page = s.rows("log", limit=50, offset=0)
    assert page["total"] == 120 and page["limit"] == 50 and page["offset"] == 0
    assert page["columns"] == ["n"] and len(page["rows"]) == 50 and page["rows"][0]["n"] == 0
    page2 = s.rows("log", limit=50, offset=100)
    assert len(page2["rows"]) == 20 and page2["rows"][0]["n"] == 100      # last partial page
    assert s.rows("log", limit=9999)["limit"] <= 500                     # limit is bounded


def test_rows_validates_name_and_existence(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    with pytest.raises(TableError):
        s.rows("no_such_table")                 # missing table -> error
    with pytest.raises(TableError):
        s.rows("bad name; DROP TABLE t")        # invalid identifier -> rejected before any SQL


def test_query_rejects_non_select(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    for bad in ("DROP TABLE t", "DELETE FROM t", "UPDATE t SET x=1", "INSERT INTO t VALUES (1)"):
        with pytest.raises(TableError):
            s.query(bad)


def test_query_readonly_connection_blocks_writes(tmp_path):
    """Even a SELECT that sneaks a write-ish form can't mutate — the ro connection errors."""
    import sqlite3
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    s.insert("t", {"x": 5})
    with pytest.raises(TableError):
        s.query("SELECT 1; DROP TABLE t")             # multi-statement rejected
    # table still intact
    assert s.query("SELECT COUNT(*) AS n FROM t")[0]["n"] == 1
    # a write attempted through the ro connection directly raises OperationalError
    with pytest.raises(sqlite3.OperationalError):
        s._ro.execute("INSERT INTO t VALUES (9)")


def test_invalid_identifiers_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(TableError):
        s.create_table("bad name", ["x"])            # space in name
    with pytest.raises(TableError):
        s.create_table("t", ["1col"])                # column starts with digit


def test_list_and_drop(tmp_path):
    s = _store(tmp_path)
    s.create_table("a", ["x"]); s.create_table("b", ["y"])
    names = {t["name"] for t in s.tables()}
    assert names == {"a", "b"}
    assert s.drop("a") is True and s.drop("a") is False
    assert {t["name"] for t in s.tables()} == {"b"}


def test_tools_end_to_end(tmp_path):
    s = _store(tmp_path)
    ct = CreateTableTool(s); it = InsertRowTool(s); qt = QueryTableTool(s)
    lt = ListTablesTool(s); dt = DropTableTool(s)
    assert "ready" in asyncio.run(ct.run(ct.Params(name="readings", columns=["day:text", "score:integer"])))
    asyncio.run(it.run(it.Params(table="readings", values={"day": "Mon", "score": 82})))
    asyncio.run(it.run(it.Params(table="readings", values={"day": "Tue", "score": 90})))
    out = asyncio.run(qt.run(qt.Params(sql="SELECT AVG(score) AS avg FROM readings")))
    assert "86" in out
    assert "readings" in asyncio.run(lt.run(lt.Params()))
    # a bad query returns an error string, not a crash
    assert "error" in asyncio.run(qt.run(qt.Params(sql="DELETE FROM readings"))).lower()
    assert "dropped" in asyncio.run(dt.run(dt.Params(name="readings")))


# ---- primary key + upsert (keyed daily-log support) ----

def test_key_column_creates_primary_key(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "score:integer", "notes:text"])
    pk = [r["name"] for r in s._rw.execute("PRAGMA table_info(readings)") if r["pk"]]
    assert pk == ["date"]


def test_reinsert_same_key_updates_not_duplicates(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "score:integer", "notes:text"])
    s.insert("readings", {"date": "2026-07-15", "score": 79, "notes": "ok"})
    s.insert("readings", {"date": "2026-07-15", "score": 82, "notes": "better"})   # same date -> upsert
    rows = s.query("SELECT * FROM readings")
    assert len(rows) == 1                                   # one row per date, no duplicate
    assert rows[0]["score"] == 82 and rows[0]["notes"] == "better"


def test_upsert_only_overwrites_provided_columns(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "score:integer", "notes:text"])
    s.insert("readings", {"date": "2026-07-15", "score": 79, "notes": "first note"})
    s.insert("readings", {"date": "2026-07-15", "score": 88})   # no notes -> keep the old note
    rows = s.query("SELECT * FROM readings")
    assert len(rows) == 1 and rows[0]["score"] == 88 and rows[0]["notes"] == "first note"


def test_no_key_table_allows_duplicates(tmp_path):
    s = _store(tmp_path)
    s.create_table("log", ["date:date", "msg:text"])         # no :key
    s.insert("log", {"date": "2026-07-15", "msg": "a"})
    s.insert("log", {"date": "2026-07-15", "msg": "b"})
    assert len(s.query("SELECT * FROM log")) == 2             # append-only, as before


def test_two_key_columns_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(TableError):
        s.create_table("x", ["a:integer:key", "b:text:key"])


def test_list_marks_pk(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "score:integer"])
    cols = next(t["columns"] for t in s.tables() if t["name"] == "readings")
    assert any("date" in c and "PK" in c for c in cols)


def test_insert_coerces_nonscalar_to_json(tmp_path):
    # a dict/list value (e.g. a nested API payload forwarded verbatim) must NOT raise a bind
    # error and silently drop the row — it is stored as JSON text and the row persists.
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "samples:text", "score:integer"])
    s.insert("readings", {"date": "2026-06-20", "samples": [{"level": 70}, {"level": 40}], "score": 82})
    rows = s.query("SELECT * FROM readings WHERE date='2026-06-20'")
    assert len(rows) == 1
    assert rows[0]["score"] == 82
    assert "70" in rows[0]["samples"]     # stored as JSON text, not lost


def test_json_type_alias_roundtrips(tmp_path):
    # 'json'/'list' are self-documenting aliases for TEXT; a real list inserted into a json column is
    # stored as JSON text (via _coerce) and is queryable with json_extract/json_array_length.
    s = _store(tmp_path)
    s.create_table("recipes", ["name:text:key", "ingredients:json", "steps:list"])
    cols = {c["name"]: c["type"] for c in s._rw.execute("PRAGMA table_info(recipes)")}
    assert cols["ingredients"] == "TEXT" and cols["steps"] == "TEXT"
    s.insert("recipes", {"name": "Pancakes",
                         "ingredients": ["flour", "eggs", "milk"],
                         "steps": ["mix", "cook"]})
    rows = s.query("SELECT json_extract(ingredients, '$[1]') AS second, "
                   "json_array_length(ingredients) AS n FROM recipes WHERE name='Pancakes'")
    assert rows == [{"second": "eggs", "n": 3}]


def test_delete_row_removes_matching(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key", "score:integer"])
    s.insert("readings", {"date": "2026-06-20", "score": 40})
    s.insert("readings", {"date": "2026-06-21", "score": 80})
    n = s.delete_rows("readings", {"date": "2026-06-20"})
    assert n == 1
    assert [r["date"] for r in s.query("SELECT date FROM readings")] == ["2026-06-21"]


def test_delete_row_requires_match(tmp_path):
    s = _store(tmp_path)
    s.create_table("readings", ["date:date:key"])
    s.insert("readings", {"date": "2026-06-20"})
    with pytest.raises(TableError):
        s.delete_rows("readings", {})           # refuse to nuke the whole table
    assert len(s.query("SELECT * FROM readings")) == 1


# ---- add_column ----

def test_add_column_adds_typed_column(tmp_path):
    s = _store(tmp_path)
    s.create_table("sleep_log", ["date:date:key", "score:integer"])
    s.insert("sleep_log", {"date": "2026-07-15", "score": 80})
    msg = s.add_column("sleep_log", "sleep_start:text")
    cols = {c["name"]: c["type"] for c in s._rw.execute("PRAGMA table_info(sleep_log)")}
    assert cols["sleep_start"] == "TEXT" and "sleep_start" in msg
    # existing row gets NULL in the new column
    assert s.query("SELECT sleep_start FROM sleep_log")[0]["sleep_start"] is None


def test_add_column_rejects_key_flag(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    with pytest.raises(TableError):
        s.add_column("t", "y:integer:key")     # cannot add a PK to an existing table


def test_add_column_missing_table(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(TableError):
        s.add_column("nope", "y:text")


def test_add_column_tool(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    tool = AddColumnTool(s)
    out = asyncio.run(tool.run(tool.Params(table="t", column="note:text")))
    assert "note" in out and "error" not in out.lower()


# ---- rename_column / drop_column / rename_table ----

def test_rename_column_preserves_data(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["a:integer"])
    s.insert("t", {"a": 7})
    s.rename_column("t", "a", "b")
    assert s._columns("t") == ["b"]
    assert s.query("SELECT b FROM t")[0]["b"] == 7


def test_rename_column_missing_column(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["a:integer"])
    with pytest.raises(TableError):
        s.rename_column("t", "nope", "b")


def test_drop_column_removes_it(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["a:integer", "b:text"])
    s.drop_column("t", "b")
    assert s._columns("t") == ["a"]


def test_drop_column_pk_surfaces_error(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["a:integer:key", "b:text"])
    with pytest.raises(TableError):
        s.drop_column("t", "a")            # dropping a PK column -> sqlite error -> TableError
    assert "a" in s._columns("t")          # still there


def test_rename_table(tmp_path):
    s = _store(tmp_path)
    s.create_table("old", ["x:integer"])
    s.rename_table("old", "new")
    names = {t["name"] for t in s.tables()}
    assert names == {"new"}


def test_rename_table_collision(tmp_path):
    s = _store(tmp_path)
    s.create_table("a", ["x:integer"]); s.create_table("b", ["y:integer"])
    with pytest.raises(TableError):
        s.rename_table("a", "b")           # target already exists


def test_alter_injection_rejected(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["x:integer"])
    with pytest.raises(TableError):
        s.rename_table("t", "n; DROP TABLE t")   # invalid identifier rejected before any SQL
    assert {tt["name"] for tt in s.tables()} == {"t"}


def test_alter_tools(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["a:integer", "b:text"])
    assert "renamed" in asyncio.run(RenameColumnTool(s).run(RenameColumnTool(s).Params(table="t", old="a", new="c")))
    assert "dropped" in asyncio.run(DropColumnTool(s).run(DropColumnTool(s).Params(table="t", column="b")))
    assert "renamed" in asyncio.run(RenameTableTool(s).run(RenameTableTool(s).Params(old="t", new="t2")))


def test_copy_table_creates_dest_mirroring_schema(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["date:date:key", "score:integer"])
    s.insert("src", {"date": "2026-07-15", "score": 80})
    s.insert("src", {"date": "2026-07-16", "score": 90})
    msg = s.copy_table("src", "dst")
    # dest created with same columns, types, and PK
    info = {c["name"]: (c["type"], c["pk"]) for c in s._rw.execute("PRAGMA table_info(dst)")}
    assert info["date"] == ("TEXT", 1) and info["score"] == ("INTEGER", 0)
    assert s.query("SELECT COUNT(*) AS n FROM dst")[0]["n"] == 2 and "2" in msg


def test_copy_table_into_existing_dest_maps_shared_columns(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["date:date:key", "score:integer"])
    s.insert("src", {"date": "2026-07-15", "score": 80})
    # dest has an extra column not in src
    s.create_table("dst", ["date:date:key", "score:integer", "sleep_start:text"])
    s.copy_table("src", "dst")
    row = s.query("SELECT * FROM dst WHERE date='2026-07-15'")[0]
    assert row["score"] == 80 and row["sleep_start"] is None   # extra dest column stays NULL


def test_copy_table_copies_more_than_500_rows(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["n:integer"])
    for i in range(561):
        s.insert("src", {"n": i})
    s.copy_table("src", "dst")                                  # no read-path 500-row cap
    assert s.query("SELECT COUNT(*) AS n FROM dst")[0]["n"] == 561


def test_copy_table_with_where_filters(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["date:text", "score:integer"])
    for d, v in [("2026-06-30", 1), ("2026-07-01", 2), ("2026-07-02", 3)]:
        s.insert("src", {"date": d, "score": v})
    n_msg = s.copy_table("src", "dst", where="date >= '2026-07-01'")
    assert s.query("SELECT COUNT(*) AS n FROM dst")[0]["n"] == 2 and "2" in n_msg


def test_copy_table_rejects_semicolon_where(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["x:integer"]); s.insert("src", {"x": 1})
    with pytest.raises(TableError):
        s.copy_table("src", "dst", where="1=1; DROP TABLE src")
    # src intact; dst not populated with junk
    assert s.query("SELECT COUNT(*) AS n FROM src")[0]["n"] == 1


def test_copy_table_missing_source(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(TableError):
        s.copy_table("nope", "dst")


def test_copy_table_tool(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["x:integer"]); s.insert("src", {"x": 1})
    tool = CopyTableTool(s)
    out = asyncio.run(tool.run(tool.Params(source="src", dest="dst")))
    assert "copied" in out.lower() and "error" not in out.lower()


def test_copy_table_filtered_conflict_counts_actual_inserts(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["date:date:key", "score:integer"])
    for d, v in [("2026-07-01", 1), ("2026-07-02", 2), ("2026-07-03", 3)]:
        s.insert("src", {"date": d, "score": v})
    # dest has a PK and a pre-existing row whose key collides with a filtered source row
    s.create_table("dst", ["date:date:key", "score:integer"])
    s.insert("dst", {"date": "2026-07-02", "score": 999})   # colliding key, protected by DO NOTHING
    # filter selects the colliding key (07-02) plus a new one (07-03)
    msg = s.copy_table("src", "dst", where="date >= '2026-07-02'")
    # (1) reported count is only the newly-inserted row (1), not the 2 rows read
    assert "1 row" in msg and "2 row" not in msg
    # (2) the pre-existing colliding row is untouched (DO NOTHING protected it)
    assert s.query("SELECT score FROM dst WHERE date='2026-07-02'")[0]["score"] == 999
    assert s.query("SELECT COUNT(*) AS n FROM dst")[0]["n"] == 2      # the one collision + one new


def test_copy_table_whole_conflict_counts_actual_inserts(tmp_path):
    s = _store(tmp_path)
    s.create_table("src", ["date:date:key", "score:integer"])
    s.insert("src", {"date": "2026-07-01", "score": 1})
    s.insert("src", {"date": "2026-07-02", "score": 2})
    s.create_table("dst", ["date:date:key", "score:integer"])
    s.insert("dst", {"date": "2026-07-01", "score": 999})   # collides with one source row
    msg = s.copy_table("src", "dst")                        # whole-table path
    assert "1 row" in msg and "2 row" not in msg            # only the non-colliding row inserted
    assert s.query("SELECT score FROM dst WHERE date='2026-07-01'")[0]["score"] == 999


# ---- update_rows ----

def test_update_rows_updates_only_matching(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["id:integer:key", "status:text"])
    s.insert("t", {"id": 1, "status": "open"})
    s.insert("t", {"id": 2, "status": "open"})
    n = s.update_rows("t", {"status": "closed"}, {"id": 1})
    assert n == 1
    rows = {r["id"]: r["status"] for r in s.query("SELECT id, status FROM t")}
    assert rows == {1: "closed", 2: "open"}


def test_update_rows_empty_match_refused(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["id:integer", "status:text"])
    s.insert("t", {"id": 1, "status": "open"})
    with pytest.raises(TableError):
        s.update_rows("t", {"status": "closed"}, {})     # empty match would hit every row
    assert s.query("SELECT status FROM t")[0]["status"] == "open"


def test_update_rows_empty_set_refused(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["id:integer"])
    s.insert("t", {"id": 1})
    with pytest.raises(TableError):
        s.update_rows("t", {}, {"id": 1})


def test_update_rows_coerces_nonscalar(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["id:integer:key", "tags:json"])
    s.insert("t", {"id": 1, "tags": ["a"]})
    s.update_rows("t", {"tags": ["x", "y"]}, {"id": 1})       # list -> JSON text, not a bind error
    assert "y" in s.query("SELECT tags FROM t WHERE id=1")[0]["tags"]


def test_update_rows_tool(tmp_path):
    s = _store(tmp_path)
    s.create_table("t", ["id:integer:key", "status:text"])
    s.insert("t", {"id": 1, "status": "open"})
    tool = UpdateRowsTool(s)
    out = asyncio.run(tool.run(tool.Params(table="t", set={"status": "done"}, match={"id": 1})))
    assert "1" in out and "error" not in out.lower()
