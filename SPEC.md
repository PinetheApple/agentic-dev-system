# SPEC — Minimal ADS core loop

> **Status:** draft for user sign-off (wayfinder ticket #001). The SPEC is
> user-owned. Nothing here is silently edited later — unambiguous gaps get
> decided and recorded; ambiguous ones stop for the user.
>
> **Scope of this doc:** the *frame* the from-scratch core is built off — the
> phase spine, the invariants, the user-ownership contract, and the deferred
> backlog. It deliberately does **not** freeze the exact on-disk schemas: those
> are sharpened by the contract tickets #002–#007, which this SPEC blocks. Where
> a schema is named below it is cited as **raw material** from the existing
> overbuilt `ads/` (mine for detail, discard the structure), not as settled.

## 1. What the core *is*

A stateless-per-iteration control loop ("structured Ralph"): each iteration
reconstructs all state from disk, advances exactly one phase, validates, and
persists atomically. The reference shape is `music_app`'s `project-loop`; the
existing `ads/` (6.1k LOC, never landed one real task) is contract raw material.

The core is **harness-agnostic**: the driver owns phase sequencing and state; a
swappable adapter owns in-phase reasoning. The minimal core wires exactly two
adapters — Claude Code (`claude -p`) and a `stub` (canned, token-free tests).

## 2. The phase spine

```
intake → plan → review(approve) → execute(one task) → validate → done(user sign-off)
                   ^___reject___|                         |
                                 execute ←──exit-criteria fail──|
```

**Review is one gate or two, by whether the work needs a design:**

- **No-design work** (a task or project with nothing to design) — **one** gate:
  approve the plan (spec + contract), then execute.
- **Design work** (a new app/website) — **two** gates: approve the spec, then
  approve the design, then execute. A design-stage reject never regenerates the
  already-approved spec (freeze-approved-upstream).

The plan phase decides which by whether it produces a design artifact. Exact
verbs and gate mechanics: ticket #006.

- **intake** — verbatim copy of user intent to disk. No LLM call.
- **plan** — one `run()` call returns `{spec, design, tasks}`. The driver writes
  the artifacts and checks the task graph is acyclic **before** committing
  anything. The plan carries the **validation contract** (per-task exit
  criteria) — correctness is defined here, before any code (Missions Ch.4).
- **review** — the user approves the plan before any code runs; one gate for
  no-design work, two (spec then design) for design work (see spine diagram).
  Reject bounces back to plan, bounded. Exact verbs and gate placement: #006.
- **execute** — serial. Compute the ready set (deps satisfied, pairwise-disjoint
  `owns`) and call `run()` per task. Re-run picks up only `pending` tasks — this
  is what makes resume-after-crash safe. Serial now; the disjoint-`owns` model
  stays in the data layer as the seam parallelism graduates through (#003).
- **validate** — forgery-proof gates, author-agnostic; no agent self-report
  counts as done (#004).
- **done** — declared by the **user**, not the loop (§4).

## 3. Invariants (the frame every contract ticket honors)

1. **Stateless per iteration.** One `state.json` is the *only* thing the loop
   reads to reconstruct itself. Every write is atomic (temp-file + `os.replace`).
   *(raw material: `ads/state.py` `State` dataclass, `save_state`.)*
2. **Append-only audit.** A write-only `events.jsonl` records every event; the
   loop never reads it back. Observability is built on this tee (#005).
3. **Resume-after-crash safe.** Re-running from any point recomputes the ready
   set and continues; only `pending` work is retried. No in-memory state
   survives an iteration.
4. **Forgery-proof validation.** A leaf is done only when its driver-run `cmd`
   criteria exit 0 **and** a cold critic — given only `spec.md` + the task's
   on-disk `owns` diff, never the author's self-summary — passes with non-empty
   `cited_paths`. `pass:true` + empty `cited_paths` is auto-failed structurally.
   *(raw material: `ads/validate.py` verdict schema; sharpened by #004.)*
5. **Parallelism seam kept, executor serial.** The task DAG + disjoint-`owns`
   ready-set live in the data layer even though execution is serial, so
   read-only parallelization graduates by swapping only the executor (#003).
6. **User owns the ends.** Scope is agreed up front; "done" is user-declared
   (§4). SPEC is user-owned and never silently edited.
7. **Observability is a core pillar, not deferred.** A live terminal feed
   (colored tag per event) + a pinned status footer + the `events.jsonl` tee
   ship with the core (#005). The interactive curses TUI is deferred (#009).
8. **Bitter-lesson posture.** Only thin deterministic bookkeeping is hard-coded
   (running validation, blocking on unaddressed handoffs, atomic writes);
   orchestration logic lives in prompts/skills so it improves per model release.
   The exact split is settled in #002.

## 4. User-in-the-loop contract

- **Scope up front.** The user approves a plan that carries the validation
  contract before any code is written (Missions Ch.4/6).
- **Done is user-declared.** A task is done when its contract is satisfied
  **and** the user explicitly signs off — never on agent self-report.
- **Gap handling.** An unambiguous gap in the SPEC is decided by the agent and
  recorded; an ambiguous one is a stop-point for the user.

Exact CLI verbs and gate mechanics: ticket #006.

## 5. Adapter boundary

The spine calls one `run()` Protocol; Claude Code and `stub` both satisfy it.
The Protocol lets a role name its own model/tier (planning=careful,
execution=fast, validation=precise, possibly a different provider) so
model-per-role graduates without touching the boundary. OpenCode is a deferred
second adapter that proves harness-agnosticism. Exact signature: ticket #007.
*(raw material: `ads/adapters/base.py` `Adapter` Protocol, `RunResult`.)*

## 6. Non-goals for the minimal core (deferred as fog)

Named here so the build stays minimal; each graduates from the map's **Not yet
specified** once the green spine proves out:

- Net-sandbox / firewall containment posture.
- Real multi-task parallelism (concurrent executor on the disjoint-`owns` seam).
- Interactive curses TUI beyond the maiden self-hosted task.
- Async control verbs (pause/resume/redirect/edit/replan/abort).
- Escalation flow (agents never self-grant privilege).
- Resume/reconcile hardening (worktree merge tripwires, resumptive re-split).
- OpenCode second adapter.
- Behavioural "user-testing" validator (spawn app, computer-use).

## 7. Constraints

Zero-runtime-dependency stdlib Python; pyright-strict + ruff clean; files
≤400–500 lines; minimal comments; `uv` for dev deps; stdlib `unittest` for the
token-free suite.

**One blessed exception (#005):** the observability feed may depend on `rich` for
its pinned live-ticking footer — a well-tested lib beats a hand-rolled TUI for a
core pillar (invariant #7). Per-line tags stay raw ANSI; everything else stays
stdlib-only.
