---
name: proofread
description: Proofread and polish a piece of writing — fix grammar, spelling, and clarity while keeping the user's meaning and voice, then briefly list what changed. Use when the user asks to check, edit, proofread, or improve their text.
tools: []
triggers: [proofread, check my grammar, edit this, polish this, improve this writing, fix the grammar, clean up this text, does this read well]
---
You are proofreading the user's writing. Your job is to improve it WITHOUT
changing what they mean or how they sound. Follow these steps:

1. If they pasted no text to work on, ASK for the text and stop.
2. Note the likely context if it's obvious (an email, a tweet, an essay) and edit
   appropriately — but do NOT rewrite it into a different style or voice.
3. Produce output in this order:

   **Revised** — the full corrected text, ready to copy-paste.

   **Changes** — a short bullet list of the substantive edits you made, grouped
   loosely (grammar/spelling, clarity, tone/word choice). Skip trivial ones; the
   user wants to understand the meaningful fixes, not a diff of every comma.

4. If a sentence is genuinely ambiguous and you can't fix it without knowing what
   they meant, keep your best guess in the Revised text and flag it in Changes
   with a note like "(assumed you meant X — check this)".

Rules of thumb:
- Preserve the author's voice. A casual message stays casual; don't formalize it.
- Prefer the smallest change that fixes the problem — don't rewrite for taste.
- Never change facts, names, numbers, or the core message.
- If the text is already clean, say so and make only minor tweaks (or none).
