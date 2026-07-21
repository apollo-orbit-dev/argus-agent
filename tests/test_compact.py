"""Compaction must pass think=False to the summarizer.

Regression for the "empty summary" failure: this model is a reasoning model, and with
thinking left ON an auxiliary summary call spends its whole max_tokens budget on the hidden
reasoning pass and returns empty content (finish_reason 'length'). Every auxiliary call must
therefore disable thinking. This test locks that in for compact() so the bug can't return.
"""
from config import Config
from engine.engine import Engine
from engine.protocol import ModelResponse


class _FakeSummarizer:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        self.calls.append({"think": think, "max_tokens": max_tokens,
                           "user_text": messages[-1]["content"]})
        return ModelResponse(content=self.content, finish_reason="stop")


def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="main",
                 telegram_bot_token="", memory_scope="session")
    return Engine(cfg, data_dir=str(tmp_path))


async def test_compact_passes_think_false_and_shrinks(tmp_path):
    e = _engine(tmp_path)
    fake = _FakeSummarizer("Notes: user tracks daily sales in daily_sales; focus is revenue.")
    e._model_client = lambda: fake
    sid = "s"
    e.store.extend_messages(sid, [{"role": "user", "content": f"msg {i} " + "x" * 40}
                                  for i in range(10)])
    res = await e.compact(sid, keep_recent=2)

    assert res["compacted"] is True
    # the actual bug fix: thinking OFF for the auxiliary summary
    assert fake.calls and fake.calls[0]["think"] is False
    # history shrank to summary + the kept-recent messages
    conv = e.store.conversation(sid)
    assert len(conv) == 3
    assert conv[0]["content"].startswith("[Summary")


async def test_compact_reports_empty_summary_when_model_returns_blank(tmp_path):
    # if the model still returns nothing, compact must not silently corrupt history
    e = _engine(tmp_path)
    e._model_client = lambda: _FakeSummarizer("")
    sid = "s"
    seed = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    e.store.extend_messages(sid, seed)
    res = await e.compact(sid, keep_recent=2)

    assert res["compacted"] is False
    assert res["reason"] == "empty summary"
    assert len(e.store.conversation(sid)) == len(seed)  # history untouched


async def test_compact_bounds_transcript_for_huge_history(tmp_path):
    # a very large history is head+tail sampled, not sent whole and not truncated to only the start
    e = _engine(tmp_path)
    fake = _FakeSummarizer("ok")
    e._model_client = lambda: fake
    sid = "s"
    e.store.extend_messages(sid, [{"role": "user", "content": "HEAD_MARKER " + "a" * 60000},
                                  {"role": "assistant", "content": "b" * 60000},
                                  {"role": "user", "content": "TAIL_MARKER " + "c" * 200},
                                  {"role": "assistant", "content": "recent-1"},
                                  {"role": "user", "content": "recent-2"}])
    await e.compact(sid, keep_recent=2)
    sent = fake.calls[0]["user_text"]
    assert len(sent) < 45000                       # bounded, not the full ~120k chars
    assert "HEAD_MARKER" in sent                   # opening preserved
    assert "TAIL_MARKER" in sent                   # recent context preserved
    assert "earlier turns omitted" in sent
