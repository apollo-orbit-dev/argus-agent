---
name: extract_to_table
description: Pull the records out of a document, file, or pasted text into a queryable table — read the source, design a schema, create the table, and insert one row per record. Use when the user wants invoices, expenses, rows, or line-items structured into a table.
tools: [download_file, read_document, read_file, create_table, insert_row, list_tables, query_table]
triggers: [into a table, structure this into a table, structure it into a table, make a table from, get the rows out of, get the line items, turn this into a table, turn these into a table, parse this into a table, tabulate this, extract the invoices, extract the expenses, extract the transactions, extract the line items, pull the invoices, pull the expenses]
---
The failure mode this skill exists to prevent: getting the text out of the source and then answering
with PROSE, or dumping everything into one giant `text` column — a paragraph, a bullet list, a wall of
comma-separated values. That is not extraction. The win is typed, queryable ROWS: one row per record,
real columns, real types.

**1. Get the content — route by source type.**
- A workspace DOCUMENT already uploaded (PDF/Word/Excel/scanned) → `read_document(name)`. It handles
  OCR for scans — don't try to read a scanned PDF any other way.
- A CSV or plain-text FILE already in the workspace → `read_file(name)`.
- A URL that points to a DOCUMENT (a `.pdf`/`.xlsx`/`.docx` link) → `download_file(url)` first, then
  `read_document` on the saved name. **Never `fetch_page` a PDF** — `fetch_page` reads HTML pages, not
  documents, and won't OCR a scan.
- Text the user PASTED directly into the conversation → use it as-is, no fetch needed.
Pick the one tool that matches the source; don't guess-and-check across tools.

**2. Design the schema — condensed, right here (don't assume `design_table` also ran; only one skill
fires per turn):**
- Decide the GRAIN first — what does one row represent? One invoice, one expense, one line item, one
  transaction. Say it before naming columns.
- Type each column for real: `integer` for counts, `real` for money/amounts, `date` for dates (ISO
  text), `text` for names/labels/descriptions. Everything-as-`text` is the failure mode — it blocks
  `SUM`/`AVG`/`COUNT` and date filtering later.
- Any field that's a list or nested (line-items within an invoice, tags, multiple categories) → a
  `json` column. Declare it `fieldname:json`, pass a real list/dict to `insert_row` — never a
  stringified/comma-joined blob. Read it back with `json_extract`/`json_each`/`json_array_length`.
- If there's a natural unique identifier per record (an invoice number, a transaction id) add `:key` to
  that column so re-extracting the same source UPSERTS instead of duplicating rows. Skip it if records
  don't have one.

**3. Create or reuse the table.** Call `list_tables` first — if a table already fits the records
you're extracting, insert into it instead of creating a duplicate. Otherwise `create_table` with the
columns and types you settled on in step 2.

**4. Insert one row per record.** Call `insert_row` once per record — an invoice, an expense, a
transaction, a line item — with values matched to the typed columns you created, and any list/nested
field passed as a real list/dict (not text). Keep count as you go: the number of `insert_row` calls IS
the number of records extracted.

**5. Confirm and offer next steps.** Tell the user: the table name, the columns/types you used, and how
many rows you inserted. Then offer a natural next step — `query_table` to look at or filter the data,
or `make_chart` if a summary chart would help.

Rules of thumb:
- Text out, prose back = failure. Text out, typed rows in a table = success.
- One `text` column holding everything is the same failure as no extraction at all — it isn't
  queryable.
- Match the tool to the source: `read_document` for documents (OCR included), `read_file` for
  CSV/plain files, `download_file` + `read_document` for a document URL. Never `fetch_page` a PDF.
- `list_tables` before `create_table` — reuse a fitting table rather than duplicating one.
- Count your `insert_row` calls; report that count back to the user as the row count.
