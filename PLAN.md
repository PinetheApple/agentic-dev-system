# PLAN — Build order for the minimal ADS core

> Companion to `SPEC.md`. Draft for user sign-off (wayfinder ticket #001).
> The core is built **from scratch** on a new branch of this repo; git history
> and the current README/`ads/` stay as raw spec material, not preserved code.

## Build order

The route the wayfinder map already charts — each step is a ticket:

1. **#001 (this doc)** — distill `SPEC.md` + `PLAN.md`. *Root; blocks all.*
2. **Contracts (parallel after #001)** — grill each to a settled contract:
   - **#002** core phase-spine: `state.json` schema, phase transitions, per-phase
     JSON contract, `events.jsonl` taxonomy, structured handoff, bitter-lesson split.
   - **#003** task data model: DAG + disjoint-`owns`, serial ready-set (the seam).
   - **#004** validation contract: `cmd` exit-code + cold-critic
     `{pass, evidence, cited_paths}`, forgery-proof auto-fail.
   - **#005** observability: `loop_fmt`-style live feed + pinned footer
     (incl. token/budget burn) + `jsonl` tee.
   - **#006** user-in-the-loop: CLI verbs, gate placement, sign-off mechanic,
     gap→decide-or-stop branching.
   - **#007** adapter Protocol: `run()` boundary, claude-code + stub, model-per-role.
3. **#008 — build green on stub.** Assemble the spine (#002) over the task model
   (#003), validation gate (#004), live feed (#005), user verbs (#006), against
   the stub adapter (#007). Full stdlib `unittest` suite runs the loop end-to-end
   token-free. Pyright-strict, ruff clean.
4. **#009 — prove on Claude Code.** Swap stub → claude-code adapter; drive one
   real task end-to-end: ADS's own **curses TUI** (reads `state.json`, mutates
   nothing — a botched run can't corrupt the loop). User declares done.
   *Resolving this = destination reached.*

Contracts #002–#007 are independent and parallelizable once #001 lands; #008
gates on all six; #009 gates on #008.

## Deferred-feature backlog

The green spine self-hosts these; each graduates from the map's **Not yet
specified** once the spine proves out (order is indicative, not fixed):

1. Net-sandbox / firewall containment posture.
2. Read-only parallelization (first thing off the disjoint-`owns` seam), then
   real multi-task concurrent execution.
3. Interactive curses TUI beyond the maiden task.
4. Async control verbs (pause/resume/redirect/edit/replan/abort).
5. Escalation flow.
6. Resume/reconcile hardening (merge tripwires, resumptive re-split).
7. OpenCode second adapter (proves harness-agnostic claim).
8. Behavioural "user-testing" validator.

## Salvage-vs-discard from old `ads/`

Decided per-module as the contracts (#002–#007) fix what the new core needs
(currently fog in **Not yet specified**). Default posture: mine the *schemas and
contracts* (state fields, task frontmatter, verdict shape, `run()` Protocol),
discard the *structure* (escalation/control/sandbox/reconcile/resplit modules
are deferred features, not core).
