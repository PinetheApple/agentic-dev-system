<!-- labels: wayfinder:grilling, wayfinder:closed -->
# Task data model: DAG + disjoint-owns, serial ready-set (the parallelism seam)

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

What is the task file format (frontmatter: `owns` paths, `deps`, gates) and the ready-set
computation — kept in the data layer now even though the executor runs serially — so that
real parallelism graduates later by swapping only the executor, not the model? What's the
acyclicity check and the disjoint-`owns` pairwise rule?

The point of keeping this in the minimal core: it's the seam. Grill the shape so serial-
now / parallel-later costs nothing at graduation. Factory's Missions (see the reference)
is production evidence *for* serial-now: parallel agents conflict / duplicate / make
inconsistent architectural calls, and coordination overhead eats the gains — they run
features serially with parallelization only on *read-only* ops (search, research, review).
Design the seam so that read-only parallelization is the first thing that graduates.
Parallelizable after #001.

## Resolution

Settled by `/grilling`. The task data model is a pruned, sharpened take on the old
`ads/tasks.py` precedent — same seam shape, cut down to what the minimal spine needs.

**Task file — one `<id>.md` per task, frontmatter + body.** Core frontmatter is exactly:

```
id            — repo-unique task id
status        — pending → active → done | failed   (failed terminal, blocks dependents)
depends_on    — list of task ids (the DAG edges)
owns          — list of repo-relative file-or-dir paths this task may write
exit_criteria — inline validation contract (shape/semantics owned by #004)
```

Dropped from old `ads/` (each rode in on a deferred feature, graduates with it):
`parent` (resplit), `critical` (escalation), `expert` (prompt-orchestration / #002 split),
`tier` (model-per-role → adapter Protocol #007). Status enum cut from seven to four.

**`exit_criteria` lives inline on the task record** (decision A): "a task and its
definition of done" is one on-disk file, serving resume-after-crash (one file fully
reconstructs a task). #003 owns the field's presence; #004 owns what a criterion means.

**`owns` semantics (decision C):** entries are repo-relative paths, file *or* directory.
Two entries **overlap iff one is a segment-aware prefix of the other** — `src/tui` collides
with `src/tui/app.py`, does **not** false-match `src/tui-old`. Chosen over exact-match
(old `ads/`) because exact-match's failure mode is a *missed* conflict — two tasks stomping
the same subtree looking parallel-safe — which is exactly what this seam exists to prevent.
Globs rejected as YAGNI (glob-vs-glob disjointness has no clean containment test).

**The seam (decision E):** ship `ready_batch(tasks) -> list[Task]` now — the full
disjoint-`owns` computation — even though serial execution never needs the batching. The
serial executor consumes `ready_batch(...)[0]` and **recomputes each iteration**
(stateless-per-iteration applied literally; costs nothing serial). `ready_batch` = pending
tasks whose deps are all `done`, greedily filtered so no two share overlapping `owns`.
Graduation to real parallelism is then a pure executor swap — `for t in ready_batch(...):
spawn(t)` — with the parallel-safety logic already living in the data layer and exercised
by the stub suite from day one. This is what makes invariant #5 true rather than
aspirational. (Chosen over a `next_ready()` single-task API that would defer the hard part.)

**Read-only parallelization graduates first, implicitly (decision F):** a read-only task
declares `owns: []`. Empty owns intersects nothing → always mutually disjoint and disjoint
from every writer → the parallel executor co-schedules them for free, with **no new field**.
No `read_only` flag (it would be a second, contradictable conflict channel). The model
stays a pure algebra — *empty owns = no conflict, full stop*. Enforcement that a task
actually stayed inside its declared `owns` is pushed to **#004**: the cold critic already
sees the on-disk diff vs declared `owns`, so "wrote outside its lane" is a forgery-proof
validation failure where the evidence lives — not a #003 data-model rule.

**Acyclicity check (decision G):** 3-color DFS (WHITE/GRAY/BLACK), catching both cycles
(with the cycle path) and `depends_on` referencing an unknown task id in one pass — kept
verbatim from old `ads/`. Runs **once as the plan-commit gate**, after the plan phase
returns `{spec, design, tasks}` and *before* any task file is written; a cyclic/dangling
plan is rejected before it ever lands on disk. Execute-time trusts the graph is a DAG (no
per-iteration re-check — mid-run hand-editing is deferred resume/reconcile fog).
`ready_batch`'s unknown-dep guard still fails loudly if a file is corrupted mid-run.

**Hand-offs to sibling tickets (not new tickets — cross-refs to open ones):**
- #004: (a) `exit_criteria` criterion semantics + verdict shape; (b) "task stayed inside
  its declared `owns`" as a forgery-proof check against the on-disk diff.
- #007: `tier` / model-per-role selection (deliberately *not* a task field).

Assets: this resolution; raw material `ads/tasks.py` (`Task`, `ready_batch`, DFS cycle
check), `ads/task_io.py` (per-`<id>.md` load/write).
