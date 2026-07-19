from engine.rules.detect import has_rule_cue
from engine.protocol import ModelResponse
from engine.engine import Engine
from config import Config


def test_cue_detection():
    assert has_rule_cue("Don't do that again")
    assert has_rule_cue("Always confirm before deleting")
    assert has_rule_cue("From now on, use metric units")
    assert has_rule_cue("never use emoji")
    assert not has_rule_cue("What's the weather in London?")
    assert not has_rule_cue("Thanks, that looks great")


class _FakeAux:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    async def chat(self, messages, tools=None, max_tokens=None,
                   temperature=None, think=None, reasoning=None):
        self.calls += 1
        assert think is False          # aux calls MUST disable the reasoning pass
        return ModelResponse(content=self.content, finish_reason="stop")


async def test_autodetect_saves_rule(tmp_path):
    e = Engine(Config(), data_dir=str(tmp_path))
    fake = _FakeAux("Never use emoji")
    e._aux_model_client = lambda: fake
    saved = await e.autodetect_rule("sess", "stop using emoji, don't do it again")
    assert [r["text"] for r in saved] == ["Never use emoji"]
    assert [r["source"] for r in e.rules_list()] == ["auto"]
    assert fake.calls == 1


async def test_autodetect_saves_nothing_on_none(tmp_path):
    e = Engine(Config(), data_dir=str(tmp_path))
    e._aux_model_client = lambda: _FakeAux("NONE")
    saved = await e.autodetect_rule("sess", "don't worry about it")
    assert saved == [] and e.rules_list() == []
