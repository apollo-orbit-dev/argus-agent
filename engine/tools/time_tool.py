"""get_current_time — trivial, network-free. Good for testing tool selection and
chaining without network flakiness polluting the signal.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from engine.tools.base import Tool

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


class TimeTool(Tool):
    name = "get_current_time"
    description = ("Return the current date and time as an ISO-8601 string. "
                  "Optional 'timezone' is an IANA name like 'America/New_York' (default UTC).")

    class Params(BaseModel):
        timezone: str = Field(default="UTC", description="IANA timezone name, e.g. 'Europe/London'")

    async def run(self, args: "TimeTool.Params") -> str:
        tz = timezone.utc
        tzname = "UTC"
        if args.timezone and args.timezone.upper() != "UTC" and ZoneInfo is not None:
            try:
                tz = ZoneInfo(args.timezone)
                tzname = args.timezone
            except Exception:
                # unknown timezone -> fall back to UTC, don't crash
                tz = timezone.utc
                tzname = "UTC (requested '%s' unknown)" % args.timezone
        now = datetime.now(tz)
        return f"{now.isoformat(timespec='seconds')} ({tzname})"
