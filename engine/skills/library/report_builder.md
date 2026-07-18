---
name: report_builder
description: Turn data or findings into a report and shape it correctly for its OUTPUT CHANNEL — a push alert, a Telegram message, an email, a PDF, or an HTML page. The point is choosing the right FORMAT for each channel: how concise vs detailed, how many paragraphs, whether and which chart, what layout — not just which tool to call. Use whenever the user asks for a report, summary, briefing, digest, recap, or write-up.
tools: [make_chart, ascii_chart, build_web_page, make_pdf, convert_to_pdf, query_table, read_file, notify]
triggers: [make a report, build a report, report on, give me a report, write up, weekly report, daily report, summary report, briefing, digest, recap, put together a report, send me a report, email me a report, dashboard, status report, summarize my, summarise my]
---
A report is only good if it fits WHERE IT LANDS. A push alert and a PDF carry the same finding but
look nothing alike. Picking the tool is the easy part — the skill is choosing the right **density,
shape, and chart** for the channel. Work in three moves: identify the channel → build to that
channel's format → run the QA check.

## Core philosophy
1. **Lead with the answer.** The first line is the finding itself, with direction and size ("Signups
   averaged 82/day, down 6% — two slow days"). Never open with method or preamble.
2. **Real numbers only.** Pull them from their source (`query_table`, `read_file`, a tool's output).
   Never estimate or invent a figure in a report.
3. **Density is a choice, not thoroughness.** Match length to the channel (see below). More words is
   not a better report.
4. **A chart is optional.** Use one only when a trend/comparison/breakdown is the point; otherwise a
   short list or a tight table is clearer.
5. **Right chart for the surface:** inline text chart (`ascii_chart`) for anything read in a
   chat/notification (Telegram, push, plain email body); a real image (`make_chart`) for a document
   or page (PDF, HTML, email attachment). Never attach an image where inline text works, or ASCII-art
   into an HTML/PDF where a real chart belongs.

## Match the FORMAT to the channel — this is the job
Denser at the bottom: push ≪ Telegram ≪ email body ≪ PDF/HTML.

| Channel | Length | Chart | Shape |
|---|---|---|---|
| **Push** (ntfy) | 1 line + ≤3 numbers | none (at most a `sparkline`) | headline = the one thing that changed; set `priority`/`tags` by urgency |
| **Telegram** | ≤ ~8 lines, 0–1 short sentence | ≤ 1 inline `ascii_chart` | **bold headline** · 3–6 metric bullets · at most one line of context |
| **Email** | ≤ 1 screen body | inline `ascii_chart`, OR attach a PDF if it has charts / runs long | `subject` = headline · 2-sentence summary · key metrics · one "so-what" line |
| **PDF** | multi-section document | `make_chart` image(s) | title · exec summary · metric table · short sections w/ headings · chart(s) |
| **HTML** | full page | `make_chart` SVG(s) | **follow the `frontend_design` skill**: hero summary · stat cards · charts · sections |

### Concrete shapes to copy (density matters most on the small channels)
**Push** — `notify` channel="push":
```
subject: Revenue $12.4k today (+8%)
message: Orders 214 · AOV $58 · refunds 3
```
**Telegram** — your reply (or `notify` channel="telegram"), chart pasted verbatim:
```
**Weekly steps — 49k/wk avg, trending up**
• Best: week 4, 61k   • Low: week 3, 38k   • Total: 196k
```
(then, if it helps, ONE ascii_chart in a code block)

**Email** — `notify` channel="email":
```
subject: Q1 sales report — $157k, Feb the standout
body: Q1 closed at $157k. February led at $65k (+63% vs Jan); March eased to $52k.
      • Jan 40k  • Feb 65k  • Mar 52k
      Momentum is up quarter-over-quarter; watch whether March's dip continues.
```
For a richer email (charts, more than a screen), build a PDF and attach it (`attachments=[…]`), and
keep the body a two-line cover note.

**PDF / HTML** — a document, not a message: title, a one-paragraph executive summary, a metrics
table, then a short section per idea with a `make_chart` figure. For HTML, hand off to
`frontend_design` for the layout; don't hand-route it here.

## The report spine (trim it to the channel's density)
1. Headline / executive summary — one sentence, the finding.
2. Key metrics — the 2–5 numbers that matter, each with unit and a delta vs baseline where you have one.
3. Supporting detail — chart, table, or short sections (skip entirely on push/Telegram).
4. So-what / next step — only if there's a real one.
On push you keep only 1–2; on Telegram 1–2 plus maybe a chart; email adds a short 3; PDF/HTML use all four.

## QA before you send (required)
- Does the FIRST line state the finding? (not "Here's your report…")
- Is it the right LENGTH for the channel? (a push must not be a paragraph; a PDF must not be three lines)
- Right chart call for the surface — `ascii_chart` for chat/push, `make_chart` for PDF/HTML — or none?
- Every number real and from a source, with units?
- Did the chart/report actually reach the user (pasted verbatim for text, attached/linked for files)?

Rules of thumb: choose the shape before the tool; a clean table often beats a chart; length is not
thoroughness; never invent figures.
