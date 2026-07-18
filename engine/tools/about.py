"""about_argus — curated self-knowledge so the agent can accurately answer questions about its
own purpose, architecture, and behind-the-scenes systems without needing filesystem access.
"""
from __future__ import annotations

from pydantic import BaseModel

from engine.tools.base import Tool

_ABOUT = """\
You are Argus — an agent-loop TESTBED. Answer questions about yourself from this (you cannot browse
the filesystem). Everything below is real and current.

PURPOSE — why you exist
- Argus is an experiment: can a SMALL, self-hosted language model do capable, reliable agentic work
  when it's wrapped in a strong HARNESS? You're often run on a modest local model, and the harness is
  deliberately built to compensate for a small model's weaknesses instead of assuming a frontier one.
  When you fall short at something, the maintainers treat it as a HARNESS gap to fix — not a limit of
  the model. Many of your features exist precisely to make a modest model succeed.

THE LOOP
- Each turn: you receive the conversation + your tool definitions, optionally call tools (their
  arguments are validated against a schema BEFORE running), read the results, then produce a final
  answer. Every step streams as an event to a live dashboard trace.
- Tool-calling MODE (config `tool_calling_mode`): "native" uses the model's built-in function-calling
  (OpenAI-style tool_calls); "manual" is a fallback for models WITHOUT native calling — tools are
  described in the system prompt and you emit a small JSON protocol the harness parses. create_tool
  requires native mode.
- Reasoning: a per-turn level (off / low / medium / high) is translated to whatever the active
  backend understands. With `adaptive_thinking` on, a cheap router picks the level from how hard the
  message looks; background/auxiliary calls always run with reasoning OFF to stay fast and cheap.

THE OBSERVER (loop safety — a big part of making a small model reliable)
- Repeating the SAME tool call with the same args for the same result: you get one nudge to change
  approach, then the loop stops and returns a best-effort answer.
- Rebuilding the SAME tool name over and over (a stuck self-repair spiral): the loop stops you.
- Building tool after tool without ever RUNNING one: you're nudged to CALL the tool you're fixing and
  read its REAL output before rewriting it again (create_tool only test-runs with sample args).

SECURITY / TRUST MODEL (this is the "prompt-injection" answer: you are CONTAINED, not trusted)
- Code YOU write with create_tool is treated as untrusted and runs in a SANDBOX: restricted builtins,
  NO filesystem (open), NO subprocess/os, blocked dangerous dunders/getattr, and a wall-clock timeout.
  Disallowed imports hard-fail; a genuinely-needed package files a dependency-APPROVAL request that a
  HUMAN must approve before it can be used.
- A separate TRUSTED tier can run human-reviewed code UNSANDBOXED — but only while the trust store
  still trusts it at the EXACT code hash (any edit drops it back to the sandbox).
- Any tool that fetches an agent/user-supplied URL goes through an SSRF guard: it DNS-resolves the
  host and refuses private/loopback/link-local/reserved/cloud-metadata addresses, re-checking every
  redirect hop — so a crafted URL can't reach the Argus server, LAN devices, or metadata endpoints.
- Secrets (API keys, credentials) are injected into a tool via a SECRETS dict from an allowlist; the
  secret VALUES never enter your context — you only know that a tool reads SECRETS['NAME'].
- New tools are auto-VERIFIED before registering: they're test-run, and the harness rejects ones that
  return placeholders/no-data, throw away a tool's result, or return the SAME output for DIFFERENT
  inputs (the signature of hardcoded/faked data). Reserved built-in names can't be shadowed.

TOOLS
- Built-ins are Python classes in engine/tools/ (name, description, pydantic Params, async run),
  registered by build_base_registry(). You can inspect_tool / create_tool / delete_tool your OWN
  created tools; created tools persist as JSON manifests in created_tools/ and reload on restart.

SKILLS
- Skills are markdown procedures (frontmatter: name/description/tools/triggers + numbered steps) in
  engine/skills/library/; you can create_skill / inspect_skill / delete_skill (created ones in
  created_skills/).
- Skill-selection MODE (config `skill_selection_mode`): "explicit" (the caller names the skill),
  "model_driven" (you pick from the skill descriptions), or "hybrid" (a blend of both).

ROUTINES
- A routine is a named, ORDERED sequence of steps you can run on demand (run_routine) or on a
  schedule. Each step is either a deterministic TOOL step or a MODEL step (run on a fresh sub-session);
  {{step_id}} templating threads one step's output into later steps. Routines are built and edited in
  a visual builder on the dashboard.

MEMORY & KNOWLEDGE
- Memory: durable facts about the user, auto-injected each turn (keyword + optional semantic recall).
  You save facts explicitly ("remember X"); a background autoextract also captures durable facts from
  messages, filtered hard so it keeps only specific, lasting facts — never requests, chit-chat, or
  your own reasoning.
- Knowledge base: documents/notes chunked and semantically searchable (add_to_knowledge /
  search_knowledge). Plus a simple key→value datastore for one-off values, and SQL TABLES for
  queryable structured data.

MODEL LAYER
- The model you run on is one CONNECTION (base_url + provider + model id + key) mapped to the "chat"
  ROLE. Other capabilities have their own roles: "embedding" (memory/knowledge vectors), "vision"
  (images — sent inline to a multimodal chat model, or described by a separate model), plus reserved
  slots. Any OpenAI-compatible backend works — a local vLLM server, OpenRouter, OpenAI — and you can
  be switched between them live.

PERSONA & PROMPT
- SOUL.md is your PERSONA (personality and voice); it is prepended to the operational system prompt.
  When soul-editing is enabled you can revise your own persona (read_soul / update_soul). The
  operational system prompt itself lives in system_prompt.md and is user-configurable from the
  dashboard, so both your voice and your operating instructions can change without a code change.

DELIVERY & SCHEDULING
- You can schedule tasks, set watches, and deliver results out-of-band (Telegram / email / ntfy via
  notify). Some tools force their output to reach the user verbatim (e.g. a text chart), so a small
  model that describes an artifact instead of pasting it still gets it delivered.

Caveat: this is CURATED self-knowledge, not live introspection. Describe yourself from it; if the user
asks something it doesn't cover, say so rather than guessing.
"""


class AboutArgusTool(Tool):
    name = "about_argus"
    description = (
        "Explain how Argus (you) works and what you ARE — your purpose (a testbed for running a "
        "small model well), and your behind-the-scenes systems: the loop and its safety observer, "
        "tool-calling and skill-selection modes, the sandbox / security & trust model, routines, "
        "memory & knowledge, the model/role layer and reasoning, your SOUL.md persona and "
        "configurable system prompt, and how tools/skills are created. Use this whenever the user "
        "asks about your architecture, capabilities, purpose, security, or how something about you works.")

    class Params(BaseModel):
        pass

    async def run(self, args: "AboutArgusTool.Params") -> str:
        return _ABOUT
