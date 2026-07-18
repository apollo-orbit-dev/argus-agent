---
name: compare_options
description: Compare two or three options against the criteria the user cares about by searching for each, reading a good source, and giving a grounded recommendation. Use when the user asks which of several choices is better.
tools: [web_search, fetch_page]
triggers: [compare, versus, vs, which is better, difference between, pros and cons, which should i]
---
You are comparing options for the user. Base the comparison on what you read, not
on memory. Follow these steps in order:

1. Read the request and identify the 2 or 3 OPTIONS being compared and the
   CRITERIA the user cares about (price, quality, ease, etc.). If the options are
   unclear, ASK the user and stop.
2. For EACH option, call `web_search` with a focused query about that option and
   the criteria.
3. For an option where the snippets are not enough, pick the best result and call
   `fetch_page` on its URL to read more. You do not need to fetch for every
   option — only where you need detail.
4. Gather how each option does on each criterion.
5. Present a SHORT comparison: either a compact table (options as rows, criteria
   as columns) or a few bullets per option.
6. End with a clear RECOMMENDATION and one sentence on why, grounded in what you
   read. Mention the source URL(s) you used.

Rules of thumb:
- One tool call at a time; wait for the result before the next step.
- Keep the comparison tight — cover the criteria that matter, skip the rest.

**If `web_search` or `fetch_page` isn't in your tool list:** these are BUILT-IN tools — a missing
one just means its dependency isn't configured on this server yet (`web_search` needs SearXNG;
`fetch_page` needs Firecrawl). Do NOT `create_tool` a replacement — you can't rebuild them in the
sandbox. Compare from what you already know and tell the user that web lookup isn't set up (they can
enable it by setting `SEARXNG_BASE_URL` / `FIRECRAWL_BASE_URL` in the server's `.env`).
