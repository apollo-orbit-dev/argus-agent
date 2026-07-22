import asyncio
from datetime import datetime, timedelta

import pytest

from engine.scheduler import (
    Scheduler, compute_next_run, describe, now_local, parse_schedule, parse_time,
)
from engine.tools.schedule import ScheduleTaskTool, ListScheduledTasksTool, CancelScheduledTaskTool


# ---- parsing ----

def test_parse_time():
    assert parse_time("8am") == (8, 0)
    assert parse_time("3pm") == (15, 0)
    assert parse_time("12am") == (0, 0)
    assert parse_time("12pm") == (12, 0)
    assert parse_time("14:30") == (14, 30)
    assert parse_time("9:05am") == (9, 5)
    assert parse_time("bogus") is None


def test_parse_schedule_forms():
    now = datetime.fromisoformat("2026-07-11T10:00:00+00:00")
    assert parse_schedule("in 30 minutes", now)[0]["type"] == "once"
    assert parse_schedule("every hour", now)[0] == {"type": "interval", "seconds": 3600}
    assert parse_schedule("every 15 minutes", now)[0] == {"type": "interval", "seconds": 900}
    daily = parse_schedule("every day at 8am", now)[0]
    assert daily == {"type": "daily", "hour": 8, "minute": 0}
    # "everyday" (no space) must parse the same as "every day"
    assert parse_schedule("everyday at 8am", now)[0] == {"type": "daily", "hour": 8, "minute": 0}
    weekly = parse_schedule("every monday at 9am", now)[0]
    assert weekly == {"type": "weekly", "weekday": 0, "hour": 9, "minute": 0}
    assert parse_schedule("tomorrow at 7pm", now)[0]["type"] == "once"


def test_parse_schedule_errors():
    assert parse_schedule("whenever i feel like it")[0] is None
    assert parse_schedule("every 5 seconds")[1]  # below 30s minimum


def test_compute_next_run_daily_rolls_forward():
    now = datetime.fromisoformat("2026-07-11T10:00:00+00:00")
    nxt = compute_next_run({"type": "daily", "hour": 8, "minute": 0}, now)
    assert nxt.hour == 8 and nxt.day == 12  # 8am already passed today -> tomorrow


def test_describe():
    assert describe({"type": "interval", "seconds": 3600}) == "every 1h"
    assert "Monday" in describe({"type": "weekly", "weekday": 0, "hour": 9, "minute": 0})


# ---- scheduler CRUD + firing ----

def test_add_list_cancel(tmp_path):
    sched = Scheduler(str(tmp_path / "jobs.json"), run_task=None)
    job = sched.add("do a thing", {"type": "interval", "seconds": 3600}, "sess1")
    assert sched.list("sess1") and job.id in sched.jobs
    assert sched.list("other") == []  # scoped by session
    assert sched.cancel(job.id, "sess1") is True
    assert sched.list("sess1") == []
    # persisted + reloads
    sched2 = Scheduler(str(tmp_path / "jobs.json"), run_task=None)
    assert job.id in sched2.jobs


async def test_fire_due_runs_and_delivers(tmp_path):
    ran, delivered = [], []

    async def fake_run(session_id, instruction, **kwargs):
        ran.append((session_id, instruction, kwargs.get("origin")))
        return f"result of {instruction}"

    async def fake_deliver(session_id, text):
        delivered.append((session_id, text))

    sched = Scheduler(str(tmp_path / "j.json"), run_task=fake_run, deliver=fake_deliver)
    # a one-shot already due
    job = sched.add("weather", {"type": "once", "at": (now_local() - timedelta(minutes=1)).isoformat()}, "42")
    await sched.fire_due()
    assert ran == [("42", "weather", "scheduled")]   # scheduler passes origin="scheduled"
    assert delivered and "result of weather" in delivered[0][1]
    assert sched.jobs[job.id].active is False  # one-shot deactivates
    assert sched.jobs[job.id].runs == 1


async def test_recurring_reschedules(tmp_path):
    async def fake_run(s, i, **kwargs): return "ok"
    sched = Scheduler(str(tmp_path / "j.json"), run_task=fake_run)
    job = sched.add("hourly", {"type": "interval", "seconds": 3600}, "s")
    job.next_run = (now_local() - timedelta(seconds=1)).isoformat()
    await sched.fire_due()
    assert sched.jobs[job.id].active is True  # still active
    assert datetime.fromisoformat(sched.jobs[job.id].next_run) > now_local()


# ---- tools ----

async def test_schedule_tool_flow(tmp_path):
    sched = Scheduler(str(tmp_path / "j.json"), run_task=None)
    tool = ScheduleTaskTool(sched, "sess")
    out = await tool.run(tool.Params(instruction="check weather", when="every day at 8am"))
    assert "scheduled" in out.lower() and "every day at 08:00" in out
    lst = ListScheduledTasksTool(sched, "sess")
    assert "check weather" in await lst.run(lst.Params())
    # bad schedule -> clear error
    out2 = await tool.run(tool.Params(instruction="x", when="at the crack of dawn"))
    assert "error" in out2.lower()


async def test_update_reschedules_and_scopes(tmp_path):
    sched = Scheduler(str(tmp_path / "j.json"), run_task=None)
    job = sched.add("old", {"type": "interval", "seconds": 3600}, "sess")
    updated = sched.update(job.id, "sess", instruction="new", spec={"type": "daily", "hour": 7, "minute": 0})
    assert updated and updated.instruction == "new" and updated.schedule["type"] == "daily"
    assert sched.update(job.id, "other") is None  # wrong session
    # tool
    from engine.tools.schedule import UpdateScheduledTaskTool
    tool = UpdateScheduledTaskTool(sched, "sess")
    out = await tool.run(tool.Params(task_id=job.id, when="every hour"))
    assert "updated" in out.lower()
    out2 = await tool.run(tool.Params(task_id="nope", instruction="x"))
    assert "no scheduled task" in out2.lower()


async def test_cancel_tool(tmp_path):
    sched = Scheduler(str(tmp_path / "j.json"), run_task=None)
    job = sched.add("x", {"type": "interval", "seconds": 60}, "sess")
    cancel = CancelScheduledTaskTool(sched, "sess")
    assert "cancelled" in (await cancel.run(cancel.Params(task_id=job.id))).lower()
    assert "no scheduled task" in (await cancel.run(cancel.Params(task_id="nope"))).lower()


# ---- routine jobs (scheduler kind="routine") ----

def test_job_kind_defaults_and_backward_compat():
    from engine.scheduler import Job
    # a Job dict persisted BEFORE 'kind' existed still loads (default applies)
    j = Job(id="job_x", instruction="do", schedule={"type": "interval", "seconds": 60},
            session_id="s", next_run=now_local().isoformat())
    assert j.kind == "prompt"


def test_scheduler_routine_job_calls_run_routine_not_run_task(tmp_path):
    calls = {"task": [], "routine": []}

    async def run_task(sid, instr):
        calls["task"].append((sid, instr)); return "task-out"

    async def run_routine(sid, name):
        calls["routine"].append((sid, name)); return "routine-out"

    delivered = []

    async def deliver(sid, text):
        delivered.append(text)

    sch = Scheduler(str(tmp_path / "jobs.json"), run_task, deliver=deliver, run_routine=run_routine)
    job = sch.add("morning_briefing", {"type": "interval", "seconds": 60}, "chat1", kind="routine")
    asyncio.run(sch._run_job(job))
    assert calls["routine"] == [("chat1", "morning_briefing")]   # routine path taken
    assert calls["task"] == []                                   # NOT the prompt path
    assert delivered == []                                       # generic deliver skipped for routines
    assert job.runs == 1 and "routine-out" in job.last_result


def test_scheduler_prompt_job_still_delivers(tmp_path):
    async def run_task(sid, instr, **kwargs):
        return "answer"

    delivered = []

    async def deliver(sid, text):
        delivered.append(text)

    sch = Scheduler(str(tmp_path / "jobs.json"), run_task, deliver=deliver)
    job = sch.add("remind me", {"type": "interval", "seconds": 60}, "chat1")   # kind defaults to prompt
    asyncio.run(sch._run_job(job))
    assert len(delivered) == 1 and "answer" in delivered[0]      # prompt jobs deliver generically


async def test_scheduling_tools_are_owner_wide_across_sessions(tmp_path):
    # Regression: a job created in one interface's session must be visible AND manageable from
    # another (single-user, global identity). list_scheduled_tasks was wrongly session-scoped, so
    # a Telegram-created job was invisible from the dashboard.
    sched = Scheduler(str(tmp_path / "j.json"), run_task=None)
    sched.add("telegram job", {"type": "daily", "hour": 7, "minute": 0}, "telegram_chat")
    lst = ListScheduledTasksTool(sched, "dashboard")          # DIFFERENT session than the job's
    assert "telegram job" in await lst.run(lst.Params())      # still sees it
    job_id = next(iter(sched.jobs))
    cancel = CancelScheduledTaskTool(sched, "dashboard")
    assert "Cancelled" in await cancel.run(cancel.Params(task_id=job_id))   # can manage it too
    assert "no scheduled tasks" in (await lst.run(lst.Params())).lower()


def test_scheduled_delete_cancels_a_job_from_the_dashboard(tmp_path):
    """The dashboard's delete is deliberately NOT session-scoped: the agent's cancel_scheduled_task
    filters by session so one chat can't cancel another's, but the owner's admin-gated view lists
    every session's jobs — including Telegram's, which is the main reason to want the button."""
    import tempfile

    from config import Config
    from engine.engine import Engine

    eng = Engine(Config(), data_dir=tempfile.mkdtemp())
    job = eng.scheduler.add("send the morning briefing", {"type": "daily", "hour": 7, "minute": 0},
                            session_id="telegram-chat")
    assert any(j["id"] == job.id for j in eng.scheduled_jobs())

    assert eng.scheduled_delete(job.id) == {"ok": True, "id": job.id}
    assert not any(j["id"] == job.id for j in eng.scheduled_jobs()), "cancelled job must leave the list"


def test_scheduled_delete_reports_a_miss_instead_of_raising(tmp_path):
    """An unknown/already-cancelled id must come back ok:False so the dashboard can say so, rather
    than 500 or silently look successful."""
    import tempfile

    from config import Config
    from engine.engine import Engine

    eng = Engine(Config(), data_dir=tempfile.mkdtemp())
    assert eng.scheduled_delete("no-such-job") == {"ok": False, "id": "no-such-job"}
