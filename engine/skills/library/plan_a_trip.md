---
name: plan_a_trip
description: Plan a short trip itinerary for a destination and dates by checking the weather, searching for top attractions, reading one good source, and suggesting weather-appropriate activities. Use when the user wants trip or travel planning.
tools: [web_search, fetch_page, weather]
triggers: [plan a trip, plan my trip, things to do in, itinerary, trip to, travel to, day trip, weekend in, visiting]
---
You are planning a short trip for the user. Use the tools — do not invent
attractions or weather. ACT on what you have; do not stall asking for details you
can proceed without. Follow these steps in order:

1. Find the DESTINATION in the user's request. If no dates are given, DO NOT stop
   to ask — just plan for current/typical conditions and mention at the end that
   you can tailor it to specific dates if they share them. Only ask a question if
   even the destination is unclear.
2. Call `weather` for the destination to learn the current conditions.
3. Build ONE short search query like "top things to do in <destination>" and call
   `web_search`.
4. Look at the results. Pick the SINGLE result whose title and snippet best cover
   attractions or an itinerary. Note its URL.
5. Call `fetch_page` on that URL to read the details.
   - If `fetch_page` fails or the page is thin, call `fetch_page` on another
     result, or call `web_search` once more with a refined query. Do NOT repeat a
     query you already tried.
6. Write a SHORT itinerary (a few days or a simple day-by-day list). Match the
   suggestions to the weather from step 2 — favor indoor options if it will be
   rainy or cold, outdoor options if it will be clear.
7. End with the source URL(s) you used.

Rules of thumb:
- One tool call at a time; wait for the result before the next step.
- Keep it concise — a handful of concrete suggestions beats a long list.

**If `web_search` or `fetch_page` isn't in your tool list:** these are BUILT-IN tools — a missing
one just means its dependency isn't configured on this server yet (`web_search` needs SearXNG;
`fetch_page` needs Firecrawl). Do NOT `create_tool` a replacement. Plan from what you already know
and tell the user that live web lookup isn't set up (they can enable it by setting
`SEARXNG_BASE_URL` / `FIRECRAWL_BASE_URL` in the server's `.env`).
