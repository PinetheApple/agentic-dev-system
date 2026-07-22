<!-- labels: wayfinder:grilling -->
# Validation contract: cmd exit-code + cold-critic cited_paths, forgery-proof

Assignee: _(unassigned)_
Blocked by: #001

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
