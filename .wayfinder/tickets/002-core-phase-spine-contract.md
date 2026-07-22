<!-- labels: wayfinder:grilling -->
# Core phase-spine contract: state machine, phase JSON, state.json schema, event taxonomy

Assignee: _(unassigned)_
Blocked by: #001

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
