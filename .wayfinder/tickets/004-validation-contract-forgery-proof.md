<!-- labels: wayfinder:grilling, wayfinder:closed -->
# Validation contract: cmd exit-code + cold-critic cited_paths, forgery-proof

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

What is the minimal forgery-proof validation gate: a driver-run `cmd` (real exit code is
the verdict) plus one **cold-critic** `judgment` run given only `spec.md` + the task's
on-disk `owns` diff (never the author's self-summary), returning `{pass, evidence,
cited_paths}` — with `pass:true` + empty `cited_paths` structurally auto-failed? What's
the exact verdict schema and the anti-rubber-stamp rule for the spine?

No agent self-report counts as done. Two folds from Missions (see the reference): (a) the
gate validates against a **validation contract authored at plan time** — assertions the
task must satisfy, defined before code, not tests shaped by the code afterward (interlocks
with #006, where the user agrees that contract up front); (b) the cold critic should run
on a **different model / provider** than the executor where the adapter allows it, so
validation isn't biased by shared training data (interlocks with #007 tiering). Grill the
contract. Parallelizable after #001.

## Resolution

The minimal forgery-proof validation gate: **two author-agnostic gates per leaf**,
a driver-run `cmd` and a cold-critic `judgment`, with structural anti-forgery.

**Contract shape** (authored at plan time — interlocks #006): per-task
`exit_criteria: list[ExitCriterion]`, each `{check: "cmd" | "judgment", value: str}`.
`cmd.value` = shell command; `judgment.value` = the NL assertion the cold critic
checks. **Every leaf must carry ≥1 `judgment` criterion** — plan-graph validation
rejects a leaf with none (makes SPEC §4's "AND a cold critic" non-optional). `cmd`
criteria are optional (some tasks have no runnable check). Verbatim from old
`ads/tasks.py`'s `ExitCriterion` model.

**Gate 1 — `cmd`:** driver runs `subprocess.run(value, cwd=repo)`; the real exit
code *is* the verdict (0 = pass). Timeout 300s (carried from old `CMD_TIMEOUT_SECONDS`).
**No sandbox in the minimal core** — net/containment posture is deferred fog (SPEC §6);
`cmd` runs in-place at the repo cwd.

**Gate 2 — `judgment`:** one cold-critic `run()` in a **fresh context fed only
`spec.md` + the owns-diff** — never the author's transcript/self-summary. Returns
`{pass: bool, evidence: str, cited_paths: list[str]}` (old 3-field verdict, verbatim).
Same adapter/model as the rest of the core (per #007's "one model everywhere now"), but
the `run()` call **names its tier** (`"validate"`) so model-per-role / different-provider
graduates via #007 **without touching #004**. Independence today is *contextual*, not
model-level.

**Owns-diff:** the driver captures a git **baseline ref before the task executes**
(in-place), then computes `git diff <baseline> -- <owns paths>` as text and hands that
to the critic. No worktree/merge — that machinery stays deferred fog (SPEC §6). Core
assumes the target repo is git (true for the maiden self-host).

**Anti-rubber-stamp rule (structural, not honor-based):**
1. `pass:true` + empty `cited_paths` → **auto-fail** (old rule).
2. **Every `cited_path` must actually appear in the owns-diff handed to the critic** — a
   citation to a path not in the diff = hallucinated = **auto-fail**. Since the diff *is*
   the owns-diff, cited paths are by construction within the task's declared `owns`; no
   separate owns check needed.

**Leaf done ⟺** all `cmd` criteria exit 0 **AND** the `judgment` verdict passes
structurally (non-empty, diff-verified `cited_paths`). No agent self-report ever counts.

**Fail-handling boundary:** #004 returns the structured `TaskValidation` and **appends
the finding to the task's scratch file** (resume read-set, not a transcript — the next
attempt reads it without inheriting the author's context). The **bounded-retry state
machine** — reset failed task → pending, loop back to execute, halt after N rounds — and
the **retry-bound value** live in **#002**'s driver.

**Audit:** validate emits structured pass/fail events (task id, check, verdict,
`cited_paths`) to `events.jsonl` per invariant #2 / #005. **No standalone
`validation-report.md`** — the append-only log + live feed carry the audit.

**Deferred to fog:** the once-per-run **integration critic** (cross-task seam gate over
the full merged diff + `attribute_paths` routing) — graduates as another critic call when
multi-task cross-seam bugs actually bite.

**Interlocks:** #006 (user agrees this contract up front), #007 (tiering lets the
critic's model/provider independence graduate), #002 (retry state machine + bound, plan
graph validation host), #005 (validate events on the live feed), #003 (`owns`/diff derive
from the task data model).

Raw material: `ads/validate.py` (`_parse_verdict`, `_run_cmd_criterion`,
`_run_judgment_criterion`), `ads/tasks.py` (`ExitCriterion`).
