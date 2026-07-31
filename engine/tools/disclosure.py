"""Progressive tool disclosure — show a small model the K most relevant tools, not all ~35.

THE LOAD-BEARING DESIGN DECISION: disclosure narrows PRESENTATION ONLY.

`engine/loop.py` uses one registry for two jobs — presentation (`build_request` ->
openai_schema/text_schema) and dispatch (`validate`, `get`). If a hidden tool were dropped from the
registry the loop dispatches through, hiding it would make it UNEXECUTABLE and would make the
"unknown tool 'x'. Known tools: ..." error in engine/tools/base.py lie about what exists.

So `DisclosedRegistry` shares `_tools` BY REFERENCE with the full registry: `get()`, `validate()`,
`names()` and `list()` all still see EVERY tool. Only `openai_schema()`/`text_schema()` are filtered.
A hidden tool is "not advertised this turn", never "unavailable" — a model that remembers a tool from
earlier in the conversation just calls it and it runs. Filtering dispatch would be a different, far
more dangerous feature.

Selection itself is pure and unit-testable: no model call, no network. Embeddings, when configured,
are computed by the caller and passed in; a failure there degrades to keyword ranking for that turn
rather than raising or blocking the turn.
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from engine.textmatch import tokens
from engine.tools.base import Tool, ToolRegistry

log = logging.getLogger("argus.disclosure")

# Scoring weights, deliberately mirroring the skill selector (engine/skills/selection/explicit.py):
# an exact name hit is the strong precise signal, descriptive overlap is the weak one, and a strong
# embedding match (~0.9 cosine) is worth about as much as a name hit.
W_NAME_HIT = 5.0
W_OVERLAP = 2.0
W_EMB = 3.0


def tool_doc(tool: Tool) -> str:
    """The text a tool is RANKED on: its name, its description, and its parameter names +
    descriptions. Parameters matter — 'currency_convert' says little, but its `from_currency` /
    `amount` params carry the words a user actually types."""
    parts = [getattr(tool, "name", ""), getattr(tool, "description", "") or ""]
    params = getattr(tool, "Params", None)
    schema = None
    if params is not None and hasattr(params, "model_json_schema"):
        try:
            schema = params.model_json_schema()
        except Exception:           # a hand-built model can fail to render; ranking must not care
            schema = None
    for pname, pinfo in ((schema or {}).get("properties") or {}).items():
        parts.append(str(pname))
        desc = pinfo.get("description") if isinstance(pinfo, dict) else None
        if desc:
            parts.append(str(desc))
    return " ".join(p for p in parts if p)


def doc_key(doc: str, model: str = "") -> str:
    """Cache key for an embedded tool doc — content-addressed, so a tool whose description changes
    (create_tool rewriting one mid-session) is re-embedded and a stable one never is. `model` is
    folded in too: the cache is process-lifetime and a PATCH to embedding_model/embedding_base_url
    can happen mid-process, so a doc-only key would silently keep serving vectors from the OLD
    model/endpoint (dimension mismatches degrade to a silent 0.0 cosine — no error, just worse
    ranking). Keying on the model discriminator makes a config change a cache MISS, not stale data."""
    return hashlib.sha1(f"{model}\x00{doc}".encode("utf-8")).hexdigest()


def cosine(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def score_tool(tool: Tool, qtok: set[str], query_l: str,
               emb: Optional[float] = None) -> float:
    """Relevance of one tool to one user message. Pure: no model, no network, no I/O.

    - name hit (+5): the tool's name appears verbatim in the message, raw or with '_' as a space.
    - overlap (x2): fraction of the message's content words that appear in the tool's doc.
    - embedding (x3): cosine similarity, clamped to [0,1].

    Mode is expressed through the ARGUMENTS, not a branch: keyword ranking passes `emb=None`,
    embedding-only ranking passes an empty `qtok` (so the overlap term is exactly 0), and hybrid
    passes both.
    """
    score = 0.0
    name = (getattr(tool, "name", "") or "").lower()
    if name and (name in query_l or name.replace("_", " ") in query_l):
        score += W_NAME_HIT
    if qtok:
        doctok = tokens(tool_doc(tool))
        score += W_OVERLAP * (len(qtok & doctok) / max(1, len(qtok)))
    if emb is not None:
        score += W_EMB * max(0.0, min(1.0, emb))
    return score


def rank_tools(registry: ToolRegistry, user_text: str, *, mode: str = "keyword",
               doc_embs: Optional[dict[str, list[float]]] = None,
               query_emb: Optional[list[float]] = None,
               exclude: Iterable[str] = (),
               include_denied: bool = False) -> list[tuple[str, float]]:
    """Every tool in `registry`, best first. Ties break on REGISTRY INSERTION ORDER, so the same
    inputs always produce the same view.

    Denied tools are skipped by default: they can never be advertised, so letting one rank would
    silently spend a slot of the K budget on a tool the schema builders then drop. `find_tool`
    passes include_denied=True because it needs to know a match was blocked in order to SAY so."""
    embeddings_usable = bool(doc_embs) and bool(query_emb)
    if mode in ("embedding", "hybrid") and not embeddings_usable:
        # Embeddings unconfigured or the request failed — fall back to keyword FOR THIS TURN.
        mode = "keyword"
    qtok = set() if mode == "embedding" else tokens(user_text)
    query_l = (user_text or "").lower()
    skip = set(exclude)
    if not include_denied:
        skip |= registry.denied_names()
    scored: list[tuple[float, int, str]] = []
    for i, tool in enumerate(registry.list()):
        if tool.name in skip:
            continue
        emb = None
        if mode in ("embedding", "hybrid"):
            emb = cosine(query_emb, (doc_embs or {}).get(tool.name))
        scored.append((-score_tool(tool, qtok, query_l, emb), i, tool.name))
    scored.sort()
    return [(name, -neg) for neg, _i, name in scored]


def select_visible(registry: ToolRegistry, user_text: str, *, mode: str = "keyword", k: int = 12,
                   core: Iterable[str] = (), pinned: Iterable[str] = (),
                   doc_embs: Optional[dict[str, list[float]]] = None,
                   query_emb: Optional[list[float]] = None) -> set[str]:
    """The set of tool names this turn ADVERTISES. Pure — safe to call in a unit test.

    `core` (never hidden) and `pinned` (the active skill's declared tools + ctx.extra_tools) are
    both unconditional: if together they already meet or exceed K, they ARE the view. K is a soft
    budget that pins always win, because a skill naming a tool the model can't see is worse than a
    slightly larger schema block. Names not registered (a gated-off dependency, a typo in the
    configured core list) are ignored silently.
    """
    # Denied tools are not advertisable, so they are not selectable either — not even via `core`
    # or `pinned`. A denied name in the configured core list would otherwise burn a slot of K on a
    # tool openai_schema()/text_schema() then drop.
    known = set(registry.names()) - registry.denied_names()
    keep = {n for n in core if n in known} | {n for n in pinned if n in known}
    if len(keep) >= k:
        return set(keep)
    budget = k - len(keep)
    ranked = rank_tools(registry, user_text, mode=mode, doc_embs=doc_embs,
                        query_emb=query_emb, exclude=keep)
    return keep | {name for name, _s in ranked[:budget]}


async def embed_tool_docs(embedder, registry: ToolRegistry, user_text: str,
                          cache: dict[str, list[float]]):
    """(doc_embs, query_emb) for `registry`, or (None, None). NEVER raises, never blocks a turn.

    Tool docs are batch-embedded once and cached in-process by sha1(tool_doc), so only the query
    costs a request per turn. Any failure — unconfigured endpoint, HTTP error, mismatched batch —
    returns (None, None) and the caller ranks by keyword for that turn.
    """
    if embedder is None or not getattr(embedder, "configured", False):
        return None, None
    try:
        # Discriminator folded into every cache key (see doc_key): a mid-process config PATCH that
        # switches embedding_model/embedding_base_url must miss the cache, not silently serve
        # vectors from the old model/endpoint.
        disc = f"{getattr(embedder, 'model', '')}\x00{getattr(embedder, 'base_url', '')}"
        docs = {t.name: tool_doc(t) for t in registry.list()}
        missing = sorted({d for d in docs.values() if doc_key(d, disc) not in cache})
        if missing:
            vecs = await embedder.embed(missing)
            if not vecs or len(vecs) != len(missing):
                return None, None
            for doc, vec in zip(missing, vecs):
                cache[doc_key(doc, disc)] = vec
        query_emb = await embedder.embed_one(user_text)
        if not query_emb:
            return None, None
        doc_embs = {name: cache[doc_key(doc, disc)] for name, doc in docs.items()
                   if doc_key(doc, disc) in cache}
        return doc_embs, query_emb
    except Exception:
        log.debug("tool-doc embedding failed; falling back to keyword ranking", exc_info=True)
        return None, None


class DisclosedRegistry(ToolRegistry):
    """A ToolRegistry that ADVERTISES a subset. Dispatch is unchanged.

    `get`, `validate`, `names` and `list` are inherited untouched and read the SHARED `_tools`
    dict, so they see every tool. Only `openai_schema()` / `text_schema()` are narrowed. Concretely
    this means:

    - `names()` stays full, so ManualMode's `_known_tools` (modes/manual.py) still repairs a
      name-as-action envelope for a hidden tool instead of failing to parse it.
    - `validate()`'s unknown-tool message still lists every real tool.
    - `list()` stays full, so tools_overview() / builtin_tool_names() / the routine registry are
      untouched: disclosure is a per-turn presentation change, not a capability change.
    - `register()` auto-reveals, so a tool create_tool builds mid-turn can never be unseeable.

    Deny is the ONE thing this view cannot widen. `reveal()`/`register()` still add unconditionally
    (they know nothing about policy), but the deny filter lives in the schema builders, downstream
    of both — see `_visible_view()`. So neither find_tool nor a mid-turn create_tool can advertise
    a tool the owner denied.
    """

    def __init__(self, full: ToolRegistry, visible: Iterable[str]):
        self.full = full
        self._tools = full._tools               # shared by REFERENCE — never a copy
        self._visible = set(visible)
        # The deny resolver must survive the wrap, and must survive _visible_view()'s fresh
        # ToolRegistry below. Disclosure ("worth showing this turn?") and deny ("may not be used at
        # all") are independent axes, and deny is the one that must never be lost by a copy.
        self.permissions = getattr(full, "permissions", None)

    # ---- disclosure surface ----
    def visible_names(self) -> list[str]:
        """ADVERTISED names, in registry insertion order — what the schemas actually contain.
        A denied tool is never advertised, even if it is in the disclosure view (reveal() and
        register() both add unconditionally; the deny filter is applied AFTER them, here)."""
        denied = self.denied_names()
        return [n for n in self._tools if n in self._visible and n not in denied]

    def hidden_names(self) -> list[str]:
        """The complement of visible_names() — everything registered but NOT advertised, which
        includes denied tools (unadvertisable, but still dispatchable and still gated)."""
        shown = set(self.visible_names())
        return [n for n in self._tools if n not in shown]

    def reveal(self, names: Iterable[str]) -> list[str]:
        """Add `names` to the view. Returns the ones actually added (registered and not already
        visible), so a caller can report/emit only real changes. The view only ever GROWS."""
        added = []
        for n in names:
            if n in self._tools and n not in self._visible:
                self._visible.add(n)
                added.append(n)
        return added

    def register(self, tool: Tool) -> None:
        super().register(tool)                  # writes through to the shared dict
        self._visible.add(tool.name)

    # ---- presentation (the ONLY narrowed methods) ----
    def _visible_view(self) -> ToolRegistry:
        # `permissions` MUST be carried into this fresh registry. It is built per call, AFTER any
        # reveal()/register() has already grown `_visible`, which is precisely what makes the deny
        # filter un-bypassable: find_tool can reveal a denied tool and create_tool can register one
        # mid-turn, and neither ends up in a catalog. Drop the resolver here and deny silently
        # stops working the moment disclosure is switched on.
        r = ToolRegistry(permissions=self.permissions)
        for name, tool in self._tools.items():
            if name in self._visible:
                r.register(tool)
        return r

    def openai_schema(self) -> list[dict]:
        return self._visible_view().openai_schema()

    def text_schema(self) -> str:
        return self._visible_view().text_schema()


FIND_TOOL_DESCRIPTION = (
    "Find a tool that isn't listed above. Describe what you need to do (e.g. 'convert currency', "
    "'read a PDF') and the matching tools will be added to your available tools. Call this before "
    "saying you can't do something.")


class FindToolTool(Tool):
    """The escape hatch — the tool-level analogue of load_skill.

    Registered BEFORE selection runs (so it is a real, rankable tool) and always in the core set, so
    it is never itself hidden. Its `disclosure`/`registry` attributes are rebound to the live view by
    run_task, the same way created tools get their registry rebound."""

    name = "find_tool"
    description = FIND_TOOL_DESCRIPTION
    disclosure = None            # rebound to the DisclosedRegistry for this run
    registry = None              # ditto (a plain ToolRegistry when disclosure is off)

    class Params(BaseModel):
        query: str = Field("", description="what you need to do, e.g. 'convert currency'. "
                                           "Leave empty (or 'all') for the full tool catalog.")

    def __init__(self, disclosure=None, top: int = 5):
        self.disclosure = disclosure
        self.registry = disclosure
        self.top = top

    async def run(self, args: "FindToolTool.Params") -> str:
        reg = self.disclosure or self.registry
        if reg is None:
            return "find_tool error: no tool registry is available."
        q = (args.query or "").strip()
        # find_tool exists to REVEAL hidden tools, so it is the obvious way to resurrect a denied
        # one ("I have no web access" -> find_tool('search the web')). Denied tools are excluded
        # from everything it offers; a denied MATCH is reported honestly instead, because silently
        # returning nothing would send the model looking for a workaround.
        denied = reg.denied_names()
        if not q or q.lower() in ("all", "*", "everything"):
            lines = [f"- {t.name}: {(t.description or '').split('.')[0].strip()}"
                     for t in reg.list() if t.name not in denied]
            if not lines:
                return "find_tool: no tools are available (every tool is disabled by your policy)."
            return ("Full tool catalog (call any of these by name):\n" + "\n".join(lines))
        # Exclude what's already visible: revealing tools the model can already see would make
        # find_tool a no-op that still claims "these are now available to you". A view exposes
        # visible_names(); a plain ToolRegistry (disclosure off) has nothing to exclude.
        already_visible = set(reg.visible_names()) if hasattr(reg, "visible_names") else set()
        ranked = rank_tools(reg, q, mode="keyword", exclude=already_visible)[:self.top]
        # Ranked a second time WITH the denied tools, purely to answer "did the thing you asked for
        # exist but get turned off?". Kept separate so a denied match never costs a usable tool its
        # slot in the reveal above — ranking is pure, so this is free.
        blocked = [n for n, _s in rank_tools(reg, q, mode="keyword", exclude=already_visible,
                                             include_denied=True)[:self.top] if n in denied]
        note = ("" if not blocked else
                f"\nNote: {', '.join(blocked)} also match, but are disabled by your policy and "
                f"cannot be used. Don't try to call them.")
        if not ranked:
            if blocked:
                return (f"find_tool: the only tools matching {q!r} ({', '.join(blocked)}) are "
                        f"disabled by your policy and cannot be used.")
            return f"find_tool: no tools matched {q!r}."
        names = [n for n, _s in ranked]
        if hasattr(reg, "reveal"):
            reg.reveal(names)
        blocks = []
        for name in names:
            tool = reg.get(name)
            if tool is None:
                continue
            blocks.append(f"- {tool.name}: {tool.description}\n  args:\n{_arg_lines(tool)}")
        return ("These tools are now available to you — call one directly:\n"
                + "\n".join(blocks) + note)


def _arg_lines(tool: Tool) -> str:
    """One tool's argument contract, formatted like text_schema()'s per-tool block."""
    params = getattr(tool, "Params", None)
    schema = {}
    if params is not None and hasattr(params, "model_json_schema"):
        try:
            schema = params.model_json_schema()
        except Exception:
            schema = {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = []
    for pname, pinfo in props.items():
        ptype = pinfo.get("type", "any") if isinstance(pinfo, dict) else "any"
        req = "required" if pname in required else "optional"
        desc = f" — {pinfo['description']}" if isinstance(pinfo, dict) and pinfo.get("description") else ""
        out.append(f"    - {pname} ({ptype}, {req}){desc}")
    return "\n".join(out) if out else "    (no arguments)"
