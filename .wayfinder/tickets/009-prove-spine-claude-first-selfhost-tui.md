<!-- labels: wayfinder:task, wayfinder:closed -->
# Prove the spine end-to-end on Claude Code — first self-hosted task: the curses TUI

Assignee: PinetheApple (Claude Code session)
Blocked by: #008
Status: closed

## Question

Run the green spine for real: swap the stub for the Claude Code adapter and drive **one
real task end-to-end** — the first self-hosted feature, ADS's own **curses TUI** (reads
`state.json`, mutates nothing, so a botched run can't corrupt the loop). intake → plan →
execute → validate → **user declares done**.

This resolving = the destination reached. Answer records what the loop built, the
observed live-feed behaviour, where (if anywhere) it halted, and the user's done-call.
HITL — user signs off. Once green, the deferred backlog in **Not yet specified** starts
graduating (sandbox, parallelism, OpenCode…).

## Resolution

Spine driven end-to-end on the **real Claude Code adapter** (`adapter: "claude-code"`,
not stub) for the maiden self-hosted task — ADS's own read-only status TUI. Destination
reached: intake → plan → execute → validate → **user done-call**. Run
`.agent/runs/20260722-134638`, terminal `phase: done`.

**What the loop built** — `ads/tui.py`: an import-guarded `rich` live dashboard over
`state.json` + an `events.jsonl` tail. §7's token-free floor holds — pure helpers
(`read_snapshot`, `tail_events`, `progress`, `event_summary`, `render` fallback) are
importable and unit-tested with **`rich` not installed**, and `--help` works before
`rich` is ever touched. Read-only by construction: it mutates nothing, so a botched run
can't corrupt the loop. Ships beside the transport-only `ads/adapters/claude.py`
(subprocess envelope, stream-json parse, role→tier→model) and `ads/config.py` +
`.agent/config/` (harness-aware self-host seed).

**Observed live-feed behaviour** — the event log records the loop's real path, not a
happy-path stub: `01-tui` was attempted to the ceiling (seq 18 `halt` — "exceeded
validation attempts ceiling (3)"), the user approved past the block (seq 19
`gate_close`), the retry landed (seq 21 `task:done`), the cold critic passed (seq 22
`validate:verdict {pass: true}`), and the loop opened the signoff gate (seq 23
`gate_open {gate: signoff}`) and halted clean — exactly where HITL is meant to stop.

**Where it halted** — the `awaiting_signoff` gate, by design. Nowhere else.

**User's done-call** — signoff approved via `driver approve --at awaiting_signoff`:
seq 24 `done`, seq 25 `gate_close {decision: approve, gate: awaiting_signoff}`,
`phase: done`, halt cleared.

**Validation of the built feature** (canonical pyenv env — has `rich` 15.0.0): stdlib
`unittest` suite **48 green** (tui helpers, claude adapter, config); real one-frame
render witnessed against the live run (phase/tasks/events painted correctly); read-only
invariant proven — `md5sum` of `state.json` + `events.jsonl` identical before/after a
render.

SHA: `cb87643` on `origin/minimal-core`. Green — the **Not yet specified** backlog
(sandbox, parallelism, OpenCode…) now starts graduating.
