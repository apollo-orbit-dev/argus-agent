"""Agent profiles — a named, switchable SNAPSHOT of the agent's configuration.

A profile bundles the surfaces that used to be global singletons: persona (SOUL), base system
prompt, the per-tool permission matrix, skill visibility, which standing rules apply, model-role
assignments and a set of feature flags. Selecting one reshapes what Argus is for a task
("Research", "Coding", "Home-ops") without hand-editing Settings.

THREE DECISIONS THIS FILE ENCODES (maintainer, 2026-07-31):

1. Memory is GLOBAL and shared across profiles. It is explicitly NOT a profile field — neither are
   sessions, tables, credentials or connections. A profile selects a model ROLE BINDING (a
   connection label), never an API key.
2. Profiles are SNAPSHOTS, not patches: a profile fully specifies every field it governs, so what
   you read in a profile is what runs — no action-at-a-distance from a global edit.
3. A profile fully OWNS the per-tool permission matrix.

THE STALENESS RULE (the load-bearing part). Because a profile is a snapshot, a tool added to Argus
after the profile was written has NO entry in that profile's matrix. The default for such an unknown
tool is `ask` — never `allow`, and never silently inherited from the global PermissionStore. `ask`
fails safe without failing useless: the tool is still ADVERTISED (only `deny` is hidden from the
catalog — see engine/tools/base.py), so a newly added capability stays discoverable and the owner is
prompted once and can then pin it into the profile. Defaulting to `deny` would hide every future
tool from every profile written before it.

Skills mirror that, one level weaker: an unknown SKILL is VISIBLE. A skill is prompt text, not a
capability — it executes nothing on its own — so there is no permission matrix for skills, only
visibility, and a new skill that were silently invisible everywhere would be undiscoverable. Same
for a standing rule.

`dep-install` is NOT a tool (it is a mid-tool sub-gate inside create_tool) and is never
profile-owned: ProfilePolicy delegates it to the global PermissionStore.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from engine.approvals.types import default_for, states_for

log = logging.getLogger("argus.profiles")

# Bumped when the on-disk shape changes incompatibly. Written from day one so the staleness problem
# can be migrated later without guessing what an old file meant.
SCHEMA = 1

# The absent-entry default for a tool the profile has never heard of. NOT `allow`, NOT the global
# value. See the module docstring.
UNKNOWN_TOOL_STATE = "ask"

# Keys that are NOT tools and must never be governed by a profile's tool matrix.
NON_TOOL_KEYS = {"dep-install"}

# The Config fields a profile governs. Everything else in Config (model identity, ports, secrets,
# sandbox, storage) stays global — a profile reshapes BEHAVIOUR, not the deployment.
PROFILE_FLAG_FIELDS = (
    "enable_observer",
    "enable_action_verify",
    "enable_clarify",
    "enable_rules",
    "enable_rules_autodetect",
    "enable_memory_autoextract",
    "skill_selection_mode",
    "adaptive_thinking",
    "tool_disclosure_mode",
)

# The capability ROLES a profile binding actually overrides today. A profile stores a binding for
# any capability (the migration snapshot copies every global role), but only `chat` is resolved
# through the profile at turn time — see Engine._profile_chat_client. Two of the rest are
# deliberately global rather than merely unimplemented:
#   * embedding — memory and knowledge vectors are GLOBAL and shared across profiles (decision 1
#     above), so a per-profile embedding model would write mismatched vectors into one store;
#   * utility   — background work (compaction, autoextract, titling) is not scoped to a session, so
#     it has no profile to resolve through.
# The dashboard reads this to keep the Models page honest about which role changes a live profile
# will override, instead of offering per-profile bindings that silently do nothing.
PROFILE_BOUND_ROLES = ("chat",)

# allow > ask > deny. Used to decide whether an activation WIDENS a tool's permission.
_RANK = {"deny": 0, "ask": 1, "allow": 2}


def _clean_states(raw) -> dict[str, str]:
    """Keep only well-formed tool -> allow|ask|deny entries. A hand-edited profiles.json cannot
    smuggle an unknown state past this (and an unknown tool name is fine — it may be a created tool
    that is not registered right now)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and k not in NON_TOOL_KEYS and v in states_for(k):
            out[k] = v
    return out


def _clean_bools(raw) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items() if isinstance(k, str)}


def _clean_str_map(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v}


@dataclass
class Profile:
    """One profile record. Every governed field is stored EXPLICITLY (snapshot semantics)."""

    name: str
    description: str = ""
    soul: str = ""
    system_prompt: str = ""
    flags: dict = field(default_factory=dict)          # Config field -> value (PROFILE_FLAG_FIELDS)
    tools: dict = field(default_factory=dict)          # tool name -> allow|ask|deny
    skills: dict = field(default_factory=dict)         # skill name -> visible?  (absent = visible)
    rules: dict = field(default_factory=dict)          # rule id -> applies?     (absent = applies)
    model_roles: dict = field(default_factory=dict)    # capability -> connection LABEL (never a key)
    created_at: float = 0.0
    updated_at: float = 0.0

    # ---- the three absent-entry defaults ----
    def permission(self, tool: str) -> str:
        """Effective Allow/Ask/Deny for a tool under this profile. THE STALENESS RULE: a tool with
        no entry resolves to `ask` — never `allow`, never the global value."""
        if tool in NON_TOOL_KEYS:
            return default_for(tool)
        state = self.tools.get(tool)
        return state if state in ("allow", "ask", "deny") else UNKNOWN_TOOL_STATE

    def skill_visible(self, name: str) -> bool:
        """Is this skill offered under this profile? A skill added after the profile was written
        (no entry) is VISIBLE."""
        return bool(self.skills.get(name, True))

    def rule_applies(self, rule_id: str) -> bool:
        """Does this standing rule apply under this profile? A rule created after the profile was
        written (no entry) DOES apply — same discoverability argument as skills."""
        return bool(self.rules.get(rule_id, True))

    def stale_tools(self, registry_names) -> list[str]:
        """Tools present in the registry but absent from this profile's matrix — i.e. the ones
        currently running on the `ask` default. The dashboard MUST surface this: a stale profile
        that announces itself is fine, one that is invisible is the failure mode."""
        return sorted(n for n in registry_names if n not in self.tools and n not in NON_TOOL_KEYS)

    # ---- config ----
    def to_config(self, base):
        """Deserialize into the SAME Config object the engine already consumes.

        Deliberately NOT a parallel config path: the engine's turn code keeps reading a plain
        Config, so it never has to learn what a profile is. An unparseable flag set degrades to the
        base config (loud in the log) rather than taking down the turn."""
        patch = {k: v for k, v in self.flags.items() if k in PROFILE_FLAG_FIELDS}
        if not patch:
            return base
        try:
            return base.patch(patch)
        except Exception:
            log.warning("profile %r has invalid flags %r; falling back to the global config",
                        self.name, patch, exc_info=True)
            return base

    # ---- (de)serialization ----
    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description, "soul": self.soul,
                "system_prompt": self.system_prompt, "flags": dict(self.flags),
                "tools": dict(self.tools), "skills": dict(self.skills), "rules": dict(self.rules),
                "model_roles": dict(self.model_roles),
                "created_at": self.created_at, "updated_at": self.updated_at}

    @classmethod
    def from_json(cls, data: dict, name: str = "") -> "Profile":
        name = str(data.get("name") or name or "").strip()
        flags = data.get("flags")
        flags = {k: v for k, v in flags.items() if k in PROFILE_FLAG_FIELDS} if isinstance(flags, dict) else {}
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            soul=str(data.get("soul") or ""),
            system_prompt=str(data.get("system_prompt") or ""),
            flags=flags,
            tools=_clean_states(data.get("tools")),
            skills=_clean_bools(data.get("skills")),
            rules=_clean_bools(data.get("rules")),
            model_roles=_clean_str_map(data.get("model_roles")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )

    def copy_as(self, name: str, description: str = "") -> "Profile":
        rec = self.to_json()
        rec["name"] = name
        rec["description"] = description or self.description
        p = Profile.from_json(rec)
        p.created_at = p.updated_at = time.time()
        return p


def widened_tools(prev: Optional[Profile], new: Profile, registry_names=()) -> list[dict]:
    """Tools whose effective permission is WIDER under `new` than under `prev` (deny < ask < allow).

    This is what makes an activation legible: the maintainer chose full profile ownership of
    permissions over a confirmation gate, so activation does not block — it must be VISIBLE instead.
    Narrowing needs no announcement. Every registered tool is considered, not just the ones both
    matrices name, so the `ask` default of a stale profile participates."""
    names = set(registry_names) | set(new.tools) | set(prev.tools if prev else ())
    out = []
    for n in sorted(names):
        if n in NON_TOOL_KEYS:
            continue
        after = new.permission(n)
        before = prev.permission(n) if prev is not None else after
        if _RANK.get(after, 1) > _RANK.get(before, 1):
            out.append({"tool": n, "from": before, "to": after})
    return out


class ProfilePolicy:
    """The `policy` object ApprovalBroker.gate() consults, backed by a PROFILE's matrix.

    Same duck-type as PermissionStore (`get`/`set`), so the broker and the tool registry's deny
    filter are unchanged — they just ask a different object. Two rules:

      * an unknown tool resolves to `ask` (the staleness rule), never to the global value;
      * `dep-install` is not a tool and is delegated to the GLOBAL store, so a profile can never
        blanket-approve package installs by omission.

    `set` (the approval card's "always allow" / "always deny") pins the state into the PROFILE that
    was live for that turn — which is exactly the "prompted once, then pin it" path the staleness
    rule promises. It never touches the global store.
    """

    def __init__(self, store: "ProfileStore", profile_name: str, fallback):
        self.store = store
        self.profile_name = profile_name
        self.fallback = fallback          # global PermissionStore (dep-install + missing profile)

    def _profile(self) -> Optional[Profile]:
        return self.store.get(self.profile_name)

    def get(self, key: str) -> str:
        if key in NON_TOOL_KEYS:
            return self.fallback.get(key)
        prof = self._profile()
        if prof is None:                  # profile deleted mid-turn: fail safe, not open
            return UNKNOWN_TOOL_STATE
        return prof.permission(key)

    def set(self, key: str, state: str) -> None:
        if key in NON_TOOL_KEYS:
            self.fallback.set(key, state)
            return
        if state not in states_for(key):
            raise ValueError(f"invalid policy {key}={state}")
        prof = self._profile()
        if prof is None:
            raise KeyError(f"no profile {self.profile_name!r}")
        prof.tools[key] = state
        self.store.save_profile(prof)


class ProfileStore:
    """profiles.json — one record per profile, plus the global default and per-session bindings.

    Scope/binding: the active profile is PER-SESSION, with a global default used for new sessions.
    That is what makes a mid-session swap (argus-4vi) a rebinding rather than a rewrite, and what
    would let a sub-agent (argus-vjr) run under a different profile later. Channels (Telegram,
    dashboard, API) select a session; they do not each carry their own profile.

    Atomic writes (temp + os.replace), same as PermissionStore/RulesStore.
    """

    def __init__(self, path: str):
        self.path = path
        self.schema = SCHEMA
        self.profiles: dict[str, Profile] = {}
        self.active_profile: str = ""     # the GLOBAL DEFAULT, used by any unbound session
        self.sessions: dict[str, str] = {}
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            data = json.loads(open(self.path, encoding="utf-8").read())
        except Exception:
            log.exception("profiles.json is unreadable; starting from an empty profile set")
            return
        if not isinstance(data, dict):
            return
        self.schema = data.get("schema") if isinstance(data.get("schema"), int) else SCHEMA
        raw = data.get("profiles")
        if isinstance(raw, dict):
            for name, rec in raw.items():
                if not isinstance(rec, dict):
                    continue
                p = Profile.from_json(rec, name=str(name))
                if p.name:
                    self.profiles[p.name] = p
        active = data.get("active_profile")
        self.active_profile = active if isinstance(active, str) and active in self.profiles else ""
        if not self.active_profile and self.profiles:
            self.active_profile = next(iter(self.profiles))
        sess = data.get("sessions")
        if isinstance(sess, dict):
            self.sessions = {k: v for k, v in sess.items()
                             if isinstance(k, str) and v in self.profiles}

    def _save(self) -> None:
        data = {"schema": SCHEMA, "active_profile": self.active_profile,
                "profiles": {n: p.to_json() for n, p in self.profiles.items()},
                "sessions": dict(self.sessions)}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)

    def save_profile(self, profile: Profile) -> Profile:
        profile.updated_at = time.time()
        if not profile.created_at:
            profile.created_at = profile.updated_at
        self.profiles[profile.name] = profile
        if not self.active_profile:
            self.active_profile = profile.name
        self._save()
        return profile

    # ---- reads ----
    def names(self) -> list[str]:
        return list(self.profiles.keys())

    def list(self) -> list[Profile]:
        return list(self.profiles.values())

    def get(self, name: str) -> Optional[Profile]:
        return self.profiles.get(name)

    def default(self) -> Optional[Profile]:
        return self.profiles.get(self.active_profile)

    def name_for_session(self, session_id: str) -> str:
        """The profile bound to this session, else the global default. Exactly one profile is
        always active once migration has run — there is no 'no profile' state."""
        name = self.sessions.get(session_id or "")
        if name and name in self.profiles:
            return name
        return self.active_profile

    def for_session(self, session_id: str) -> Optional[Profile]:
        return self.profiles.get(self.name_for_session(session_id))

    def sessions_using(self, name: str) -> list[str]:
        return [s for s, n in self.sessions.items() if n == name]

    # ---- writes ----
    def ensure_default(self, factory) -> Optional[Profile]:
        """Migration: with no profiles.json (or an empty one), the CURRENT global settings become a
        profile — `factory()` builds it — and it becomes the active and default profile. Nobody's
        setup changes; the engine's resolved config is identical to pre-migration because the
        snapshot is taken FROM that config. Idempotent."""
        if self.profiles:
            return self.default()
        prof = factory()
        prof.created_at = prof.updated_at = time.time()
        self.profiles[prof.name] = prof
        self.active_profile = prof.name
        self._save()
        return prof

    def create(self, profile: Profile) -> Profile:
        if not profile.name.strip():
            raise ValueError("a profile needs a name")
        if profile.name in self.profiles:
            raise ValueError(f"a profile named {profile.name!r} already exists")
        return self.save_profile(profile)

    def duplicate(self, source: str, new_name: str, description: str = "") -> Profile:
        """Duplicate is the PRIMARY authoring path: with snapshot semantics, 'copy the one that
        works and change two things' is the natural workflow."""
        src = self.get(source)
        if src is None:
            raise KeyError(f"no profile {source!r}")
        if not new_name.strip():
            raise ValueError("a profile needs a name")
        if new_name in self.profiles:
            raise ValueError(f"a profile named {new_name!r} already exists")
        return self.save_profile(src.copy_as(new_name, description))

    def rename(self, old: str, new: str) -> Profile:
        prof = self.get(old)
        if prof is None:
            raise KeyError(f"no profile {old!r}")
        new = (new or "").strip()
        if not new:
            raise ValueError("a profile needs a name")
        if new != old and new in self.profiles:
            raise ValueError(f"a profile named {new!r} already exists")
        del self.profiles[old]
        prof.name = new
        self.profiles[new] = prof
        if self.active_profile == old:
            self.active_profile = new
        self.sessions = {s: (new if n == old else n) for s, n in self.sessions.items()}
        self._save()
        return prof

    def delete(self, name: str) -> None:
        """Deleting the ACTIVE profile is refused; deleting the LAST profile is refused. Both keep
        the invariant that exactly one profile is always active."""
        if name not in self.profiles:
            raise KeyError(f"no profile {name!r}")
        if len(self.profiles) <= 1:
            raise ValueError("this is the last profile — Argus always runs under one, so it can't "
                             "be deleted. Create another profile first.")
        if name == self.active_profile:
            raise ValueError(f"{name!r} is the active profile — activate another one first, then "
                             f"delete it.")
        using = self.sessions_using(name)
        if using:
            raise ValueError(f"{name!r} is in use by {len(using)} session(s) — switch them to "
                             f"another profile first.")
        del self.profiles[name]
        self._save()

    def set_default(self, name: str) -> Profile:
        """Make `name` the global default (used by every session with no binding of its own)."""
        prof = self.get(name)
        if prof is None:
            raise KeyError(f"no profile {name!r}")
        self.active_profile = name
        self._save()
        return prof

    def bind(self, session_id: str, name: str) -> Profile:
        """Bind ONE session to a profile. The next turn in that session resolves through it;
        history is untouched."""
        prof = self.get(name)
        if prof is None:
            raise KeyError(f"no profile {name!r}")
        if not session_id:
            raise ValueError("session_id is required")
        self.sessions[session_id] = name
        self._save()
        return prof

    def unbind(self, session_id: str) -> None:
        if self.sessions.pop(session_id, None) is not None:
            self._save()
