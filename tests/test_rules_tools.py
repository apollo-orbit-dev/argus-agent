from engine.rules.store import RulesStore
from engine.tools.rules import SaveRuleTool, ListRulesTool, RemoveRuleTool


async def test_save_and_list(tmp_path):
    store = RulesStore(str(tmp_path / "rules.json"))
    save = SaveRuleTool(store)
    out = await save.run(save.Params(rule="Always confirm before deleting files"))
    assert "Always confirm before deleting files" in out
    lst = ListRulesTool(store)
    listed = await lst.run(lst.Params())
    assert "Always confirm before deleting files" in listed
    rec = store.list()[0]
    assert rec["id"] in listed                     # id shown so the model can remove it


async def test_save_rejects_empty(tmp_path):
    store = RulesStore(str(tmp_path / "rules.json"))
    save = SaveRuleTool(store)
    out = await save.run(save.Params(rule="   "))
    assert store.list() == [] and "empty" in out.lower()


async def test_remove(tmp_path):
    store = RulesStore(str(tmp_path / "rules.json"))
    r = store.add("Never use emoji")
    rm = RemoveRuleTool(store)
    assert "removed" in (await rm.run(rm.Params(rule_id=r["id"]))).lower()
    assert store.list() == []
    assert "no rule" in (await rm.run(rm.Params(rule_id="missing"))).lower()


async def test_list_empty(tmp_path):
    store = RulesStore(str(tmp_path / "rules.json"))
    lst = ListRulesTool(store)
    assert "no standing rules" in (await lst.run(lst.Params())).lower()
