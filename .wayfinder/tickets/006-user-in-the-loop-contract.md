<!-- labels: wayfinder:grilling, wayfinder:closed -->
# User-in-the-loop contract: scope agreed up front, done declared by user

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

How does the user agree scope up front and declare "done"? What are the CLI verbs
(`start` / `approve` / `status` / `done` …), where do the review gate(s) sit in the
spine, and what's the sign-off mechanic that makes the user — not the loop — the one who
ends a task? How do unambiguous-gap (decide+record) vs ambiguous (stop-point) branch?

This is destination-defining (done is user-declared). Fold Missions' model (see the
reference): scope-agreed-up-front = the user **approves a plan that carries a validation
contract** (the assertions of #004) before any code; "done" = that contract satisfied
**and** the user's explicit sign-off — never agent self-report. Grill it. Parallelizable
after #001.

## Resolution

**Interaction model — blocking-drain.** `start` drives the loop in the foreground
(streaming the #005 live feed) until it reaches a gate, then **halts and exits**. Each
verb re-drains the loop to the next halt. Halts *are* the crash-safe re-entry boundaries
(stateless-per-iteration) — no daemon; async control verbs stay deferred fog (SPEC §6).

**Verbs (7):** `init`, `start "<task>"`, `approve [--at <gate>]`, `reject "<reason>"`,
`answer "<text>"`, `status [--json]`, `resume`. (`resume` = pure crash re-entry, clears no
gate. No `done`/`watch` verb: signoff folds into `approve`; the live feed streams inline
during drives, the deferred interactive TUI is #009.)

**Gate mechanic — state-driven, one `approve`.** The loop parks in a named halt-state in
`state.json`; `approve` clears **whatever gate the state names** (`status` shows which +
what's being approved). Optional `approve --at <gate>` must match the current halt-state
or the command refuses (stale-approve guard); bare `approve` clears whatever's parked.

**Halt-states and what drains each:**
- `awaiting_plan_approval` (no-design work — the single gate) → `approve` / `reject`.
- `awaiting_spec_approval` → `awaiting_design_approval` (design work — the two gates
  from #001; approved spec frozen, design-reject never regenerates it) → `approve` /
  `reject`.
- `awaiting_clarification` (ambiguous gap; agent's question carried as a field in
  `state.json` + emitted event) → `answer "<text>"` (supply the fact, bounces to plan
  with it recorded) / `approve` (proceed with the agent's stated provisional assumption).
- `awaiting_signoff` (full task-graph validate passed) → `approve` sets `done`;
  `reject "<reason>"` bounces back to execute/plan for more work.

**Sign-off teeth (the destination-defining rule).** The loop has **no** code path that
writes `done` on its own. `validate` passing (#004: cmd exit-0 + cold-critic `pass` with
non-empty `cited_paths`) transitions to `awaiting_signoff`, **never** to `done`. The
transition `awaiting_signoff → done` has exactly one trigger: a user `approve` at that
gate. One signoff gate after the whole graph validates (per-milestone signoff = deferred
fog). This is the structural enforcement of "done is user-declared, never agent
self-report."

**Gap branch (SPEC §4).** The **plan phase** classifies gap vs ambiguity
(prompt/skill-driven per the bitter-lesson posture, invariant 8 — deterministic layer
only records + halts). Unambiguous gap → agent decides, emits a `gap_decided` event
(decision + rationale) to `events.jsonl`, **does not halt**, flows on; the append-only
audit (invariant 2) is where self-made decisions stay inspectable. Ambiguous →
`awaiting_clarification` halt (above).

Feeds #002 (halt-states + clarification-question field in the `state.json` schema; the
`gap_decided`/gate events in the taxonomy) and #008 (the 7-verb CLI to build).
