<!-- labels: wayfinder:grilling, wayfinder:closed -->
# Core phase-spine contract: state machine, phase JSON, state.json schema, event taxonomy

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

What exactly is the minimal state machine — the phases intake→plan→execute(one
task)→validate→user-sign-off — expressed as: the `state.json` schema (the only thing the
loop reads to reconstruct itself), the phase-transition rules, the JSON contract each
phase returns, and the `events.jsonl` event taxonomy? What makes it stateless-per-
iteration and resume-after-crash safe with the fewest moving parts?

Two threads Missions (see the reference) forces into this ticket: (a) the **structured
handoff** a finished execute-phase writes — what's done/undone, commands run + exit
codes, issues discovered — and how the loop blocks on unaddressed handoff issues; (b) the
**bitter-lesson posture**: how much of the spine is thin deterministic bookkeeping
(running validation, blocking on handoffs, atomic state writes) vs orchestration logic
that lives in prompts/skills so it improves with each model release. State the split
explicitly — the old `ads/` leaned all-Python-state-machine.

Grill with `/grilling` + `/domain-modeling`. Independent of #003–#007 (parallelizable
after #001).

## Resolution

The minimal spine, settled across six threads. **Frame:** deterministic skeleton,
prompt-driven meat — the bitter-lesson line runs *between* the driver and the adapter.
The driver hard-codes only thin bookkeeping (transition table, shape-checks, atomic
writes, ready-set compute, `blocking:true`→halt, `attempts` ceiling); everything
reasoning-shaped (what the plan is, how to execute, self-unblocking, critic verdict)
lives in the adapter prompt/skill so it improves per model release. Phase *sequencing*
stays deterministic Python — a fixed contract, not intelligence — because prompt-driven
sequencing would break resume-after-crash (invariants 1, 3).

**1. `state.json` — 9 fields** (the only thing the loop reads to reconstruct itself;
atomic temp+`os.replace` write). Old `ads/state.py` carried 20; the other 11
(`escalations`, `approved_cmds`, `paused`, `control_cursor`, `replan_scope`, `attached`,
`current_activity`, dual retry/step counters) are deferred features, dropped.

```json
{
  "phase": "intake|plan|review|execute|validate|done",
  "review_stage": "spec|design|null",
  "gate": "null|blocked",
  "tasks":    { "<task-id>": "pending|running|done|failed" },
  "attempts": { "<task-id>": 0 },
  "cursor": "<task-id>|null",
  "halt_reason": "string|null",
  "adapter": "claude-code|stub",
  "updated_at": "iso8601Z"
}
```
`tasks` is the id→status map — the ready-set input *and* the resume anchor; task bodies
(deps/`owns`/criteria) live on disk per #003, not here. `attempts` is the single
budget-ceiling counter (a safety floor, not a feature): review-reject and validate-fail
both increment it; at ceiling the driver halts to `blocked` instead of looping forever.
`gate` carries only `blocked` in the green spine (`reconcile`/`escalation`/`paused` are
fog). `adapter` is persisted so approve/resume can't switch harness mid-run.

**2. Phase-transition table** (hard-coded; per-task interleaved validate):

```
intake  → plan                                       (after intent written to disk)
plan    → review                                     (acyclicity check passed)
plan    → [halt blocked]                             (cyclic / malformed graph)
review  → execute       (approve — no-design: 1 gate; design: 2nd approve, spec frozen)
review  → plan          (reject; bounded by attempts ceiling)
execute → validate      (task run() returned handoff)
validate→ execute       (fail: task→pending, attempts++; or next ready task, cursor++)
validate→ done          (all tasks done+validated)
validate→ [halt blocked](attempts ceiling hit)
done    → (terminal — user sign-off)
```
Ready-set drives the execute↔validate loop: after each validate, recompute ready set;
nonempty → next `execute`, empty+all-done → `done`. `review_stage` sequences the
two-gate design case `null→spec→design→execute`; no-design skips `null→(approve)→execute`.

**3. Per-phase `run()` contract — uniform envelope**, per-phase `payload`:

```json
{ "ok": true, "payload": { … }, "error": null }
```
One deterministic place for the driver to check adapter-level success/failure (timeout,
malformed output) before inspecting phase-specific `payload`.
- **plan** → `{ "spec": md, "design": md|null, "tasks": [ {id, deps, owns, criteria, goal} ] }`
  — `design:null` = no-design (1 gate); `tasks` carries the validation contract inline
  (Missions Ch.4, authored before code). Task-body schema is #003's; #002 fixes only the envelope.
- **execute** → the structured handoff (thread 4).
- **validate** → `{ pass, evidence, cited_paths }` — the cold-critic verdict; #004 sharpens, #002 fixes the envelope.
- **intake / review / done** → no `run()` call (deterministic bookkeeping or human).

**4. Structured handoff** (execute's `payload`; Missions Ch.5):

```json
{ "task_id": "…", "status": "complete|blocked",
  "commands": [{ "cmd": "…", "exit": 0 }],
  "undone": ["…informational…"],
  "issues": [{ "desc": "…", "blocking": true }] }
```
Driver rule (its one bit of execute-phase orchestration): **any `issues[].blocking==true`
→ halt to `gate:blocked`**, `halt_reason` = the issue descs, regardless of `status`.
`undone` is informational (surfaced in the feed, doesn't halt). **Self-unblocking lives
in the execute prompt** — the executor tries research / alternate approaches inside the
`run()` call (Missions' read-only internal parallelization) and only sets `blocking:true`
once genuinely exhausted; so `blocking:true` *means* "unresolvable by the executor
itself," and the driver's halt-to-user is thin and mechanical. Auto-self-heal (re-plan
from a handoff issue) is fog.

**5. `events.jsonl` taxonomy — open emit, documented core set.** `append_event(kind,
**payload)` accepts any `kind` (write-only, best-effort, never read back — asserting a
closed enum on an audit stream buys nothing and adds a failure mode where a logging typo
crashes the loop). The ~11 core kinds below are the *documentation* contract #005 styles
a colored tag against; unknown kinds get a default tag. Every event carries `ts` + `kind`.

| `kind` | emitted when | key payload |
|--------|-------------|-------------|
| `phase_enter` | driver advances phase | `phase`, `review_stage?` |
| `intent` | intake writes user intent | `text` |
| `plan_ready` | plan returns valid graph | `task_count`, `has_design` |
| `gate_open` | review/blocked halt reached | `gate`, `stage?`, `reason?` |
| `gate_close` | user approves/resumes | `gate`, `decision` |
| `task_start` | execute begins a task | `task_id` |
| `task_handoff` | execute returns handoff | `task_id`, `status`, `undone_n`, `issues_n` |
| `validate` | critic verdict recorded | `task_id`, `pass`, `cited_n` |
| `activity` | adapter `run()` begins/ends (heartbeat) | `label`, `state:start\|end`, `model` |
| `halt` | any blocked halt | `reason`, `gate` |
| `done` | user signs off | — |

The `activity` heartbeat is the observability seam #005 builds the pinned footer +
spinner on (#009 the TUI).

**Stateless / resume-safe with fewest moving parts:** state.json is the only read
surface; every write atomic; re-run recomputes the ready set from `tasks` status and
continues only `pending` work; no in-memory state survives an iteration.

**Bitter-lesson posture — settled here, does not graduate to its own ticket.** The map's
fog line seeding it as a possible standalone ticket is cleared: the split is fixed above
(deterministic driver / prompt adapter, per-phase).

Raw material mined: `ads/state.py` (`State`, `save_state`, `append_event`, `halt`).
Structure discarded (the 11 deferred fields).
