"""Routines — named, ordered step sequences (loaded data), run on command or (later) on schedule.

A routine pins the plan for a recurring task so the small model stops re-deciding the flaky parts.
v1: hybrid (deterministic `tool` steps + model `model` steps) and linear (no branches/loops).
See docs/routines-spec.md. Routines are one-JSON-per-file under `routines/` (deploy-excluded runtime
state), same loaded-data ethos as skills and created tools.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger("argus.routines")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_STEPS = 20
MAX_MODEL_STEPS = 6
_CHANNELS = ("telegram", "email", "push", "none")


@dataclass
class Routine:
    name: str
    description: str = ""
    enabled: bool = True
    steps: list = field(default_factory=list)     # list[dict]; each: tool step or model step
    output: str = ""                               # step id to deliver; "" -> last step
    deliver: dict = field(default_factory=dict)    # {channel, subject}
    trigger: dict = field(default_factory=dict)    # {on_demand, phrases, schedule}
    created_at: str = ""
    updated_at: str = ""
    last_run: dict = field(default_factory=dict)   # {at, ok, error}

    @property
    def output_id(self) -> str:
        if self.output:
            return self.output
        return self.steps[-1]["id"] if self.steps else ""


class RoutineValidationError(ValueError):
    pass


def validate_routine(r: Routine, known_tools: Optional[set] = None,
                     known_skills: Optional[set] = None) -> None:
    """Validate a routine's shape. When known_tools/known_skills are given (dashboard/agent save),
    also check that referenced tools/skills exist. Mutates steps to fill in default `id`s."""
    if not _NAME_RE.match(r.name or ""):
        raise RoutineValidationError("name must be snake_case: [a-z][a-z0-9_]*")
    if not r.steps:
        raise RoutineValidationError("a routine needs at least one step")
    if len(r.steps) > MAX_STEPS:
        raise RoutineValidationError(f"too many steps (max {MAX_STEPS})")
    ids: list[str] = []
    model_steps = 0
    for i, s in enumerate(r.steps):
        if not isinstance(s, dict):
            raise RoutineValidationError(f"step {i} must be an object")
        typ = s.get("type")
        sid = s.get("id") or (s.get("tool") if typ == "tool" else f"step{i + 1}")
        s["id"] = sid
        if not _NAME_RE.match(sid or ""):
            raise RoutineValidationError(f"step id '{sid}' must be snake_case")
        if sid in ids:
            raise RoutineValidationError(f"duplicate step id '{sid}'")
        ids.append(sid)
        if typ == "tool":
            if not s.get("tool"):
                raise RoutineValidationError(f"tool step '{sid}' needs a 'tool'")
            if known_tools is not None and s["tool"] not in known_tools:
                raise RoutineValidationError(f"unknown tool '{s['tool']}' in step '{sid}'")
            if not isinstance(s.get("args", {}) or {}, dict):
                raise RoutineValidationError(f"step '{sid}' args must be an object")
        elif typ == "model":
            model_steps += 1
            if not (s.get("prompt") or "").strip():
                raise RoutineValidationError(f"model step '{sid}' needs a 'prompt'")
            sk = s.get("skill")
            if sk and known_skills is not None and sk not in known_skills:
                raise RoutineValidationError(f"unknown skill '{sk}' in step '{sid}'")
        else:
            raise RoutineValidationError(f"step '{sid}' type must be 'tool' or 'model'")
    if model_steps > MAX_MODEL_STEPS:
        raise RoutineValidationError(f"too many model steps (max {MAX_MODEL_STEPS})")
    if r.output and r.output not in ids:
        raise RoutineValidationError(f"output '{r.output}' is not a step id")
    ch = (r.deliver or {}).get("channel")
    if ch and ch not in _CHANNELS:
        raise RoutineValidationError(f"deliver.channel '{ch}' must be one of {_CHANNELS}")


def _from_dict(d: dict) -> Routine:
    fields = Routine.__dataclass_fields__
    return Routine(**{k: d[k] for k in fields if k in d})


class RoutineStore:
    def __init__(self, directory: str):
        self.dir = directory
        self._routines: dict[str, Routine] = {}

    def load_dir(self) -> None:
        if not os.path.isdir(self.dir):
            return
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, fn), encoding="utf-8") as fh:
                    r = _from_dict(json.load(fh))
                validate_routine(r)            # shape only; tool/skill existence checked on save
                self._routines[r.name] = r
            except Exception as e:              # skip a malformed routine, don't crash startup
                log.warning("skipping malformed routine %s: %s", fn, e)

    def list(self) -> list[Routine]:
        return list(self._routines.values())

    def get(self, name: str) -> Optional[Routine]:
        return self._routines.get(name)

    def save(self, r: Routine, known_tools: Optional[set] = None,
             known_skills: Optional[set] = None) -> None:
        validate_routine(r, known_tools, known_skills)
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, f"{r.name}.json"), "w", encoding="utf-8") as fh:
            json.dump(asdict(r), fh, indent=2)
        self._routines[r.name] = r

    def delete(self, name: str) -> bool:
        if self._routines.pop(name, None) is None:
            return False
        p = os.path.join(self.dir, f"{name}.json")
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            log.warning("could not remove routine file %s", p)
        return True
