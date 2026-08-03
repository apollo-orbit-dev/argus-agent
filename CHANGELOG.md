# Changelog

All notable changes to this project are documented here.

## Unreleased

### Added
- **Agent profiles.** A named, switchable **snapshot** of what Argus is for a task — persona (SOUL),
  base system prompt, the per-tool Allow/Ask/Deny matrix, skill visibility, which standing rules
  apply, model-role bindings and the behavioural feature flags. Create / duplicate / rename / delete
  / activate from Settings; the active profile is shown on every page, because a user who cannot see
  which profile is live cannot reason about what the agent may do. The binding is **per session**
  with a global default for new ones, so Telegram and the dashboard can run different profiles at
  once. **Memory is NOT part of a profile** — it is about the user, not the task, and stays global,
  as do sessions, tables, connections and credentials (a profile selects a model *role binding*,
  never an API key).
  - **Snapshot, not patch.** A profile fully specifies every field it governs, so what you read in
    it is what runs — no action-at-a-distance from a global edit. The cost is staleness, which is
    handled explicitly: **a tool added after a profile was written resolves to `ask`** — never
    `allow`, and never silently inherited from the global permission store. `ask` still advertises
    the tool, so a new capability stays discoverable; the dashboard reports per profile how many
    tools are "not configured — currently Ask".
  - **Activation never blocks, so it is announced.** Switching profiles emits a trace event naming
    the profile and every tool whose permission is *wider* than under the outgoing one (narrowing
    needs no announcement), and the dashboard toasts the same list.
  - **Skills are scoped, not gated.** A profile chooses which skills it can see; there is no
    per-skill permission (a skill is prompt text, it executes nothing). A hidden skill is invisible
    to *every* selection mode, including an explicit by-name request. A skill added after the
    profile was written is visible.
  - **Migration is a no-op for existing installs:** with no `profiles.json`, the current settings
    become a profile named `Default` that is active and default, and the engine's resolved config is
    identical to what it was before.
  - **Reachable from Telegram:** `/profiles` lists them, marking the one active in *this* chat and
    the global default; `/profile` reports which one this chat runs under and why; `/profile <name>`
    binds **this chat only** (never the global default) and replies with the same widened-tools
    announcement the dashboard toasts — stated explicitly when nothing widened, because silence
    would read as "not checked". An unknown name is refused with the valid names and no fuzzy
    matching. Creating, renaming, deleting and editing a matrix stay in the dashboard.
- **Mid-turn steering (`ENABLE_STEERING`, ON by default — this CHANGES what a plain message does while the agent is working).** Send a message *while* the agent is
  working and it changes course inside that run instead of cancelling it. The text is appended to
  the run's next tool result inside a bounded marker — the only mid-turn slot that doesn't break
  the message-role alternation providers validate — and the system prompt tells the model that a
  marker bearing **this run's nonce** is the user speaking, with the same authority as the original
  request. The nonce is random per RUN, lives only in memory and in the outbound request, and is
  never written to a stored message, an event, the trace or a log; the conversation on disk carries
  an opaque sentinel instead. So text arriving from a web page, a file or another tool *cannot*
  reproduce a valid marker, and the model is never asked to judge whether a "user instruction" in
  tool output is real. Marker-shaped text with the wrong id is reported in the trace
  (`steer_rejected`) and flagged to the model as untrusted. On every channel: a plain message
  during a run steers (with an immediate confirmation of which reading happened), `/task <text>`
  queues a **new** task instead, `/steer <text>` is the explicit form, and `/stop` still kills the
  run. A steer that arrives after the model has already written its answer is not dropped and does
  not force an extra step — you are told it landed late and it runs as the next message.
  Dashboard: a "send to the running task" box on the console, plus the steer inline in the trace.
  - **`/stop` means stop.** Anything queued to steer is discarded when the run is stopped or
    preempted, rather than resurfacing as a fresh task — a steer must not outlive the run it
    was aimed at.
  - **Upgrade note.** Previously a message sent mid-run CANCELLED that run and started over, discarding every tool call already made — a one-line correction cost the whole turn. It now redirects instead. If you relied on "send a message to interrupt", use `/stop` (still kills the run) or turn steering off in Settings → Mid-turn steering.

### Fixed
- **Nothing can start a second, concurrent run on a session any more.** `POST /run` and Telegram's
  `/retry` both called `run_task` again while a run was in flight, so two turns interleaved into
  one message history and one working set. Both now either steer the live run or queue a new task
  behind it. (The plain Telegram message path already avoided this by *cancelling* the run in
  flight; with steering on it redirects that run instead of killing it.)

## 0.15.1

### Fixed
- **The confirm-to-type gate rejected the string it displayed.** The modal writes
  `Type "v0.15.0" to confirm` into a label styled `text-transform:uppercase`, so it *showed*
  `TYPE "V0.15.0" TO CONFIRM` while comparing strictly against the lowercase literal — typing
  exactly what the UI showed left the button disabled with nothing explaining why. Worst on the
  update path, where the required string is the target version and this is the last gate before
  restarting a live instance. The literal now renders untransformed (so what you read is what you
  type), and the comparison is trimmed and case-insensitive: typing the name is a deliberateness
  gate, not an identity check, and the target is already fixed before the prompt appears.

## 0.15.0

### Added
- **Edit skills and created tools from the dashboard.** A ✎ on any skill row (Developer → Library)
  opens the raw markdown; saving writes a **copy-on-edit override** into `created_skills/` and
  re-registers it live, so the shipped library is never modified and a bad edit is always
  revertible. An edited built-in stays listed as a built-in with an `edited` tag — the tag is what
  says "this one is yours now", which also means Argus updates to it won't reach you. Created tools
  get the same ✎, and saving routes through `create_tool`, inheriting its AST import gate, auto
  test-run, hardcode check and approval gate — so a failed edit refuses the save and leaves the
  working tool intact. Built-in tools are not editable: they are Python in the repo, not runtime
  content.
- **The benchmark knows when it is lying.** A trivial, dependency-free canary probe runs before the
  first task and every 10 thereafter; two consecutive failures **abort the run**, write the partial
  result, and exit non-zero. Every result now carries a `validity` verdict (`ok` / `degraded` /
  `aborted`) computed from the run's own shape, a per-decile no-tool sparkline, and a
  `compliance_gap` column. Non-ok rows are marked in the report and excluded from the size curve.
  This exists because a 35B arm once ran clean for 42 runs, produced no valid tool call for the
  remaining 125, and published all of it as model capability — found by hand-auditing a transcript,
  not by the harness.
- **Benchmark runs survive a crash.** The result file is written after every task, marked
  `complete: false` with `tasks_run`/`tasks_planned`, instead of only at the end. A host reboot
  previously discarded 48 tasks of completed, already-judged work. Partial arms are excluded from
  the size curve — the battery is ordered T1→T4, so a partial is biased easy and its rates are not
  comparable to a full arm's.
- **An upper anchor for the benchmark**: a full 2×2 (mode × scaffold) on a 284B model, so the
  scaffolding claim is no longer measured only inside the small band. See "Measuring it" in the
  README.

### Changed
- **A tool set to Deny is no longer advertised to the model.** It used to be sent in full — name,
  description and JSON schema — on every single turn, and was only refused once the model had
  already spent the tokens and picked it. Now it is absent from both catalogs (native `tools` array
  and the manual-mode system prompt), so a denied tool costs nothing per turn and the model is no
  longer told it has a capability it does not have.
  - **The gate is unchanged.** Not advertising a tool is an optimization and a hint, never the
    security boundary: a denied tool named out of conversation history (or hallucinated) is still
    refused at call time with "Blocked by your policy", and dispatch (`get`/`validate`/`names`) is
    still deliberately unfiltered. Verified live against two providers — both models did re-emit a
    call for a tool removed mid-session, and both were refused.
  - **Composes with progressive tool disclosure.** Neither `find_tool` nor a tool created mid-turn
    can re-admit a denied tool, and a denied tool no longer consumes a slot of the disclosure
    budget. `find_tool` says so honestly instead of silently returning nothing.
  - Nothing is denied by default, so this is a no-op on a default install, and it applies only when
    interactive approvals are enabled — the same flag that switches the gate itself.

### Fixed
- **Telegram no longer silently drops long replies.** A message over Telegram's 4096-character limit
  was rejected, the plain-text fallback re-sent the *same* over-long text, and the second failure was
  swallowed at debug level — the user received nothing, with no error anywhere they'd see it.
  Replies are now split on paragraph/line/word boundaries with **balanced HTML**: tags left open at a
  cut are closed and reopened in the next part (a naive split produces markup Telegram rejects,
  reproducing the original bug less often and more confusingly). Code blocks split on newlines and
  are reopened as their own block. This also covered background delivery (scheduled results, watch
  alerts, the `notify` tool), the list commands (`/skills`, `/tools`, `/memories`, …), and a
  pre-existing flaw where the answer path split *markdown* before HTML expansion pushed it back over
  the limit.
- **Benchmark runs now exercise the scaffolding they measure.** Memory auto-extraction, rule
  auto-detection and session auto-titling are dispatched as detached background tasks; the benchmark
  removed each run's temporary `data_dir` the instant the answer was ready, so those tasks raced the
  teardown and died mid-write. They are now drained (bounded) before teardown. Any capability they
  contribute had been unmeasured in every scaffold-on arm.

## 0.14.0

### Added
- **Update Argus from the dashboard and from Telegram.** A new "Update Argus" card in Settings and an
  `/update` slash command move this install to the newest published **release tag** and restart it —
  no checkout, no ssh, no deploy script. Updates follow tags, never `main`: a tag is the point where
  the release process has asserted the version is coherent.
  - **Preview before acting.** Shows `v<current> → v<target>` plus the CHANGELOG sections in between,
    read from the target tag (they don't exist in the running checkout yet).
  - **Preflight refusals say why**, each with its own readable message: not a git checkout, no
    `origin`, a dirty tree (it names the files), the running Python not being the checkout's own
    virtualenv, no network, no tags, already up to date, ahead of every tag, an unresolvable tag
    (shallow clone), or no pip.
  - **Automatic rollback.** If `pip install -e .` or the post-install verification fails, the previous
    ref is checked out and reinstalled immediately, both streamed, and no restart is offered. Only if
    the rollback itself fails does it fall back to printing the exact commands.
  - **Your data is never touched.** `.env`, databases, `model_presets.json`, workspaces, routines,
    created tools/skills and `SOUL.md` are all gitignored, so `git checkout <tag>` structurally cannot
    write to them — and there is a test that proves it against the real `.gitignore`.
  - **Restart handoff.** systemd when (and only when) the user unit is active *and* its MainPID is
    this process — otherwise restarting the unit would start a second instance and collide on the
    port. Otherwise re-exec in place; on Windows the restart instruction is printed instead. The HTTP
    response and the Telegram reply are always delivered before the process is replaced.
  - `/update` previews, `/update confirm` installs, `/update revert confirm` goes back. The "✅ update
    complete" acknowledgement is delivered on the way back up, and warns if the booted version is not
    the expected one.
- **Friction log.** The loop already knew when it gave up — an exact-repeat tool thrash, or a second
  parse failure — and threw the signal away. Those are now appended to a log with the tool, the
  attempt count and the error actually hit, readable with `argus friction`, grouped by frequency so
  the top of the list is the next thing to fix. Records, never intervenes: the turn is identical with
  the log enabled, disabled, or failing to write.
- **`answered`, a second benchmark metric.** `solved` requires the declared tool chain AND a passing
  judge, so a model that answers correctly without reaching for the tool scored as a failure.
  `answered` is the judge verdict alone. The gap between them is the share of tasks a model got right
  by its own route — near zero for a 3B, 14-16 points for a capable model.

### Fixed
- **`create_tool` no longer advertises `httpx` to sandboxed tools.** The tool description told every
  model it could import `httpx`, while new tools default INTO the container sandbox, which has the
  standard library and nothing else. Argus instructed the model to do the thing it then failed it
  for, and the failure said "fix the code" when the code was fine. The description is now built for
  the environment the tool actually lands in, and a missing module in a sandboxed run names the
  module and both real escapes.
- **Environment failures state the constraint and the escape.** Audited all 120 error strings a model
  can read: 58 environment, 62 caller-input. Caller-input messages are unchanged — a bad table name
  is self-evident. Eleven environment ones now say what to do instead: blocked sandbox egress, the
  `schedule_task` time format, sandbox-down for `exec_python` and created tools, `CALL_TOOL`
  composition, `ask_data` with no model, PDF rendering (missing *or* half-installed), and OCR.
- **The Run button reflects the session you are looking at.** One global flag meant a turn in flight
  on any session blocked sending to every session, though the backend has always supported
  concurrent turns. The button, the status line, the Enter/Ctrl-Enter handlers and the clarify gate
  all derive from the viewed session, and the run box clears at send so it cannot fire one session's
  prompt at another.
- **Sandbox status distinguishes "disabled" from "not started yet."** Toggling the sandbox on left
  three indicators contradicting each other, because one condition collapsed two states. Enabled but
  unconstructed now says so and asks for a restart, rendered as an action rather than an error.
- **An auto-detected rule no longer invents a run.** `rule_saved` emitted a synthetic run id on the
  session's own stream, and the dashboard treats any unseen run id as a new run — so it registered a
  run that never completed and showed permanently as an error. Now routed through the `__control__`
  channel.
- **Telegram `/reset` clears the event trace**, matching the dashboard. The same user-facing action
  did two different things depending on the surface.
- **`create_tool` cap-2 rubrics no longer double-count tool use.** 35 of 56 benchmark rubrics named a
  tool, so the judge graded tool use — which the chain predicate already measures — and flipped
  between readings, scoring the identical answer 3 in one arm and 0 in another. Rubrics now assert
  answer quality only; the tool requirements moved into `expect` where they belong.

## 0.13.0

Per-connection request options, Telegram session commands, dashboard responsiveness fixes, and a
sandbox isolation guard. Also the first tagged release carrying progressive tool disclosure — shipped
OFF and not yet usable; see the note under Experimental.

### Added
- **Per-connection request options.** A connection can now carry `extra_body` (a free-form JSON object
  merged verbatim into every request), per-model `sampling` overrides, and a `reasoning_style`. The
  `extra_body` escape hatch means a model whose thinking toggle Argus has never seen — a different
  `chat_template_kwargs` name, a vendor budget field, guided-decoding params — works without a code
  change. `messages`, `model` and `stream` are refused. Defaults keep every existing deploy
  byte-identical.
- **Telegram `/sessions`, `/session`, `/rename`**, and `/new` relabelled to `/reset` to match the
  dashboard's reset-vs-new-session distinction. Session ids render tap-to-copy.
- **Automatic session titles.** A dashboard session's placeholder id is replaced with a generated
  title after its first completed turn, in the background. Silently skips when no model is configured
  for the aux call; never overwrites a manual rename.
- **Live sidebar refresh** via a reserved `__control__` event channel, so an out-of-band rename (an
  auto-title, another tab, Telegram) updates the session list without a page refresh.
- Benchmark report gains an `abort` column: the share of runs the loop itself ended on
  `stuck_repeating`. Diagnostic only — does not affect `solved`. Legacy results render `—`; the
  baseline arm (observer off) renders `n/a`.
- `inspect_tool` now describes BUILT-IN tools, not just created ones.

### Fixed
- **Sandbox: refuse to adopt another instance's container.** Podman object names are global per OS
  user, so a second Argus with the sandbox on would reuse the first's workspace container — whose
  bind mount points at the *first* instance's files. Two instances would have silently shared a
  workspace. Containers/networks can now be namespaced per instance (`SANDBOX_INSTANCE`), and
  adoption is refused when the mount does not match.
- **Console echoes your message immediately** instead of leaving it invisible until the turn finished.
- `SUM`/`AVG`/`TOTAL` over a declared-TEXT column now raises instead of silently returning `0.0`.
  Conservative: only the unambiguous bare-column form is refused.
- `create_tool` no longer reports "created and verified" when its hardcode check could not run —
  it now says plainly that it could not confirm the tool uses its inputs.
- The benchmark's `no_observer` predicate was passing vacuously: observer events were never captured,
  so any battery asserting "the loop never gave up" was told it held regardless.
- `update_rows` description reworded so a small model reaches for it on "change/set/mark all X".
- Emoji and other non-BMP characters now round-trip through skill frontmatter.

### Experimental
- **Progressive tool disclosure** (`TOOL_DISCLOSURE_MODE`, default `off`): advertise only the K most
  relevant tools per turn instead of the whole catalog. Narrows PRESENTATION only — a hidden tool
  still executes if the model names it, and `find_tool` retrieves it. **Not usable yet:** at the
  shipped default (K=12) its coverage pre-flight is red — 21 of 52 benchmark chain tasks lose a tool
  they need. Enabling it is not recommended until that closes. Off by default, with verified
  byte-identical behaviour when disabled.

## 0.12.1

Hardening from an external code review.

### Security
- **`POST /run` now honors the admin token when one is set.** It was the one open endpoint on a
  token-protected instance, and it's the most consequential one — it drives the agent with the full
  tool registry, the files workspace, and (if enabled) the sandbox. Open still means open (unset
  token = local dashboard as before); the dashboard already sends the header, and the Telegram bot
  calls the engine directly, so both are unaffected.

### Fixed
- **Session store opens in WAL mode** (`journal_mode=WAL`, `synchronous=NORMAL`). Per-step message
  writes happen on the async event loop; WAL keeps a write from blocking readers or stalling the loop
  and hitching the live trace stream mid-turn.
- Corrected the README's post-action-verifier description to match the code (it fires on batch
  deletes/cancels, not on every create/schedule) and removed a long-stale "M1 skeleton" module
  docstring.

## 0.12.0

Auto-start on boot: `argus service`.

### Added
- **`argus service` — install a systemd service so a self-hosted Argus starts on boot.**
  `argus service install` writes a **user-level** systemd unit (`systemctl --user`, no `sudo`),
  `argus service uninstall` removes it, and `argus service status` reports installed / enabled /
  active / linger. The unit runs the existing foreground entry (`argus run`) from the clone's own
  venv, so nothing new has to be maintained. The unit name auto-derives (`argus.service`, or
  `argus-<port>.service` for a second instance on the same machine), so `--name` is only ever an
  optional override.
- **Install/remove the service from the dashboard too.** A "Service" card in Settings shows the
  current status with Install / Uninstall buttons, backed by admin-gated `GET /service/status` and
  `POST /service/{install,uninstall}` endpoints. Because the unit is user-level, the backend does
  everything as its own user — a web request never needs `sudo`.

### Notes
- Installing never double-starts: when the instance is already running it enables-for-boot only and
  takes over on the next restart/reboot. Uninstalling only removes boot-autostart — it never stops
  the running server. If `loginctl enable-linger` needs elevated privilege and fails, the exact
  manual command is surfaced rather than silently reporting success.
- Linux/systemd only; on other platforms the CLI and dashboard refuse cleanly.

## 0.11.0

Sandboxed created tools.

### Added
- **Created tools can run inside the container sandbox.** A model-authored (`create_tool`) tool can
  now execute inside the long-lived, rootless **podman** container with the full Python standard
  library and the real writable workspace, selected per tool by a `sandboxed` flag (defaults on when
  the sandbox is available). The two paths are explicit: a *host-side* tool keeps the AST-restricted
  sandbox and can still **compose** — call any other Argus tool by name — while a *container* tool
  gets the full stdlib but runs in isolation, with no tool composition. A stdlib-only in-container
  runner marshals `{code, args}` over stdin/stdout; args are validated host-side (the tool's pydantic
  model) before they cross into the container.
- The tool-creation directive now tells the model about the trade-off, so it picks `sandboxed=false`
  when a tool needs to call another tool, and leaves it on otherwise.

### Fixed
- **Fails closed.** A `sandboxed=true` tool refuses to run when the sandbox is off or unavailable
  rather than silently falling back to host execution — the code was authored assuming the full
  stdlib. The executing runtime is resolved from the same per-run `enable_sandbox` flag the gate
  checks, so the two can never disagree.
- A dashboard config toggle (e.g. enabling the sandbox) now survives a server restart — a stale value
  left in the process environment no longer shadows the updated `.env`.
- The sandbox setup output no longer emits raw ANSI colour codes to the dashboard's log view.

### Docs
- README: a "What makes it different" highlights section, CI/version/Python/license badges, and a
  table of contents.

## 0.10.0

The container-sandbox release.

### Added
- **Container sandbox (opt-in, off by default).** `exec_python` and the agent's file workspace can
  now run inside a long-lived, rootless **podman** container instead of the language-level AST
  sandbox — giving the model the full Python standard library and a real writable home directory,
  while the container boundary keeps the host safe. Built in two stages:
  - *Isolation:* a `SandboxRuntime` seam (`FakeRuntime` for tests so CI needs no container runtime;
    `PodmanRuntime` for real), the workspace as a bind-mounted tree, resource caps gated on the
    host's actual cgroup controllers, and fail-closed registration — if the sandbox is enabled but
    the runtime is missing, `exec_python` is disabled rather than silently downgraded.
  - *Egress:* the container joins a `--internal` podman network whose only exit is a
    policy-enforcing proxy sidecar it can't bypass. `SANDBOX_NETWORK` selects `proxy` (default —
    public internet, no LAN), `none` (air-gapped), or `lan` (full network, the escape hatch).
  - Setup is a one-time `scripts/setup-sandbox.sh` or a **Set up sandbox** button on the dashboard's
    Settings page; a Sandbox card shows runtime/network/egress health. See the README.
- **`geocode` tool** (from 0.8.2/0.8.3, first collected here) and **tool composition** — created
  tools can call any built-in by name.

### Changed
- **One egress policy** (`engine/sandbox/egress_policy.py`) now backs the created-tool guard, the
  `download_file`/watch guard, and the in-container proxy — replacing two divergent implementations.
- **The file workspace is a directory tree.** `safe_path` replaces the old flatten-to-basename
  behaviour (subdirectories allowed, traversal/symlink-escape/TOCTOU all closed), and the workspace
  moved to `data/workspaces/<name>` (a legacy `data/workspace` is migrated automatically). The same
  directory is used whether the sandbox is on or off, so toggling it never loses files.

### Fixed
- **`PATCH /config` is admin-gated**, like every other mutating route — it was the one open write on
  a token-protected instance (it can repoint the model endpoint).
- **A throwaway engine can no longer write the developer's real `.env`** — the persist path follows
  `data_dir`, so tests and dashboard-driven QA stay isolated.
- **The dashboard health check no longer runs a metered search** to draw its status dot (from 0.9.0
  line, restated: SearXNG is probed with `/healthz`).
- Numerous sandbox correctness fixes found in review and on real hardware: recreate a container when
  its network no longer matches the mode (so a `lan → proxy` switch can't fail open), verify the
  egress proxy is actually listening, close a DNS-rebind TOCTOU in the proxy, drain CONNECT headers
  so nothing is smuggled into the tunnel, and drop unenforceable resource caps rather than refusing
  to start (the deploy host boots `cgroup_disable=memory`).

### Platform / ops
- **Windows:** the base agent runs, but the container sandbox is not supported on native Windows —
  the status readout and setup button say so plainly instead of failing on a missing `bash`. Run
  Argus under WSL for full support.
- `deploy.sh` rebuilds the sandbox image when its build inputs change; `.containerignore` keeps
  secrets and agent data out of the build context.

## 0.9.0

### Added
- **Routine builder: each tool's argument contract.** A tool-step's args are hand-typed JSON, but
  `/routine-meta` returned tools as bare names — a dropdown of 70+ tools and a blank box, with the
  source as the only documentation. It now also returns `tool_params` (`name`, `type`, `required`,
  `description` per argument), shown under the args box with an **insert template** button that fills
  in the required keys. Tools with no arguments say so explicitly.

- **Scheduled tasks can be cancelled from the dashboard.** The card was read-only — the agent could
  cancel a job via `cancel_scheduled_task`, but the owner couldn't, including for jobs created from
  Telegram. Adds an admin-gated `POST /scheduled/delete` and a ✕ per row with the shared confirm
  dialog. Deliberately not session-scoped: the agent's tool scopes to its own session so one chat
  can't cancel another's, but the dashboard is the owner's view of every session's jobs.

### Fixed
- **The SearXNG health probe no longer runs a real search.** `/status` probed SearXNG with
  `/search?q=ping&format=json` — a genuine query that SearXNG forwards to every configured engine,
  including metered ones like Brave. The dashboard polls `/status` every 5 seconds, so an open tab
  spent roughly **720 real search-API calls an hour** of the owner's paid quota to render a green
  dot. Now probes `/healthz` (200, 2 bytes, ~3ms, no engine touched). A SearXNG too old to have it
  answers 404, which still counts as reachable, so the worst case is a cosmetic status code rather
  than a false outage.
- **Harness-injected nudges no longer look like messages you sent.** The observer's repeat nudge, the
  create-without-verify nudge, and the output-truncated reprompt are injected with `role: "user"`
  (the model has no mid-conversation system slot), so the transcript rendered them as your own
  bubbles. They now render as a centred, dashed "Argus nudge" note, detected by the `[note] ` prefix
  those injections already share.
- **Action-column buttons no longer sit out of alignment.** `.actions` set `display:flex` on a
  `<td>`, which takes the cell out of the table layout algorithm so it never stretches to the row
  height — a 92px row (long wrapped task text) got a 44px cell, leaving the button floating and the
  cell's bottom border short of the row's. Only visible once a row wrapped, which the new Scheduled
  tasks delete column made routine. Affects the Routines, Watches, Files and Scheduled tables.
- **Watches delete reported success on failure.** The fetch shim resolves a 401 rather than rejecting,
  so a missing admin token toasted "Stopped watching" and changed nothing. Now checks `res.ok`, like
  the session mutations.
- **Tool and skill descriptions are no longer truncated** on the Developer page. They were clipped at
  140 characters even though the row already wraps; the longest built-in description is ~1170
  characters, so most of it — including the part explaining *when* to use the tool — was hidden.

## 0.8.3

### Fixed
- **`geocode` now returns JSON**, so created tools can actually use it. Its output has two readers —
  the model reading a tool result, and created-tool code calling `geocode()` through tool
  composition — and only structured output serves both. With the prose format shipped in 0.8.2,
  `json.loads(geocode({...}))` raised inside a created tool, the tool fell into its `except` branch
  and reported "Could not find location", and the model concluded composition was impossible and
  hardcoded a latitude into the tool source — the exact value `geocode` had just returned. Errors
  are JSON too, since composed code calls `json.loads` unconditionally.
- **The tool-creation directive now documents tool composition.** Every registered tool name is
  injected as a plain callable into created-tool globals at call time, and the AST validator has
  dedicated handling for those calls — but nothing ever told the model the capability existed. Added
  a `COMPOSE` section with a worked `geocode` example, plus an explicit rule against hardcoding a
  value obtained from a tool call ("that tool works for every input, and your hardcoded copy works
  for exactly one").
- **`geocode` added to the directive's list of built-ins** — it was missing, so the model would have
  classified it as a *created* tool it could delete on request.

## 0.8.2

### Added
- **`geocode` tool** — look up a place's latitude, longitude and timezone by name, with a
  disambiguating hint (`Springfield, IL`, `Cambridge, UK`). The geocoder already existed as a helper
  shared by `weather` and `time_in_zone`, but it had no `Tool` subclass, so the model could never
  call it — and created tools run in a sandbox that can't import engine modules, meaning any
  model-authored tool needing coordinates had to re-implement geocoding (including the state-hint
  disambiguation) against the raw API. Returns `latitude=… longitude=… timezone=…` rather than prose,
  because the output usually feeds another computation.
- **Dashboard: the session id is shown in the Runs card header**, click to copy. The sessions sidebar
  shows a session's *name*, so once you rename one the id had nowhere left to surface — and the id is
  what you need to quote when reporting a failure. Falls back to selecting the text where
  `navigator.clipboard` is unavailable (plain http over a LAN is not a secure context).
- **Eval harness: `--compare-config`** — A/B a *config* change instead of a skill (treatment = config
  + overrides, baseline = as-is, no skill ablated in either arm), so loop-level interventions can be
  measured with the same pass^k machinery. Unknown config fields are rejected, so a typo can't
  silently A/B nothing.
- **Eval scoring: `max_counts` and `no_observer` predicates** — express the *absence* of a pathology
  rather than the presence of a result, for batteries of deliberately unanswerable tasks where the
  right behaviour is to answer gracefully rather than complete a chain.
- **Eval reports: a Mechanism check** — for config arms, lists the observer events seen in each
  flipped case and prints an explicit **INCONCLUSIVE** warning when no flipped case shows the
  intervention firing. Added after a run printed **KEEP** off a single flipped case in which the
  mechanism never ran once. Reports also roll up observer events per arm.
- **README: "Small-model scaffolding"** — a section naming the layers that exist specifically to make
  a small model dependable (the loop-health Observer, switchable tool-calling modes, deterministic
  skill steps, explicit-first skill selection, tight tool contracts, the post-action verifier,
  `clarify`, the reliability instrument, standing rules + memory) and the failure each one counters.
- **README: "Measuring it"** — documents the two eval harnesses and how to run them: the
  cross-model capability benchmark (`python -m engine.eval.benchmark`) with the founding run's
  numbers, and the pass^k skill A/B (`scripts/skill_eval.py`) with its KEEP / no-lift / REGRESSION
  and over-fire verdicts.
- **The skill-eval harness now ships.** `scripts/skill_eval.py` and the A/B batteries + fixtures
  under `docs/ab/` were previously gitignored, leaving `engine/eval/` public with no entry point.
  `.gitignore` now un-ignores exactly those (the personal deploy/probe scripts and the local A/B
  reports stay out).

### Fixed
- **The observer's repeat-nudge now fires for calls that fail validation.** The loop had two ways to
  finish a step with a tool exchange and only one reached the observer's repeat check: the
  validation-failure branch went straight back to the top of the loop, so a call repeating with
  malformed arguments counted toward the `stuck_repeating` abort but was never nudged to change
  approach. That had been true since the nudge was written, and it is backwards from where the need
  is greatest — repeated malformed arguments are a signature small-model failure, and a validation
  error carries almost nothing the second time.

## 0.8.1

### Added
- **Durable run traces** — the tool/step trace behind the Runs card now persists to a SQLite-backed
  `events.db` sink when `enable_trace_persistence` is on (default), so runs survive a server restart
  instead of vanishing with process memory. `model_request` events are excluded from the sink to keep
  it lean. Retention is config-driven via `trace_retention_mode` (`age+runs` / `age` / `runs` / `off`),
  `trace_retention_days`, and `trace_keep_runs_per_session`; `/status` now reports `trace_persistence`
  so clients can tell whether the current process has it wired up.
- **Dashboard: trace-persistence controls** — a new "Run trace persistence" card on the **Settings**
  page (under Runtime limits) exposes the on/off switch (labelled as applying on restart, since the
  sink registers at startup), the retention-mode select, and the retention-days / keep-runs number fields,
  all reflecting from and PATCHing `/config` like the other runtime toggles. The Runs card's
  empty-state copy is now conditional on `/status`'s `trace_persistence`: "No runs yet" when
  persistence is on (runs really do survive a restart), the existing "not kept across a restart"
  wording when it's off.

## 0.8.0

### Added
- **Durable sessions** — conversations now persist to a SQLite-backed `SessionStore` (raw message
  log + working set) instead of living only in process memory, so a restart no longer discards
  history. New `/sessions` endpoints back it: `GET /sessions` (list, with per-session message
  counts), `POST /sessions` / `PATCH /sessions/{id}` / `DELETE /sessions/{id}` (admin-gated
  create/rename/delete), and `GET /sessions/{id}/messages` (paginated transcript read). Ephemeral
  (`__`-prefixed) sessions are excluded from the store and never appear in the list.
- **Dashboard Sessions sidebar** — the Console page now has a sessions rail (left of Runs) to
  create, switch, rename, and delete durable sessions. Switching a session re-subscribes the live
  trace (`/events?session_id=`) and reloads that session's persisted transcript into the trace
  viewer, so the runs list, live trace, and history all scope to whichever session is selected;
  the active session persists across reloads via `localStorage`. The implicit `"dashboard"`
  session remains the default, and it's the fallback if the active session is deleted.
- **Session transcript view** — the conversation is now a first-class **Transcript card** stacked
  above the Runs card; it's the default view, and a new turn no longer clobbers it (the run streams
  in the Runs card and the transcript refreshes when the turn completes). Click a run to drill into
  its tool-trace, click Transcript to return. The transcript renders as a **chat/messaging view**
  (your messages right, Argus's left, tool output as a muted note), with Argus's replies rendered as
  **Markdown** — bold, lists, tables, code, headings, links — sanitized with DOMPurify. Empty
  tool-call turns are hidden from the view.

## 0.7.5

### Added
- **`evolve_table` skill** — guidance so a small model *reaches for* the table-mutation tools when
  changing a table that already exists: `add_column` in place (not rebuild-and-port), `copy_table` for a
  bulk copy (not a `query` + `insert_row` loop), `rename_column`/`drop_column`/`rename_table` for schema
  changes, and `update_rows` for bulk value changes. Measured A/B on the 3B model (pass^k, Opus quality
  judge): chain-correctness 4/6 → 5/6 and judge 1.83 → 2.61 (Δ+0.78), driven mainly by the add-a-column
  case (0/3 → 3/3), with no over-fire on off-target prompts.

## 0.7.4

### Added
- **Table-mutation tools** — six new validated table tools so in-place schema changes and bulk data
  moves no longer degenerate into hundreds of one-row `insert_row` calls (which could overflow the
  step budget and crash the turn):
  - **`add_column`** — add a column (`name:type`, e.g. `sleep_start:text`) to an existing table
    without recreating it; existing rows get `NULL` there.
  - **`rename_column`**, **`drop_column`**, **`rename_table`** — the rest of the ALTER family.
  - **`copy_table`** — copy rows from one table into another in a single call: it creates the
    destination (mirroring the source's columns, types, and primary key) when it doesn't exist, or
    copies the shared columns into an existing one; an optional `where` filter copies just a subset.
    Whole-table copies run as one server-side statement (no row cap); filtered copies run the filter
    on the read-only connection and then parameterized-insert the results, so no SQL fragment ever
    touches a write path.
  - **`update_rows`** — set columns on every row matching an equality filter, fully parameterized;
    an empty match (which would rewrite the whole table) is refused.
  The four destructive operations (`drop_column`, `rename_column`, `rename_table`, `update_rows`)
  default to **Ask** in the per-tool approval matrix; `add_column`/`copy_table` default to Allow.

## 0.7.3

Internal/testbed release — a new developer instrument, no user-facing behavior change.

### Added
- **Model-capability benchmark** (`python -m engine.eval.benchmark`) — a committed, reproducible
  instrument that runs a frozen, difficulty-graded task battery per model under the standard config,
  scores each task (deterministic tool-chain + a model-graded quality judge), accumulates labeled JSON
  results, and plots a per-tier metric-vs-model-size curve — for measuring how well Argus performs as
  the driving model shrinks (finding the small-model "capability shelf"). The founding run (a ~35B vs a
  3B) shows the shelf is difficulty-dependent: small models hold on trivial single-tool tasks and fall
  off on structured/multi-step ones.

## 0.7.2

### Added
- **Update-available indicator** — the dashboard now checks whether a newer release is published on
  GitHub and shows an "↑ vX.Y.Z" badge next to the version in the footer (backed by a `/updates`
  endpoint; cached, and it degrades silently if GitHub is unreachable).
- **Clarification choice buttons** — when the agent asks a clarifying question with options
  (`ask_user` with `options`), the dashboard renders them as one-tap buttons; clicking one sends it as
  your next message instead of making you type it.

### Changed
- **Install scripts pin to the latest release** — `install.sh` / `install.ps1` now check out the latest
  release tag after cloning, rather than landing on the moving `main` branch, so a fresh install is a
  stable versioned release.

## 0.7.1

### Changed
- **`design_table` skill acts instead of interrogating** — on a clear-enough request ("track my daily
  coffee in a table") it now infers a sensible schema and builds the table, rather than stopping to ask
  the user how to structure it; a focused clarifying question is reserved for genuinely ambiguous
  requests. (Measured: the previously over-asking cases now build a well-designed table, with schema
  quality held.)

### Added
- **`native_finish` on the dashboard tool-calling-mode toggle** — the mode is now selectable in the UI
  (`native` / `manual` / `finish`), not just via `.env`/API.

## 0.7.0

### Added
- **`native_finish` tool-calling mode** (opt-in, `TOOL_CALLING_MODE=native_finish`) — native
  tool-calling with `tool_choice=required` plus a synthetic `final_answer` tool, so the model must emit
  a structured tool-or-finish decision every turn. This makes plain-prose "slips" impossible and lets a
  guided-decoding backend (vLLM) produce valid tool-call JSON, while keeping server-side parsing. A
  third option alongside `native` (default) and `manual`; `chat()` now accepts a `tool_choice` param
  (defaults to `auto`, unchanged for the other modes).

## 0.6.1

Internal/testbed release — no user-facing behavior change.

### Added
- **Model-graded judge** (`engine/eval/judge.py`) for the skill-eval harness — a pure prompt-builder +
  reply-parser that scores a run's output QUALITY (0–3) against per-case rubric criteria, complementing
  the deterministic chain-scorer (which can't see, e.g., a correct clarifying question or a
  chain-passing-but-low-quality result). Judge-model-agnostic; the developer runner grades on-target
  cases via the local model or `claude -p` (Opus). Unit-tested and blind to arm/skill.

## 0.6.0

Internal/testbed release — no user-facing behavior change.

### Changed
- **Full store isolation via `data_dir`** — every persistent store (tables, memory, knowledge,
  workspace, artifacts, created tools/skills, watches, routines, scheduled jobs, model presets, …) now
  resolves through the engine's `data_dir` argument instead of hardcoding the project root. Production
  is byte-identical (the default is the project root); passing a `data_dir` isolates an entire Engine
  in-process. Fixes latent test pollution and is the foundation for the skill-eval harness.

### Added
- **Deterministic skill-eval scorer** (`engine/eval/scoring.py`) — a pure, chain-based scorer
  (`tools_in_order` / `min_counts` / `activates` / `skill_not` / `schema_has`) used by the internal
  `pass^k` A/B harness that validates skills across models. (The harness runner itself is developer
  tooling and ships outside the package.)

## 0.5.0

### Added
- **Structured-data skills** — two skills that teach a small model to work with tables well, the first
  of the skills-led push (skills as the guidance layer that gets more out of the existing tools):
  - **`design_table`** — designs a sound schema *before* creating a table: real column types (not
    all-text), a `json` column for list/nested fields (ingredients, tags, line-items), the embed-vs-
    split judgment, and a primary key for natural ids. Fixes the small-model default of flat, all-`TEXT`
    tables with lists buried in text blobs.
  - **`extract_to_table`** — pulls records out of a document, file, or pasted text into a queryable
    table: reads the source with the right tool (`read_document` incl. OCR, `read_file`, or
    `download_file` for a document URL), designs a typed schema, then inserts one row per record —
    instead of returning the content as prose or one giant text column.
- **`json` / `list` column-type alias** for `create_table` — a list or nested field can be declared
  `field:json` (stored as JSON text, queryable with `json_extract`), so the schema self-documents the
  intent. Additive: existing schemas are unaffected.

## 0.4.0

### Added
- **Interactive blocking approvals** — sensitive agent actions now *pause the turn and wait* for a
  human decision instead of proceeding unattended. Each gated action has a visible per-action policy
  (**Allow / Ask / Deny**) you set from the Developer page, and when a policy is *Ask* the action
  blocks for a configurable window (`APPROVAL_WINDOW_SECONDS`, default 60): decide in time and the
  same turn resumes seamlessly; miss the window and it becomes a pending item you can approve later
  (which resumes the work). Prompts appear as inline **Approve / Deny** buttons in the dashboard live
  trace or as Telegram inline buttons, on whichever channel started the turn. **Every tool** has an
  Allow / Ask / Deny toggle on the Developer page — most default Allow and run exactly as before,
  while the sensitive ones (dependency installs, SOUL edits, `exec_python`, `forget`, `delete_row`)
  default Ask. Enforcement is a single check in the loop before any tool runs: Allow runs it, Deny
  refuses it (and Argus adapts), Ask pauses for your decision. Gated by `ENABLE_INTERACTIVE_APPROVALS`
  (on by default); off restores the previous record-and-continue behavior exactly.
- **More calculator functions** — `calculator` now supports `sqrt`, `cbrt`, `pow`, `abs`, `round`,
  `min`, `max`, `floor`, `ceil`, `trunc`, `exp`, `log`/`log2`/`log10`, the trig functions, `hypot`,
  `degrees`/`radians`, and the constants `pi`, `e`, `tau` — still evaluated through the safe
  AST whitelist (no `eval`), with the same runaway-exponent guard applied to `pow`.

### Added
- **Standing behavioral rules** — a durable, owner-managed set of "how to behave" directives
  ("always confirm before deleting", "never use emoji") that persist across sessions. Enabled rules
  are injected into every turn as a distinct "Standing instructions from your owner" block (separate
  from factual memory and from persona/SOUL). Rules can be captured three ways: the agent auto-drafts
  them from owner corrections (a background, cue-gated aux-model pass — "don't do that again" survives
  the session and the owner is notified with an undo hint), the model saves them explicitly
  (`save_rule` / `list_rules` / `remove_rule` tools), or they're managed on a new dashboard **Rules**
  page (add / enable-disable / delete, admin-gated). Backed by a small `rules.json` state file.
  Gated by `ENABLE_RULES` (on) and `ENABLE_RULES_AUTODETECT` (on).

## 0.2.1

### Fixed
- **Reliability metric honesty** — the Reliability page counted a tool call as a success whenever it
  didn't raise, but most tools catch their own errors and *return* an error string (so `ok` stays
  true). Tools that returned `"Error fetching…"` every call were scored 100%. Error-shaped results
  (`Error…` / `Traceback` / `looks WRONG` / fetch+parse errors) are now counted as failures; honest
  `no-data` / `CANNOT` outcomes still count as successes.
- Dashboard: the delete (`✕`) button on created-tool and created-skill rows was dropping to its own
  line; it's now inline on the right of each row.

## 0.2.0

### Added
- **Reliability harness** — a passive, always-on instrument that records tool, routine, and
  loop-health outcomes from the existing event stream into a dedicated `reliability.db`, surfaced on
  a new dashboard **Reliability** page: a top-line tool-success score, a worst-first per-tool table
  (success %, latency, sparkline, last-error drill-down), routine completion, and a loop-health strip
  (parse-failure / reprompt / validation-failure rates). Costs no model calls — it only observes.
  Gated by `ENABLE_RELIABILITY` (on by default).

### Fixed
- Scheduling tools (`list_scheduled_tasks`, `cancel_scheduled_task`, `update_scheduled_task`) are now
  **owner-wide** instead of session-scoped — a task created from Telegram is visible and manageable
  from the dashboard and vice-versa (Argus is single-user with global identity). Jobs still remember
  their origin session for delivery.
- `GET /version` (and the FastAPI app version) now derive from `pyproject.toml` via a single source,
  so the reported version can no longer drift from the package version.

## 0.1.0

Initial public release.

- Agent loop with native and manual tool-calling modes (A/B configurable via `TOOL_CALLING_MODE`).
- Live trace dashboard (the "Observatory") with Console, Automation, Data, Memory, Developer, and
  Settings views.
- Built-in tool library: calculator, unit/currency conversion, weather, geocoding, dictionary,
  Wikipedia, crypto price, time tools, web search (SearXNG) and page fetch/crawl (Firecrawl).
- Agent-created tools (`create_tool`) and a code interpreter (`exec_python`) behind a soft,
  language-level sandbox (AST-gated restricted exec + SSRF egress guard), gated by feature flags
  and off by default.
- Approval-gated dependency installs for created tools, and an opt-in trusted-tool tier for
  human-approved unsandboxed code.
- Skills system: markdown-defined procedural knowledge on top of tools, with an optional
  deterministic `steps` block executed by the routines engine instead of free-form generation.
- Structured data: a SQL-backed table store with a safe read-only query/aggregate surface, plus
  `ask_data` (natural-language question -> SQL -> answer, with schema grounding and self-repair).
- Persistent memory with keyword and semantic (embedding-based) recall, auto-extraction, and
  configurable global/session scoping.
- Routines and a task scheduler for recurring or timed multi-step jobs.
- URL/feed watches with change alerts.
- Knowledge base (RAG) via `add_to_knowledge` / `search_knowledge` over an embedded chunk store.
- Document reader for PDF/DOCX/XLSX, including OCR for scanned PDFs.
- Charts (PNG/SVG) and dependency-free ASCII charts for inline rendering.
- Artifacts (self-contained HTML pages) and PDF export (WeasyPrint) built by the agent.
- Outbound notifications to the owner via Telegram, email (SMTP), and push (ntfy).
- Multi-model roles: separate model connections for chat vs. embeddings; works against OpenRouter,
  OpenAI-compatible APIs, or a local vLLM/Ollama server.
- `argus` CLI: `start`, `stop`, `restart`, `status`, `logs`, `run`, `version`.
