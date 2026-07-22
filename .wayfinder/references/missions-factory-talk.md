# Reference — "The multi-agent architecture that actually ships" (Factory / Missions)

Talk by Luke (Factory; ex-goose/Block). External, production-proven corroboration of
this map's shape. Cited by tickets #002–#007. Load when resolving a contract ticket.

## Relevance to this map

- **Three-role architecture** = orchestrator (plan) / worker (implement, clean context,
  commit) / validator (verify). Mirrors our intake→plan→execute→validate spine.
- **Validation contract authored at plan time, before any code** — defines correctness
  independently of implementation; each feature assigned assertions it must satisfy; the
  union of features must cover every assertion. → anchors #004 + #006 (done is
  contract-satisfied + user sign-off, not agent self-report).
- **Two validators**: *scrutiny* (tests/typecheck/lint + per-feature code-review agents)
  and *user-testing* (spawns the app, computer-use, behavioural end-to-end). Neither has
  seen the code — adversarial by design. Scrutiny ≈ our #004; user-testing = fog (defer).
- **Serial execution beats parallel** for software tasks — parallel agents conflict,
  duplicate, make inconsistent architectural calls; coordination overhead eats the gains.
  Serial with *read-only* internal parallelization (search, research, code-review).
  Error rate drops; correctness compounds over multi-day runs. → backs #003 deferring
  real parallelism while keeping the disjoint-`owns` seam.
- **Structured handoffs** — worker writes what's done/undone, commands run + exit codes,
  issues discovered, procedures followed. Self-heal at milestone boundaries; progress
  blocks until handoff issues are addressed. → enriches #002 (event/handoff taxonomy).
- **Mission control** — monitoring view: % complete, **budget/tokens burned**, active
  worker, handoff summaries. → enriches #005 (footer) and the deferred TUI #009.
- **Right model per role / model-agnostic** — planning=careful reasoning,
  implementation=fast fluency, validation=precise instruction-following; validator may
  use a *different provider* to avoid shared-training-data bias. → #007 tiering + #004
  validator independence.
- **Bitter lesson** — orchestration logic lives in prompts/skills (~700 lines), only
  thin deterministic bookkeeping is hard-coded (running validation, blocking on
  unaddressed handoffs), so the system improves with each model release. → open fork for
  #002: how much of the spine is deterministic Python vs prompt/skill-driven.

## Full transcript

Chapter 1 — Bottleneck is human attention, not intelligence. Best engineers drive only a
few tasks/day because every task needs their attention, every commit their review.
Models can figure out 50 tasks; there isn't bandwidth to supervise them. Premise: human
decides *what* to build, the system figures out *how*; an agent works for hours/days and
you return to finished work.

Chapter 2 — Taxonomy of five multi-agent frameworks: **delegation** (parent spawns child,
gets a result), **creator-verifier** (separate fresh-context agent checks the work; the
author has cost bias, a fresh agent finds issues — like human code review),
**direct communication** (agents DM without a coordinator — hard, state fragments, no
single source of truth), **negotiation** (agents coordinate over a shared resource; best
when net-positive win-win, not adversarial), **broadcast** (one-to-many: status updates,
shared constraints — critical for coherence over long tasks).

Chapter 3 — **Missions** combines delegation + creator-verifier + broadcast + negotiation.
Describe a goal, scope it through conversation, approve a plan, system executes for
hours/days. Not a single session — an ecosystem communicating through structured handoffs
+ shared state. Three roles: **orchestrator** (planning/sounding-board, asks strategic
questions, produces plan with features, milestones, and a **validation contract** defining
what "done" means before any code); **workers** (implementation, clean context, read spec,
implement, commit via git so the next worker inherits a clean slate); **validators**
(verification — lint/typecheck/tests/code-review *and* behaviour end-to-end).

Chapter 4 — Validation contracts. Tests written after implementation confirm decisions,
don't catch bugs — systems that validate that way drift. The contract is written during
planning, before code, defining correctness independently of implementation (hundreds of
assertions for a complex project; each feature assigned assertions). Two validators run
after each milestone: **scrutiny** (test suite, typecheck, lint, dedicated code-review
agents per feature) and **user-testing** (acts like QA — spawns the app, computer-use,
fills forms, checks pages render, clicks buttons, verifies functional flows). Most
wall-clock time is spent in user-testing waiting on real execution, not generating
tokens. Neither validator has seen the code — adversarial by design.

Chapter 5 — Structured handoffs. A finished worker fills a structured handoff: what
completed, what left undone, commands run + exit codes, issues discovered, whether it
followed the orchestrator's procedures. Errors caught at milestone boundaries, corrective
work scoped, mission pulls itself back on track by forcing agents to write it down and
address it. Longest mission: 16 days; believe 30 possible.

Chapter 6 — Serial over parallel. 10 parallel agents = 10x throughput in theory, but in
software they conflict, duplicate work, make inconsistent architectural decisions;
coordination overhead eats the gains while burning tokens. Missions run features
serially — one worker or validator at a time — with parallelization only on read-only
ops (codebase search, API research, code review). Serial with targeted internal
parallelization: slower on paper, but error rate drops dramatically and correctness
compounds over multi-day runs.

Chapter 7 — Mission control. A chat interface doesn't work for multi-day runs. Need at a
glance: how much of the project is complete, how much of the budget is burned. Dedicated
view shows active worker, handoff summaries, course corrections — run missions async,
plug in as a PM or go do something else.

Chapter 8 — Right model per role ("droid whispering"). Planning = slow careful reasoning;
implementation = fast code fluency/creativity; validation = precise instruction
following. No single model/provider is best at all three. Model-agnostic architecture is
a structural advantage — you're only as strong as your weakest link; locked to one
provider, you're constrained by that family's weakest capability. Validation may use a
different provider to avoid shared-training-data bias. The structure also compensates for
weaker/open-weight models via contracts + milestone checkpoints.

Chapter 9 — Production data (Slack clone): 60% of time and tokens on implementation.
Validation almost never succeeds first go — follow-up features nearly always created
(the QA loop's value). End state: ~50% of LOC is tests, ~90% coverage. Heavy prompt
caching offsets long-run cost.

Chapter 10 — Bitter lesson. Fear: next model release makes your architecture obsolete.
Missions is built to get *better* with every model improvement: almost all orchestration
logic is in prompts/skills (~700 lines of text; four sentences can change execution
strategy) not a hard-coded state machine. Worker behaviour driven by orchestrator-defined
skills per mission. Only deterministic logic is thin bookkeeping — running validation,
blocking progress on unaddressed handoff issues. Missions supplies discipline; models
supply intelligence, using primitives they already know (AGENTS.md, skills).

Chapter 11 — Economics shift: a team of five that drove ~10 workstreams can drive ~30;
humans focus on architecture/product, not execution. Codebase ends *cleaner* than it
started (e2e + unit tests, skills, structure). Missions = composition of the five
strategies + connective tissue (structured handoffs, right model per role, an
architecture that improves with each model). Open questions: further parallelizing
missions; orchestrating missions into larger workflows.
