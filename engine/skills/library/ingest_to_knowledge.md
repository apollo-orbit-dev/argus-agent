---
name: ingest_to_knowledge
description: Add a document, file, web page, or whole website to your knowledge base so it can be searched later. Use whenever the user wants you to remember/learn/index a source for future questions.
tools: [download_file, read_document, fetch_page, crawl_site, add_to_knowledge]
triggers: [add to your knowledge, add this to knowledge, add to my knowledge, knowledge base, remember this document, remember this file, learn this, index this, ingest, read this into, make this searchable, save this to knowledge, add this pdf, add this site, add this page, add this document]
---
The user wants a source added to your KNOWLEDGE BASE so it can be searched later. You must GET the
content and then PERSIST it with add_to_knowledge — getting the content but not calling
add_to_knowledge means nothing was saved.

First, identify the source TYPE, then get its content the RIGHT way:

1. A FILE already in your workspace → go straight to step 3 with `file=<name>` (add_to_knowledge reads
   it, handling PDFs/Word/Excel and scanned PDFs via OCR).

2. A URL to a FILE (a .pdf / .docx / .xlsx link, or clearly a document) → download_file(url) FIRST,
   then add it with `file=<saved name>`.
   **Do NOT fetch_page a PDF** — fetch_page is for HTML pages and will not read a PDF or OCR a scan.
   Downloading it and letting read_document/add_to_knowledge handle it is the correct path.

3. A single web PAGE (an article, one page) → fetch_page(url) to get its text, then add_to_knowledge
   with that text.

4. A whole WEBSITE or docs section ("the whole site", "all the docs", "everything under…") →
   crawl_site(url) to gather many pages, then add_to_knowledge with the crawled text. (Use a
   reasonable limit; map_site first if you want to see what's there.)

5. Plain text the user pasted → just add_to_knowledge with that text.

Then, to PERSIST it:
- Call add_to_knowledge with a clear `source` label (the file name, the site/topic name) and EITHER
  `file=<workspace file>` (for downloaded/uploaded files) OR `text=<the content>` (for fetched/crawled
  pages or pasted text).
- Finally, tell the user what you added and that they can now ask you about it (you'll search_knowledge).

Rules of thumb:
- The goal is a persisted, searchable source — add_to_knowledge is the step that matters; don't skip it.
- Match the tool to the source: download_file for document links, fetch_page for one HTML page,
  crawl_site for a whole site. Never fetch_page a PDF.
- Give each source a distinct label so it can be found (and forgotten) later.

**If `fetch_page`, `crawl_site`, or `map_site` isn't in your tool list:** these are BUILT-IN tools —
a missing one just means their dependency (Firecrawl) isn't configured on this server yet. Do NOT
`create_tool` a replacement. File and pasted-text ingestion still works (`download_file` +
`add_to_knowledge` need no web dep); for a web page or whole site, tell the user that web fetching
isn't set up (they can enable it by setting `FIRECRAWL_BASE_URL` in the server's `.env`).
