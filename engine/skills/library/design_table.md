---
name: design_table
description: Design a sound table schema before creating it — pick real column types, use a json column for list or nested fields, choose a key, and decide when to split into a second table. Use whenever the user wants to make a new table or store something in a table.
tools: [list_tables, create_table, read_document, read_file, insert_row]
triggers: [make a table, create a table, new table, set up a table, a table for this, in a table, store this in a table, put this in a table, keep this in a table, track this in a table, store these in a table]
---
Before you touch `create_table`, design the shape. A small model's default — every column `text`,
lists jammed into a prose blob — is the failure mode this skill exists to prevent. The fix is judgment,
not a fixed tool sequence: work through grain, types, json fields, embed-vs-split, and a key, in order.

**0. Is the data already sitting in a source you haven't read?** If the user is pointing at a document,
file, or pasted block of text you have NOT yet read ("make a table from this invoice", "store these
contacts in a table" with an attached CSV), READ it first — `read_document` for a PDF/Word/Excel/scanned
file already in the workspace, `read_file` for a CSV/plain file — before you design anything. Only one
skill activates per turn, so if this one fired instead of the extraction skill, you still own getting the
content: read the source, THEN design the schema below, THEN `create_table` and insert one row per
record. Don't design a schema in the abstract when the real data — and its actual shape — is one tool
call away.

**1. Grain — decide what ONE row is.** One recipe. One expense. One contact. One workout. Say it to
yourself before naming a single column; every other decision (which fields are scalar, which repeat,
what's unique) follows from this. A table with no clear grain ends up with unrelated facts crammed into
one row, or one fact smeared across many.

**2. Typed columns, not all-`text`.** Pick the real type per column:
- `integer` — counts, quantities, ages, whole-number ids.
- `real` — money, measurements, anything with a decimal.
- `date` — dates/timestamps, stored as ISO text but typed so range queries make sense.
- `text` — names, labels, free descriptions.
Typing isn't cosmetic — it's what makes `SUM`, `AVG`, `COUNT`, and date-range filters work later.
Everything-as-text is the base-model default and the thing this skill corrects.

**3. A list, nested, or variable-shape field → a `json` column.** Ingredients, tags, steps, options,
line-items — anything that's naturally a list or an object, not a single scalar. Declare it
`fieldname:json`, then:
- Pass a real Python list/dict as that field's value to `insert_row` — never a comma-joined string.
- Read it back whole, or query inside it with `json_extract(col, '$.path')`, `json_each(col)`, or
  `json_array_length(col)`.
This beats both failure modes: a comma-blob in `text` (unqueryable, ambiguous separators) and rigid
`item1, item2, item3 …` columns (can't grow, mostly empty).

**4. Embed vs. split — the judgment call.** This is the part a small model skips. For any list/nested
field, ask: will I ever filter or aggregate the ITEMS across rows?
- **Embed** it as a `json` column when it's attached data you read back whole and rarely slice into —
  a recipe's ingredient list you just display alongside the recipe.
- **Split** it into a SECOND table, one row per item, when you'll query the items themselves across
  parent rows — "which recipes use eggs", "total grams of sugar across all recipes", "all line items
  over $50 this month". Give the child table a column holding the parent's key value (e.g.
  `recipe_name` matching `recipes.name`) and `JOIN` on it when querying.
Say this plainly to the user if you split: **this store has no enforced foreign keys, no indexes, and
no composite keys** — a "link" between two tables is just a plain shared column value you join on
yourself, not a constraint the database checks. Don't imply otherwise.

**5. Key — add `:key` when a natural unique id exists.** A recipe name, a client email, a date for a
one-row-per-day log. `:key` makes that column the primary key, so a re-insert with the same value
UPSERTS the row instead of duplicating it. If rows don't have a natural unique identifier (e.g. a log of
timestamped events that can repeat), skip `:key` — don't invent an artificial one unless the user wants
it.

**6. Create, confirm, insert.** Call `create_table` with the columns and types you settled on. Tell the
user the shape you chose and briefly why (e.g. "ingredients and steps as `json` since they're lists you
read back whole; `name` as the key so re-adding a recipe updates it"). Then use `insert_row` to add the
data — if you're working from a source you read in step 0, that's one `insert_row` call per record, with
list/dict fields passed as real lists/dicts, not stringified.

**Worked example — a recipe table, right vs. wrong:**
- **Good:**
  `create_table('recipes', ['name:text:key', 'servings:integer', 'ingredients:json', 'steps:json', 'tags:json', 'source:text'])`
  — grain is one recipe; `servings` is a real integer you can sum/average; `ingredients`/`steps`/`tags`
  are `json` because they're lists read back whole; `name` is the key so saving the same recipe twice
  updates it instead of duplicating.
- **Bad (the default to avoid):**
  `create_table('recipes', ['name:text', 'servings:text', 'ingredients:text', 'steps:text'])` with
  ingredients stored as `"flour, eggs, milk"` and steps as one long paragraph — nothing is queryable,
  `servings` can't be summed, and there's no key so re-adding a recipe creates a duplicate row.

Rules of thumb:
- Grain first — if you can't say what one row is, you're not ready to name columns.
- A `json` column beats both a text blob and a wall of `item1..itemN` columns.
- Split only when you'll query the items across rows; otherwise embedding is simpler and correct.
- No foreign keys exist here — a split table's "link" is a shared column value, enforced by nothing but
  your `JOIN`.
- `list_tables` first if there's any chance a fitting table already exists — reuse beats duplicating.
