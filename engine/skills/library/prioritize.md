---
name: prioritize
description: Take a brain-dump of tasks and turn it into a prioritized, actionable plan — grouped by what to do now, next, and later, with a quick reason for each. Use when the user is overwhelmed by a to-do list or asks what to work on first.
tools: []
triggers: [prioritize, help me prioritize, organize these tasks, what should i do first, sort my to-do, too much to do, where do i start]
---
You are helping the user prioritize a set of tasks. Follow these steps:

1. Gather the TASKS from what they wrote. If they only asked for help but listed
   nothing, ASK them to dump their tasks and stop.
2. For each task, weigh two things: how URGENT it is (a real deadline or a cost to
   delay) and how much it MATTERS (impact toward their goals). Note anything that
   blocks other tasks.
3. Group the tasks into three buckets and present them:

   **Now** — urgent and important; start today. 
   **Next** — important but not urgent, or waiting on a Now item.
   **Later** — low urgency/impact; fine to defer or drop.

   Under each bucket, list the tasks with a short (≤10 word) reason each.

4. Then add two one-line callouts:
   - **Quick wins:** any 2-minute tasks worth clearing immediately.
   - **Watch out:** anything with a hidden deadline or that blocks other work.

Rules of thumb:
- If a deadline or constraint would clearly change the order and isn't stated, ask
  ONE focused question — otherwise proceed and note your assumption.
- Be decisive: put each task in exactly one bucket. Don't hedge.
- If something on the list isn't actually worth doing, say so and suggest dropping it.
- Keep it scannable — this is a plan to act from, not an essay.
