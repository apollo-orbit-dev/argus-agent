"""Adaptive reasoning router: heuristics settle obvious cases, classifier the middle → a LEVEL
(off | low | medium | high) that ModelClient translates per backend."""
import types

from config import Config
from engine.engine import Engine


def _engine():
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))


def _stub_classifier(e, reply):
    calls = {"n": 0}

    async def _chat(messages, **kw):
        calls["n"] += 1
        return types.SimpleNamespace(content=reply)
    e._model_client = lambda: types.SimpleNamespace(chat=_chat)
    return calls


async def test_ack_is_off():
    e = _engine()
    calls = _stub_classifier(e, "high")
    assert await e._route_reasoning("thanks!") == "off"
    assert await e._route_reasoning("ok") == "off"
    assert calls["n"] == 0                                     # no classifier needed


async def test_hard_reasoning_is_high():
    e = _engine()
    _stub_classifier(e, "low")
    assert await e._route_reasoning("why does my code crash on empty input?") == "high"
    assert await e._route_reasoning("compare React and Vue for my use case") == "high"


async def test_general_task_is_medium():
    e = _engine()
    _stub_classifier(e, "low")
    assert await e._route_reasoning("build me a dashboard of my sales data") == "medium"
    assert await e._route_reasoning("write a short report of the results") == "medium"


async def test_long_prompt_is_high():
    e = _engine()
    _stub_classifier(e, "low")
    assert await e._route_reasoning(" ".join(["word"] * 35)) == "high"


async def test_short_factual_is_off():
    e = _engine()
    calls = _stub_classifier(e, "high")
    assert await e._route_reasoning("capital of France?") == "off"
    assert calls["n"] == 0


async def test_uncertain_middle_uses_classifier():
    e = _engine()
    calls = _stub_classifier(e, "low")
    mid = "the weather outside today near my house by the water and the trees around"
    assert await e._route_reasoning(mid) == "low"              # classifier's word
    assert calls["n"] == 1


async def test_unrecognized_classifier_word_defaults_medium():
    e = _engine()
    _stub_classifier(e, "banana")
    mid = "the situation with the neighbors and the fence and the shared driveway again"
    assert await e._route_reasoning(mid) == "medium"


async def test_honesty_risk_is_high():
    """Short 'you can't know this' probes must reason hard (else the model guesses)."""
    e = _engine()
    calls = _stub_classifier(e, "low")   # classifier would say low; honesty guard wins first
    assert await e._route_reasoning("What am I holding in my left hand right now?") == "high"
    assert await e._route_reasoning("What's in my pocket?") == "high"
    assert calls["n"] == 0
    assert await e._route_reasoning("capital of France?") == "off"   # plain short fact still off


async def test_classifier_failure_defaults_high():
    e = _engine()

    async def _boom(messages, **kw):
        raise RuntimeError("model down")
    e._model_client = lambda: types.SimpleNamespace(chat=_boom)
    mid = "the situation with the neighbors and the fence and the shared driveway again"
    assert await e._route_reasoning(mid) == "high"             # safe fallback → reason more


# ---- adaptive routing is gated by reasoning == "auto" (pinned levels win) ----
def test_adaptive_active_only_on_auto():
    def eng(adaptive, level):
        return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                             adaptive_thinking=adaptive, model_reasoning=level))
    assert eng(True, "auto")._adaptive_reasoning_active()          # auto + adaptive → router runs
    assert not eng(True, "high")._adaptive_reasoning_active()      # pinned HIGH → router steps aside
    assert not eng(True, "off")._adaptive_reasoning_active()       # pinned OFF → respected
    assert not eng(False, "auto")._adaptive_reasoning_active()     # adaptive off → never routes
    assert eng(True, "AUTO")._adaptive_reasoning_active()            # case-insensitive auto
