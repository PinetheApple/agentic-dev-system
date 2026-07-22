<!-- labels: wayfinder:map -->
# Map — Minimal ADS core loop that self-hosts

## Destination

A **from-scratch minimal ADS core loop**, driven off a preserved `SPEC.md` + `PLAN.md`,
that runs **one real task end-to-end** with **basic observability at the core** — and
whose proving ground is the loop **building out the rest of ADS itself** (curses TUI,
sandbox, further features). Scope is agreed with the user up front; **"done" is declared
by the user**. Every session orients here before choosing a ticket.

## Notes

- **Domain:** agentic dev-loop / "structured Ralph" — stateless-per-iteration control
  loop that reconstructs state from disk, runs a task, validates, persists. The mature
  reference is `music_app`'s `project-loop` skill + `scripts/run-loop.sh` +
  `scripts/loop_fmt.py`; the overbuilt prior generalization is this repo's existing
  `ads/` (5.2k LOC, never landed one real task — mine it for contract detail, discard
  the rest).
- **This effort carries execution into the map** (overrides Wayfinder's plan-don't-do
  default): the destination is a *working* loop, not a spec to hand off, so build/prove
  tickets (#008, #009) are in scope.
- **Standing preferences:** scope agreed up front; done declared by user; SPEC is
  user-owned (never silently edited — unambiguous gaps → decide + record, ambiguous →
  stop-point). Clean-code / SOLID / KISS / YAGNI, files ≤400–500 lines, minimal
  comments, zero-runtime-dep stdlib Python, pyright-strict + ruff, `uv`/`unittest`.
- **Skills to consult:** `/grilling` + `/domain-modeling` for contract tickets;
  `/prototype` where a state model needs a cheap concrete artifact; `python-expert`.
- **Key reference:** `.wayfinder/references/missions-factory-talk.md` — Factory's
  "Missions", external production-proven corroboration of this shape (three-role
  orchestrator/worker/validator, plan-time validation contract, serial-over-parallel,
  structured handoffs, model-per-role, bitter-lesson prompts-not-state-machine). Load it
  when resolving any contract ticket (#002–#007).
- **Settled while charting (frame every ticket honors):** rescope from scratch, same
  repo + new branch, keep git history + README as raw spec material; single spine
  intake→plan→execute(one task)→validate→user-sign-off; single adapter (Claude Code +
  stub); keep the task DAG + disjoint-`owns` model in the data layer even though the
  executor is serial (it's the seam parallelism graduates through); observability =
  `loop_fmt`-style live terminal feed + pinned status footer + tee to `events.jsonl`;
  first self-hosted task is the curses TUI.

## Decisions so far

<!-- index — one line per closed ticket, then zoom to the ticket for detail -->

- [Distill the canonical SPEC.md + PLAN.md for the rescoped core](tickets/001-distill-canonical-spec-plan.md) — `SPEC.md`+`PLAN.md` written on new branch `minimal-core` (user-signed-off); the frame (spine, 8 invariants, user-in-loop + adapter contracts) with exact schemas left to #002–#007; review gate is **conditional** — one for no-design work, two (spec→design) for design work.

## Not yet specified

<!-- in-scope fog: graduates to tickets as the frontier advances -->

- **Deferred-feature backlog** the green spine self-hosts (each graduates once the spine
  proves out): net-sandbox / firewall containment posture; real multi-task parallelism
  (concurrent executor + scheduling on the disjoint-`owns` seam); interactive curses TUI
  beyond the maiden task; async control verbs (pause/resume/redirect/edit/replan/abort);
  escalation flow; resume/reconcile hardening after crash.
- **OpenCode (2nd adapter)** — proves the harness-agnostic claim; graduates after the
  Claude-Code spine is green.
- **How much of old `ads/` is salvaged vs discarded** per module — sharpens once the
  contracts (#002–#007) fix what the new core needs.
- **Behavioural / "user-testing" validator** (spawn the app, computer-use, verify
  functional flows end-to-end — Missions' second validator) — deferred; also gated by
  the no-auto-UI-verify preference (browser/visual verification only on request).
- **Bitter-lesson posture** — how far orchestration lives in prompts/skills vs a
  hard-coded Python state machine. Seeded as a sub-question in #002; graduates to its own
  ticket if the SPEC (#001) leaves it open rather than settling it.

## Out of scope

<!-- ruled beyond this destination; never graduates -->

- Distribution / packaging polish (making ADS an installable product).
- Changing `music_app`'s own `project-loop` — it's the reference, a separate effort.
