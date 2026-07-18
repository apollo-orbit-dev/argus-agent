"""ask_data — NL question -> schema-grounded SQL -> answer, with error-driven self-repair."""
import asyncio

import json

from engine.protocol import ModelResponse
from engine.tools.tables import AskDataTool, QueryRowsTool, TableStore, _clean_sql


class _FakeClient:
    """Returns queued SQL replies in order; records the messages it was called with."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        self.calls.append(messages)
        content = self._replies.pop(0) if self._replies else ""
        return ModelResponse(content=content, finish_reason="stop")


def _store(tmp_path):
    s = TableStore(str(tmp_path / "t.db"))
    s.create_table("daily_sales", ["date:date:key", "revenue:int", "units:int"])
    for d, rev, units in [("2026-07-01", 90, 60), ("2026-07-02", 100, 55), ("2026-07-03", 80, 70)]:
        s.insert("daily_sales", {"date": d, "revenue": rev, "units": units})
    return s


def _ask(store, client, question):
    tool = AskDataTool(store, lambda: client)
    return asyncio.run(tool.run(tool.Params(question=question)))


# ---- SQL extraction ----
def test_clean_sql_strips_fences_and_prose():
    assert _clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert _clean_sql("Here is the query: SELECT AVG(revenue) FROM daily_sales;") == \
        "SELECT AVG(revenue) FROM daily_sales"
    assert _clean_sql("WITH x AS (SELECT 1) SELECT * FROM x") == "WITH x AS (SELECT 1) SELECT * FROM x"


# ---- happy path ----
def test_ask_data_runs_generated_sql(tmp_path):
    store = _store(tmp_path)
    client = _FakeClient(["SELECT AVG(revenue) AS avg_rev FROM daily_sales"])
    out = _ask(store, client, "What's my average revenue?")
    assert "avg_rev" in out and "90.0" in out          # (90+100+80)/3
    assert "Query:" in out                              # SQL is surfaced for transparency
    assert len(client.calls) == 1                       # got it on the first try


def test_ask_data_no_tables(tmp_path):
    store = TableStore(str(tmp_path / "empty.db"))
    out = _ask(store, _FakeClient(["SELECT 1"]), "anything")
    assert "no tables yet" in out


# ---- self-repair: bad SQL then corrected SQL ----
def test_ask_data_repairs_after_sql_error(tmp_path):
    store = _store(tmp_path)
    # first reply references a non-existent column -> sqlite error -> repair -> valid query
    client = _FakeClient([
        "SELECT AVG(revenues) FROM daily_sales",           # wrong column name
        "SELECT AVG(revenue) AS avg_rev FROM daily_sales", # corrected
    ])
    out = _ask(store, client, "average revenue")
    assert "90.0" in out
    assert len(client.calls) == 2                          # one repair round
    # the repair prompt fed the actual SQL error back to the model
    repair_msg = client.calls[1][-1]["content"].lower()
    assert "failed" in repair_msg and ("no such column" in repair_msg or "revenues" in repair_msg)


def test_ask_data_gives_up_after_max_attempts(tmp_path):
    store = _store(tmp_path)
    client = _FakeClient(["SELECT nope FROM daily_sales"] * 5)   # always wrong
    out = _ask(store, client, "average revenue")
    assert "couldn't produce a working query" in out
    assert len(client.calls) == 3                          # first + 2 repairs, then give up


# ---- refuses to fabricate: CANNOT sentinel ----
def test_ask_data_cannot_answer(tmp_path):
    store = _store(tmp_path)
    out = _ask(store, _FakeClient(["CANNOT"]), "what's my profit margin?")
    assert "can't answer that from your tables" in out
    assert "daily_sales" in out                            # shows what's available


# ---- write attempts are refused by the read-only query layer, then repaired away ----
def test_ask_data_rejects_non_select(tmp_path):
    store = _store(tmp_path)
    client = _FakeClient([
        "DELETE FROM daily_sales",                          # not a SELECT -> TableError
        "SELECT COUNT(*) AS n FROM daily_sales",            # repaired to a read
    ])
    out = _ask(store, client, "how many days")
    assert "n" in out and "3" in out
    assert len(client.calls) == 2


# ---- query_rows: JSON output for feeding structured data into a later step ----
import asyncio  # noqa: E402


def test_query_rows_returns_json(tmp_path):
    store = _store(tmp_path)
    tool = QueryRowsTool(store)
    out = asyncio.run(tool.run(tool.Params(
        sql="SELECT date AS label, revenue AS value FROM daily_sales ORDER BY date")))
    rows = json.loads(out)                                  # parseable JSON
    assert rows == [{"label": "2026-07-01", "value": 90},
                    {"label": "2026-07-02", "value": 100},
                    {"label": "2026-07-03", "value": 80}]


def test_query_rows_error_is_a_string(tmp_path):
    store = _store(tmp_path)
    tool = QueryRowsTool(store)
    out = asyncio.run(tool.run(tool.Params(sql="SELECT nope FROM daily_sales")))
    assert "query_rows error" in out
