"""Current date/time anywhere. Fast offline path for common cities / IANA zones;
falls back to geocoding (open-meteo, which returns each place's IANA timezone) so
arbitrary places like 'Atlanta, GA' resolve to the correct zone.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool
from engine.tools.geocode import geocode_place

_CITY_TO_ZONE = {
    "tokyo": "Asia/Tokyo", "london": "Europe/London", "new york": "America/New_York",
    "los angeles": "America/Los_Angeles", "chicago": "America/Chicago",
    "san francisco": "America/Los_Angeles", "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "moscow": "Europe/Moscow", "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong", "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "sydney": "Australia/Sydney", "toronto": "America/Toronto", "sao paulo": "America/Sao_Paulo",
    "mexico city": "America/Mexico_City", "cairo": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg", "utc": "UTC",
}



class TimeInZoneTool(Tool):
    name = "time_in_zone"
    description = (
        "Get the current date and time anywhere. Accepts a place — a city, optionally "
        "with state/country ('Atlanta, GA', 'Paris, France', 'Tokyo') — or an IANA timezone "
        "id ('Europe/Paris'). Arbitrary places are resolved by geocoding, so you don't "
        "need to know the timezone. Use when the user asks what time it is somewhere."
    )

    class Params(BaseModel):
        location: str = Field(..., description="A place ('Atlanta, GA', 'Tokyo') or IANA zone")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def _local_zone(self, raw: str):
        if raw.lower() in _CITY_TO_ZONE:
            return _CITY_TO_ZONE[raw.lower()]
        try:
            ZoneInfo(raw)
            return raw
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def _fmt(self, zone_name: str, where: str) -> str:
        now = datetime.now(ZoneInfo(zone_name))
        return (f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%Z%z')}) "
                f"in {where} [{zone_name}]")

    async def _geocode(self, raw: str):
        """Return ((timezone, display_name), None) | (None, None) | (None, error_str)."""
        try:
            chosen = await geocode_place(raw, self.timeout)
        except httpx.HTTPError as e:
            return None, f"time_in_zone error: could not reach the geocoder ({e})"
        if not chosen:
            return None, None
        tz = chosen.get("timezone")
        if not tz:
            return None, None
        disp = ", ".join(x for x in (chosen.get("name"), chosen.get("admin1"),
                                     chosen.get("country")) if x)
        return (tz, disp or raw), None

    async def run(self, args: "TimeInZoneTool.Params") -> str:
        try:
            raw = args.location.strip()
            zone = self._local_zone(raw)
            if zone:
                return self._fmt(zone, raw if raw.lower() in _CITY_TO_ZONE else zone)
            geo, err = await self._geocode(raw)
            if err:
                return err
            if geo:
                tz, disp = geo
                return self._fmt(tz, disp)
            return (f"time_in_zone error: couldn't find a place or timezone matching "
                    f"'{args.location}'. Try adding a state/country ('Atlanta, GA') or an "
                    f"IANA timezone id like 'Europe/Paris'.")
        except Exception as e:  # defensive: never crash the loop
            return f"time_in_zone error: {e}"
