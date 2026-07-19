"""Task 5: `origin` threads through run_task and is in scope for the whole turn.

Real (non-mocked) drive: build a real Engine (same fixture pattern as test_soul.py),
run a real turn through run_task, and assert the origin value reached the body
(stashed on the engine as `_last_run_origin` for the per-run registry-build block
that later tasks read to construct approval-aware tools).
"""
from config import Config
from engine.engine import DEFAULT_SOUL, Engine
from engine.protocol import ModelResponse


class _CaptureModel:
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        return ModelResponse(content="ok", finish_reason="stop")


def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    e = Engine(cfg)
    e._model_client = lambda: _CaptureModel()
    e._soul_file = tmp_path / "SOUL.md"
    e._system_prompt_file = tmp_path / "system_prompt.md"
    e.soul = DEFAULT_SOUL
    return e


async def test_run_task_accepts_origin_and_it_reaches_the_turn(tmp_path):
    e = _engine(tmp_path)
    await e.run_task("s", "hello", origin="telegram")
    assert e._last_run_origin == "telegram"


async def test_run_task_defaults_origin_to_api(tmp_path):
    e = _engine(tmp_path)
    await e.run_task("s", "hello")   # no origin passed -> defaults, doesn't break existing callers
    assert e._last_run_origin == "api"


async def test_run_task_origin_values_flow_through(tmp_path):
    e = _engine(tmp_path)
    for origin in ("dashboard", "telegram", "scheduled", "api"):
        await e.run_task("s", "hello", origin=origin)
        assert e._last_run_origin == origin
