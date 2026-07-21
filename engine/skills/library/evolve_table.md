---
name: evolve_table
description: Change a table that already exists — add or rename or drop a column, rename or copy the whole table, or bulk-update rows — using the in-place table tools instead of rebuilding and re-inserting. Use whenever the user wants to alter, restructure, copy, back up, or mass-update an EXISTING table.
tools: [list_tables, add_column, rename_column, drop_column, rename_table, copy_table, update_rows, query_table]
triggers: [add a column, add column, add columns, columns to, column to my, column for, another column, a new column, extra column, add a field, rename the, rename column, rename a column, rename my table, drop the, drop a column, drop column, delete the column, remove a column, remove the column, copy my, copy this, copy the, make a copy, copy of, backup, back up, duplicate, port my table, migrate my table, restructure my table, update all, mark all, set all, change all, archive all]
---
The table already exists. The failure this skill prevents is the small-model reflex of **rebuilding it
from scratch** — make a new table, then copy the rows over one `insert_row` at a time — when a single
in-place tool does the job. For hundreds of rows that reflex blows the step budget and can crash the
turn. Reach for the right mutation tool instead.

**0. Look before you touch — `list_tables` first.** You are changing something that already exists, so
get its real name and current columns from `list_tables` before you act. Use the actual table and column
names the user has; don't assume.

**1. Adding fields → `add_column`, IN PLACE. Do NOT make a new table.** If the user wants extra columns
on a table they already have ("add sleep_start and sleep_end to my sleep_log"), call `add_column` once
per new column on the existing table. Each column is a `name:type` spec, same types as `create_table`
(`text`, `integer`, `real`, `date`, `json`). Existing rows simply get `NULL` in the new column — that is
correct and expected; the column is there for data going forward. **Never** create a second table and
port rows just to gain a column.
- Right: `add_column('sleep_log', 'sleep_start:text')` then `add_column('sleep_log', 'sleep_end:text')`.
- Wrong: `create_table('sleep_log_v2', [...all old columns + 2 new...])` then read every row and
  `insert_row` it into the new table.

**2. Copying / backing up / porting a whole table → `copy_table`, ONE call.** When the user genuinely
wants the DATA moved or duplicated ("back up my expenses table", "copy sleep_log to an archive"), use
`copy_table(source, dest)` — a single call that moves every row server-side. It creates `dest` mirroring
the source's columns, types, and key if it doesn't exist yet, or copies the shared columns into an
existing `dest`. Pass an optional `where` to copy just some rows (`copy_table('sleep_log', 'recent',
where="date >= '2026-07-01'")`). **Never** hand-roll a copy as `query_table` + a loop of `insert_row` —
that's the exact pattern this tool replaces.

**3. Renaming or removing structure → `rename_column`, `drop_column`, `rename_table`.** These change the
schema in place, data preserved (except a dropped column's data, which is gone — say so).
- `rename_column('sleep_log', 'score', 'restful_score')`
- `drop_column('sleep_log', 'old_notes')` (a primary-key or indexed column can't be dropped)
- `rename_table('sleep_log', 'sleep_archive')`
Don't rebuild the table to rename or remove a column.

**4. Changing values on many rows → `update_rows`, not delete-and-reinsert.** To set columns on every row
matching a condition ("mark all my 2025 tasks archived", "set status to done where project = argus"),
call `update_rows(table, set={...}, match={...})`. It updates every row matching ALL of `match` in one
call. An empty `match` is refused (it would rewrite the whole table), so give a real filter. Don't
`delete_row` + `insert_row` to change a value, and don't loop.
**Infer the `set` and `match` from the request — act, don't interrogate.** "Mark all my 2025 tasks
archived" → `update_rows('tasks', set={'status':'archived'}, match={'year':2025})`. `list_tables` to
find the table and its columns, then just do it; the obvious `set` value ("archived", "done", "paid")
and the obvious `match` are right there in the request. Reserve a clarifying question for when the target
rows or the new value are genuinely ambiguous — for a clear request, a correct update beats a question.

**5. Confirm what changed.** After the change, tell the user plainly what you did — the column added and
its type, the rows a `copy_table`/`update_rows` touched (it reports the count), the column renamed. If a
`drop_column` destroyed data, say so.

**Worked example — "add two columns to my sleep_log and I'll fill them in later":**
- **Good:** `list_tables` (confirm sleep_log's shape) → `add_column('sleep_log', 'sleep_start:text')` →
  `add_column('sleep_log', 'sleep_end:text')` → "Added `sleep_start` and `sleep_end` (both text) to
  sleep_log; existing rows are empty there for now."
- **Bad (the reflex to avoid):** `create_table('sleep_log_new', [...])` → read all 561 rows →
  `insert_row` × 561 → (often) hit the step limit and fail, having built a duplicate table.

Rules of thumb:
- Act, don't interrogate — `list_tables` to see the real shape, infer the obvious column/value/filter
  from the request, and make the change. Ask only when the target is genuinely ambiguous.
- The table exists — mutate it, don't rebuild it. `add_column`/`rename_column`/`drop_column`/
  `rename_table` change structure in place.
- Moving rows is `copy_table` (one call), never a `query_table` + `insert_row` loop.
- Changing many rows' values is `update_rows` (with a real `match`), never delete-and-reinsert or a loop.
- `list_tables` first so you use the real table and column names.
- This is for EXISTING tables. If the user wants a brand-new table designed from scratch, that's
  `design_table`, not this.
