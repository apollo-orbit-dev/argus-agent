---
name: research
description: Answer a factual or current-events question by searching the web, reading one source, and answering from it. Use for questions that need up-to-date or external information.
tools: [web_search, fetch_page]
triggers: [look up, find out, research, causes of, cause of, history of, who won, when did, when was, how much does, how much is, price of, the latest, look into, search for, what happened, according to]
---
You are researching a question for the user. Do NOT answer from memory alone for
factual or current questions — use the tools. Follow these steps in order:

1. Build ONE focused search query from the user's question (keep it short and
   specific), then call `web_search` with it.
2. Look at the returned results. Choose the SINGLE result whose title and snippet
   best match the question. Note its URL.
3. Call `fetch_page` with that URL to read the full page content.
4. Read the fetched content and find the specific answer to the user's question.
   - If `fetch_page` failed or the page did not contain the answer, either call
     `fetch_page` on a different result's URL, or call `web_search` again with a
     refined query. Do NOT repeat a query you already tried.
5. When you have the answer, respond with your FINAL answer. Keep it concise and
   directly address the question. Mention the source URL you used.

Rules of thumb:
- One tool call at a time; wait for its result before the next step.
- Prefer the official or most authoritative source among the results.
- Two or three tool calls are usually enough — don't keep searching in circles.

**If `web_search` or `fetch_page` isn't in your tool list:** these are BUILT-IN tools — a missing
one just means its dependency isn't configured on this server yet (`web_search` needs SearXNG;
`fetch_page` needs Firecrawl). Do NOT `create_tool` a replacement — you can't rebuild them in the
sandbox. Say plainly that you can't research the web because that tool isn't set up (the user can
enable it by setting `SEARXNG_BASE_URL` / `FIRECRAWL_BASE_URL` in the server's `.env`); don't invent
findings.
