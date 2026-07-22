<!-- labels: wayfinder:task, wayfinder:closed -->
# Distill the canonical SPEC.md + PLAN.md for the rescoped core

Assignee: PinetheApple
Blocked by: _(none — frontier root)_
Status: closed

## Question

What is the canonical `SPEC.md` (what the minimal core *is* — the phase spine, the
invariants, the user-ownership + done-by-user contract) and `PLAN.md` (build order +
the deferred-feature backlog), distilled from the README, the commit/ticket trail
(tickets 002/010/011…), and the existing `ads/` code — such that the from-scratch core
can be built purely off that doc?

This is the root: it blocks every contract ticket. HITL — the agent drafts, but the
**user owns and signs off** the SPEC (per the standing preference). Mine old `ads/` for
detail; do not preserve its structure. Keep it minimal — describe the spine and name the
deferred backlog as fog, don't re-specify the whole overbuilt system.

## Resolution

`SPEC.md` + `PLAN.md` written to repo root on new branch **`minimal-core`**
(user-signed-off). Kept minimal — the *frame* only, not frozen schemas.

**SPEC.md:** the core is a stateless-per-iteration harness-agnostic loop; spine
`intake → plan → review → execute(one task) → validate → done(user sign-off)`;
8 invariants (stateless-per-iter, append-only `events.jsonl` audit, resume-safe,
forgery-proof cold-critic validation, parallelism-seam-kept/executor-serial,
user-owns-ends, observability-is-core, bitter-lesson split); user-in-loop
contract (scope up front, done user-declared, gap→decide-or-stop); adapter
boundary (claude-code + stub, model-per-role); non-goals deferred as fog.
Exact schemas left to #002–#007 to sharpen; old `ads/` cited as raw material.

**Two user decisions recorded:**
1. **Review gate is conditional** — **one** gate (approve plan) for no-design
   work; **two** (approve spec, then design; spec frozen after approval) for
   design work (new app/website). Plan phase picks by whether it emits a design
   artifact. (Collapsed the old always-two-stage review.)
2. **Docs live on the new `minimal-core` branch**, not `main`. `main`'s
   history + README + `ads/` stay as raw spec material.

Assets: `SPEC.md`, `PLAN.md` (branch `minimal-core`).
