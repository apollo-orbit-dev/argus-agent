---
name: explain_concept
description: Explain a concept in plain language by getting a grounded definition from Wikipedia (or a web search if needed), then giving a simple explanation with an analogy and why it matters. Use when the user asks what something is or how it works.
tools: [wikipedia, web_search]
triggers: [explain, what does, how does, define, in simple terms, eli5, what is meant by]
---
You are explaining a concept to the user. Ground the explanation in a source —
do not rely on memory alone. Follow these steps in order:

1. Identify the CONCEPT the user is asking about. If it is unclear, ASK the user
   and stop.
2. Call `wikipedia` with the concept name to get a grounded definition.
3. If `wikipedia` found nothing, or the result is too thin to explain from, call
   `web_search` with a focused query about the concept.
4. Read what you got and explain the concept SIMPLY:
   - a one or two sentence plain-language definition,
   - one short analogy or everyday example,
   - one sentence on why it matters.
5. End by mentioning the source you used (Wikipedia or the web page URL).

Rules of thumb:
- One tool call at a time; wait for the result before the next step.
- Avoid jargon; if you must use a term, define it in plain words.
- Keep it concise — clarity over completeness.

**If `web_search` isn't in your tool list:** it's a BUILT-IN tool — its absence just means its
dependency (SearXNG) isn't configured on this server yet. Do NOT `create_tool` a replacement.
Explain from what you already know, and note that live web lookup isn't set up (the user can enable
it by setting `SEARXNG_BASE_URL` in the server's `.env`).
