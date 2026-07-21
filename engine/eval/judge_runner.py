"""Judge backends for the eval tooling: turn a `--judge` spec into an async `fn(messages) -> str` that
returns the judge model's raw reply (parsed elsewhere by engine.eval.judge.parse_judge_reply).

Two backends:
- `claude:<model>` — the `claude -p` CLI (Opus/Fable via the user's subscription), run from a NEUTRAL
  cwd (/tmp) so the CLI agent grades cleanly instead of treating the grading text as a prompt-injection
  probe against the repo it's sitting in.
- anything else — an OpenAI-compatible ModelClient (`name` = the configured default, or
  `name=base_url|model`).
"""
from __future__ import annotations

import asyncio
import json


def _client_judge(base_url: str, model: str, api_key: str, timeout: float):
    from engine.model_client import ModelClient
    client = ModelClient(base_url, model, api_key, timeout=timeout)

    async def fn(messages: list[dict]) -> str:
        resp = await client.chat(messages, max_tokens=200, think=False, temperature=0.0)
        return resp.content or ""
    return fn


def _claude_judge(model: str):
    async def fn(messages: list[dict]) -> str:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        prompt = (system + "\n\n" + user) if system else user
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", model, "--output-format", "json",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd="/tmp")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        env = json.loads(out.decode() or "{}")
        return env.get("result", "")
    return fn


def make_judge(spec):
    """spec='claude:opus' -> the CLI/Opus backend; 'main' or 'name=url|model' -> a ModelClient. None -> None."""
    if not spec:
        return None
    if spec.startswith("claude:"):
        return _claude_judge(spec.split(":", 1)[1] or "opus")
    from config import Config
    cfg = Config()
    name, _, rhs = spec.partition("=")
    base_url, _, model = rhs.partition("|")
    return _client_judge(base_url.strip() or cfg.model_base_url,
                         (model.strip() or cfg.model_name) if rhs else cfg.model_name,
                         cfg.model_api_key, cfg.request_timeout)
