"""Shared place geocoder (Open-Meteo, keyless) used by the weather and time tools.

Handles "City, State/Country" queries (e.g. 'Atlanta, GA') by splitting off the hint
and disambiguating among candidates — so small towns resolve consistently across
tools instead of one tool succeeding and another failing on the same string.
"""
from __future__ import annotations

from typing import Optional

import httpx

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


async def geocode_place(location: str, timeout: float = 20.0) -> Optional[dict]:
    """Best-match place dict (name, admin1, country, country_code, latitude, longitude,
    timezone) or None if not found. Raises httpx.HTTPError on transport failure."""
    parts = [p.strip() for p in str(location).split(",")]
    name = parts[0]
    hint = parts[1].lower() if len(parts) > 1 and parts[1] else ""
    hint_full = _US_STATES.get(hint, hint)

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(_GEOCODE_URL, params={"name": name, "count": 10,
                                              "language": "en", "format": "json"})
    r.raise_for_status()  # non-200 -> HTTPError so callers report an error (not "not found")
    results = (r.json() or {}).get("results") or []
    if not results:
        return None
    chosen = results[0]
    if hint_full:
        for res in results:
            blob = " ".join(str(res.get(k, "")).lower()
                            for k in ("admin1", "country", "country_code"))
            if hint_full in blob or (hint and hint in blob):
                chosen = res
                break
    return chosen
