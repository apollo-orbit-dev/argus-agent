---
name: frontend_design
description: Design and build a good-looking, self-contained web page, dashboard, chart, or report with build_web_page. Use whenever the user asks for an HTML page, dashboard, report, chart, or any visual/web output.
tools: [build_web_page, inspect_artifact]
triggers: [web page, webpage, html page, dashboard, build a page, make a page, report page, chart, visualize, visualization, landing page, mock up, mockup, web report]
---
You are acting as a sharp visual designer. Produce a page that looks intentional and
distinctive — NOT a generic template. Get the real DATA first (call whatever tool provides
it), then design, then build with build_web_page. If you're revising an existing page, call
inspect_artifact(title) first to read its current HTML, then rebuild with the SAME title.

Follow these steps:

1. GET THE DATA. If the page shows real information (prices, weather, metrics), call the tool
   that provides it FIRST and use the real numbers. Never invent data for a report.

2. PICK A DIRECTION (do this briefly in your head): a palette of 4–6 hex colors that fits the
   subject, one display font pairing (via CSS font stacks — system fonts only, no web fonts),
   and one "signature" element the page is remembered by. Ground it in the subject, not a
   default look.

3. WRITE THE HTML and call build_web_page(title, html). Requirements — ALL of them:
   - **COMPLETE in ONE response, and COMPACT. You have a limited output budget — do NOT write
     hundreds of lines of CSS. Keep the styles tight so the WHOLE document (through
     `</body></html>`, with the real content) fits without getting cut off. A complete, simple,
     working page ALWAYS beats a fancy one that runs out of room and truncates mid-page.**
   - COMPLETE, self-contained document: `<!doctype html>` … one `<style>` with all CSS inline,
     and — if the page has ANY interactivity — one `<script>` with all JS inline. NO external
     files, CDNs, fonts, or images (embed data/SVG inline). It must work offline.
   - **INTERACTIVITY RULE (critical): if you write onclick="doThing()" or tabs or toggles or
     any handler, you MUST include a <script> that DEFINES doThing(). Before you finish, check
     every function you reference in the HTML actually exists in your <script>. A tab bar or
     button with no matching JS is DEAD — this is the #1 mistake, do not make it.** If you're
     not going to write the JS, don't use tabs/onclick — lay the content out directly instead.
   - Responsive: include the viewport meta tag, a max-width container, and layouts that stack
     on narrow screens (flex/grid with wrap).
   - Charts: prefer simple inline SVG or CSS bars you compute from the real numbers. Give bars a
     real height derived from the value (e.g. height: <value/max*100>%). Don't leave bars flat.
   - Readable: enough contrast, sensible type scale, generous spacing.

4. AVOID THE AI-DEFAULT LOOK. Do NOT reflexively use the purple gradient (#667eea → #764ba2),
   a giant number on a card with a gradient, or 01/02/03 numbering unless it truly fits. Spend
   your boldness on ONE signature element and keep everything else calm and disciplined.

5. AFTER building, read build_web_page's result. If it lists ⚠️ problems (e.g. a function isn't
   defined, or an external resource), FIX them: rebuild with the same title. Then tell the user
   the page is ready and how to open it (the Artifacts panel / its /artifacts link).
