"""Table store: create/insert/query/aggregate + the read-only safety guarantees."""
import asyncio

import pytest

from engine.tools.tables import (CreateTableTool, DropTableTool, InsertRowTool, ListTablesTool,
                                  QueryTableTool, TableError, TableStore)


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
