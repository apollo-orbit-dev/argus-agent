"""Tools that let the agent schedule/list/cancel tasks. Built per-run bound to the
current session_id so results are delivered back to whoever asked."""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.scheduler import Scheduler, describe, parse_schedule
from engine.tools.base import Tool


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "Schedule a task to run automatically later — once or on a repeat — and deliver "
        "the result to the user. Give the instruction to run and when. Examples for 'when': "
        "'in 30 minutes', 'tomorrow at 7pm', 'at 3pm', 'every day at 8am', 'every hour', "
        "'every 15 minutes', 'every monday at 9am'. Use this when the user asks to be "
        "reminded, to get something on a schedule, or to run something later."
    )

    class Params(BaseModel):
        instruction: str = Field(..., description="what to do when it runs, e.g. "
                                                  "'give me the weather in Nashville'")
        when: str = Field(..., description="natural-language schedule, e.g. 'every day at 8am'")

    def __init__(self, scheduler: Scheduler, session_id: str):
        self.scheduler = scheduler
        self.session_id = session_id

    async def run(self, args: "ScheduleTaskTool.Params") -> str:
        spec, err = parse_schedule(args.when)
        if err:
            return f"schedule_task error: {err}"
        job = self.scheduler.add(args.instruction, spec, self.session_id)
        return (f"Scheduled: \"{args.instruction}\" — {describe(spec)}. "
                f"Next run: {job.next_run}. (task id {job.id})")


class ListScheduledTasksTool(Tool):
    name = "list_scheduled_tasks"
    description = "List the tasks currently scheduled for this user."

    class Params(BaseModel):
        pass

    def __init__(self, scheduler: Scheduler, session_id: str):
        self.scheduler = scheduler
        self.session_id = session_id

    async def run(self, args: "ListScheduledTasksTool.Params") -> str:
        jobs = self.scheduler.list(self.session_id)
        if not jobs:
            return "You have no scheduled tasks."
        lines = ["Scheduled tasks:"]
        for j in jobs:
            lines.append(f"- [{j.id}] \"{j.instruction}\" — {describe(j.schedule)}; "
                         f"next {j.next_run}")
        return "\n".join(lines)


class UpdateScheduledTaskTool(Tool):
    name = "update_scheduled_task"
    description = ("Modify an existing scheduled task by its id: change its instruction, its "
                   "schedule ('when'), or both. Get ids from list_scheduled_tasks.")

    class Params(BaseModel):
        task_id: str = Field(..., description="the task id to update")
        instruction: str = Field(default="", description="new instruction (leave blank to keep)")
        when: str = Field(default="", description="new schedule, e.g. 'every day at 7am' "
                                                  "(leave blank to keep)")

    def __init__(self, scheduler: Scheduler, session_id: str):
        self.scheduler = scheduler
        self.session_id = session_id

    async def run(self, args: "UpdateScheduledTaskTool.Params") -> str:
        spec = None
        if args.when.strip():
            spec, err = parse_schedule(args.when)
            if err:
                return f"update_scheduled_task error: {err}"
        instruction = args.instruction.strip() or None
        if instruction is None and spec is None:
            return "update_scheduled_task error: provide a new instruction and/or a new 'when'."
        job = self.scheduler.update(args.task_id, self.session_id, instruction=instruction, spec=spec)
        if not job:
            return f"No scheduled task with id '{args.task_id}' found for you."
        return (f"Updated {job.id}: \"{job.instruction}\" — {describe(job.schedule)}. "
                f"Next run: {job.next_run}.")


class CancelScheduledTaskTool(Tool):
    name = "cancel_scheduled_task"
    description = "Cancel a scheduled task by its task id (from list_scheduled_tasks)."

    class Params(BaseModel):
        task_id: str = Field(..., description="the task id to cancel, e.g. 'job_ab12cd34'")

    def __init__(self, scheduler: Scheduler, session_id: str):
        self.scheduler = scheduler
        self.session_id = session_id

    async def run(self, args: "CancelScheduledTaskTool.Params") -> str:
        if self.scheduler.cancel(args.task_id, self.session_id):
            return f"Cancelled scheduled task {args.task_id}."
        return f"No scheduled task with id '{args.task_id}' found for you."
