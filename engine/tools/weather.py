"""weather — current conditions for a city via Open-Meteo (keyless).

Two-step: geocode the city name, then fetch the current forecast for its lat/lon.
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool
from engine.tools.geocode import geocode_place

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherTool(Tool):
    name = "weather"
    description = ("Get the current weather (temperature, humidity, wind) for a city "
                   "by name. Use when the user asks about weather or conditions somewhere.")

    class Params(BaseModel):
        location: str = Field(..., description="City name, e.g. 'Nashville' or 'Paris, France'")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def run(self, args: "WeatherTool.Params") -> str:
        try:
            place = await geocode_place(args.location, self.timeout)
            if not place:
                return f"weather: location {args.location!r} not found."
            name = place.get("name") or args.location
            admin1 = place.get("admin1") or ""
            country = place.get("country") or ""
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(_FORECAST_URL, params={
                    "latitude": place.get("latitude"),
                    "longitude": place.get("longitude"),
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                })
        except httpx.HTTPError as e:
            return f"weather error: could not reach weather service ({e})"
        if r.status_code != 200:
            return f"weather error: forecast returned HTTP {r.status_code}"
        try:
            cur = r.json().get("current") or {}
        except Exception as e:
            return f"weather error: could not parse forecast response ({e})"
        temp = cur.get("temperature_2m")
        humidity = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        where = ", ".join(x for x in (name, admin1, country) if x)
        return (f"Weather in {where}: {temp}°C, humidity {humidity}%, "
                f"wind {wind} km/h.")
