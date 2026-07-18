"""Task scheduler — lets the agent schedule work to run later (once or recurring)
and deliver the result back to whoever asked.

Small-model-friendly by design: it takes NATURAL-LANGUAGE schedules
("every day at 8am", "in 30 minutes", "every monday at 9am") rather than raw cron
syntax, which small models get wrong. The parser echoes back the interpreted
schedule so the user/model can confirm, and returns a clear error listing the
accepted forms on failure (the same validate-and-feed-back pattern used elsewhere).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

log = logging.getLogger("argus.scheduler")

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2,
    "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_WD_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_time(s: str):
    s = s.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mn > 59:
        return None
    return h, mn


def parse_schedule(text: str, now: Optional[datetime] = None):
    """(spec, error). spec is a JSON-able dict with a 'type'. Local timezone."""
    now = now or now_local()
    t = " ".join(text.strip().lower().split())

    m = re.match(r"^in\s+(\d+)\s*(second|sec|minute|min|hour|hr|day)s?$", t)
    if m:
        n = int(m.group(1))
        secs = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600,
                "hr": 3600, "day": 86400}[m.group(2)]
        return {"type": "once", "at": (now + timedelta(seconds=n * secs)).isoformat()}, None

    m = re.match(r"^every\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?$", t)
    if m:
        n = int(m.group(1))
        secs = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}[m.group(2)] * n
        if secs < 30:
            return None, "minimum repeat interval is 30 seconds"
        return {"type": "interval", "seconds": secs}, None

    if t in ("every hour", "hourly"):
        return {"type": "interval", "seconds": 3600}, None
    if t in ("every minute",):
        return {"type": "interval", "seconds": 60}, None

    m = re.match(r"^(?:every ?day|daily)\s+at\s+(.+)$", t)   # "every day" and "everyday"
    if m:
        tm = parse_time(m.group(1))
        return ({"type": "daily", "hour": tm[0], "minute": tm[1]}, None) if tm \
            else (None, f"couldn't read the time '{m.group(1)}'")

    m = re.match(r"^every\s+(\w+)\s+at\s+(.+)$", t)
    if m and m.group(1) in _WEEKDAYS:
        tm = parse_time(m.group(2))
        return ({"type": "weekly", "weekday": _WEEKDAYS[m.group(1)], "hour": tm[0],
                 "minute": tm[1]}, None) if tm else (None, f"couldn't read the time '{m.group(2)}'")

    m = re.match(r"^tomorrow\s+at\s+(.+)$", t)
    if m:
        tm = parse_time(m.group(1))
        if not tm:
            return None, f"couldn't read the time '{m.group(1)}'"
        dt = (now + timedelta(days=1)).replace(hour=tm[0], minute=tm[1], second=0, microsecond=0)
        return {"type": "once", "at": dt.isoformat()}, None

    m = re.match(r"^(?:today\s+)?at\s+(.+)$", t)
    if m:
        tm = parse_time(m.group(1))
        if not tm:
            return None, f"couldn't read the time '{m.group(1)}'"
        dt = now.replace(hour=tm[0], minute=tm[1], second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return {"type": "once", "at": dt.isoformat()}, None

    return None, ("couldn't understand that schedule. Try one of: 'in 30 minutes', "
                  "'every day at 8am', 'every hour', 'every 15 minutes', "
                  "'every monday at 9am', 'tomorrow at 7pm', or 'at 3pm'.")


def compute_next_run(spec: dict, now: Optional[datetime] = None) -> datetime:
    now = now or now_local()
    typ = spec["type"]
    if typ == "once":
        return datetime.fromisoformat(spec["at"])
    if typ == "interval":
        return now + timedelta(seconds=spec["seconds"])
    if typ == "daily":
        dt = now.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    if typ == "weekly":
        dt = now.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
        dt += timedelta(days=(spec["weekday"] - now.weekday()) % 7)
        return dt if dt > now else dt + timedelta(days=7)
    return now + timedelta(days=3650)


def describe(spec: dict) -> str:
    typ = spec["type"]
    if typ == "once":
        return f"once at {datetime.fromisoformat(spec['at']).strftime('%Y-%m-%d %H:%M %Z')}"
    if typ == "interval":
        s = spec["seconds"]
        unit = f"{s//3600}h" if s % 3600 == 0 else (f"{s//60}m" if s % 60 == 0 else f"{s}s")
        return f"every {unit}"
    if typ == "daily":
        return f"every day at {spec['hour']:02d}:{spec['minute']:02d}"
    if typ == "weekly":
        return f"every {_WD_NAMES[spec['weekday']]} at {spec['hour']:02d}:{spec['minute']:02d}"
    return "unknown"


@dataclass
class Job:
    id: str
    instruction: str            # a prompt (kind="prompt") OR a routine name (kind="routine")
    schedule: dict
    session_id: str
    next_run: str
    active: bool = True
    created_at: str = ""
    last_run: str = ""
    last_result: str = ""
    runs: int = 0
    kind: str = "prompt"        # "prompt" -> run_task(instruction); "routine" -> run_routine(instruction)


class Scheduler:
    def __init__(self, path: str,
                 run_task: Callable[[str, str], Awaitable[str]],
                 deliver: Optional[Callable[[str, str], Awaitable[None]]] = None,
                 tick_seconds: int = 20,
                 run_routine: Optional[Callable[[str, str], Awaitable[str]]] = None):
        self.path = path
        self.run_task = run_task
        self.deliver = deliver
        self.run_routine = run_routine   # (session_id, routine_name) -> output; set by the engine
        self.tick = tick_seconds
        self.jobs: dict[str, Job] = {}
        self._task: Optional[asyncio.Task] = None
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                for d in json.load(open(self.path, encoding="utf-8")):
                    self.jobs[d["id"]] = Job(**d)
        except Exception:
            log.exception("could not load scheduled jobs from %s", self.path)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump([asdict(j) for j in self.jobs.values()], fh, indent=2)
        except Exception:
            log.exception("could not save scheduled jobs")

    # ---- CRUD ----
    def add(self, instruction: str, spec: dict, session_id: str, kind: str = "prompt") -> Job:
        job = Job(id="job_" + uuid.uuid4().hex[:8], instruction=instruction, schedule=spec,
                  session_id=session_id, next_run=compute_next_run(spec).isoformat(),
                  created_at=now_local().isoformat(), kind=kind)
        self.jobs[job.id] = job
        self._save()
        return job

    def list(self, session_id: Optional[str] = None) -> list[Job]:
        return [j for j in self.jobs.values()
                if j.active and (session_id is None or j.session_id == session_id)]

    def cancel(self, job_id: str, session_id: Optional[str] = None) -> bool:
        j = self.jobs.get(job_id)
        if not j or (session_id is not None and j.session_id != session_id):
            return False
        j.active = False
        self._save()
        return True

    def update(self, job_id: str, session_id: Optional[str] = None,
               instruction: Optional[str] = None, spec: Optional[dict] = None) -> Optional[Job]:
        j = self.jobs.get(job_id)
        if not j or (session_id is not None and j.session_id != session_id):
            return None
        if instruction is not None:
            j.instruction = instruction
        if spec is not None:
            j.schedule = spec
            j.next_run = compute_next_run(spec).isoformat()
        j.active = True
        self._save()
        return j

    # ---- run loop ----
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("scheduler started (%d active jobs)", len(self.list()))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick)
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler loop error")

    async def fire_due(self, now: Optional[datetime] = None) -> None:
        now = now or now_local()
        for job in list(self.jobs.values()):
            if job.active and datetime.fromisoformat(job.next_run) <= now:
                await self._run_job(job)

    async def _run_job(self, job: Job) -> None:
        log.info("firing scheduled job %s (%s): %s", job.id, job.kind, job.instruction)
        is_routine = job.kind == "routine" and self.run_routine is not None
        try:
            if is_routine:
                result = await self.run_routine(job.session_id, job.instruction)
            else:
                result = await self.run_task(job.session_id, job.instruction)
        except Exception as e:
            result = f"(scheduled {job.kind} failed: {e})"
        job.last_run = now_local().isoformat()
        job.last_result = str(result)[:2000]
        job.runs += 1
        # Prompt jobs deliver here (generic). Routine jobs deliver themselves via their own channel
        # inside run_routine, so skip the generic path to avoid double-sending.
        if self.deliver and not is_routine:
            try:
                await self.deliver(job.session_id,
                                   f"⏰ Scheduled task ({describe(job.schedule)}):\n\n{result}")
            except Exception:
                log.exception("scheduled delivery failed for %s", job.id)
        if job.schedule["type"] == "once":
            job.active = False
        else:
            job.next_run = compute_next_run(job.schedule).isoformat()
        self._save()
