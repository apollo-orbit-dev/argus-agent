---
name: summarize_url
description: Summarize the main points of a web page the user gives you by fetching it and producing a short bullet summary. Use when the user pastes a URL and wants the gist.
tools: [fetch_page]
triggers: [summarize, summarise, tldr, tl;dr, sum up this, what does this page say]
---
You are summarizing a single web page for the user. Do NOT summarize from memory
or from the URL text alone — you must fetch it. Follow these steps in order:

1. Find the URL in the user's request. If there is no URL, ASK the user for one
   and stop.
2. Call `fetch_page` with that URL.
3. If `fetch_page` FAILED or returned no usable content, tell the user plainly
   that you could not read the page. Do NOT make up a summary.
4. If it succeeded, read the content and identify the main points.
5. Respond with a concise summary of 3 to 6 bullet points covering the key ideas.

Rules of thumb:
- Only call `fetch_page` once unless it clearly failed.
- Stick to what the page actually says — no added facts or opinions.
- Keep each bullet short and to the point.

**If `fetch_page` isn't in your tool list:** it's a BUILT-IN tool — its absence just means its
dependency (Firecrawl) isn't configured on this server yet. Do NOT `create_tool` a replacement.
Tell the user you can't read the page because the fetch tool isn't set up (they can enable it by
setting `FIRECRAWL_BASE_URL` in the server's `.env`).
