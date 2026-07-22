<!-- labels: wayfinder:grilling -->
# Task data model: DAG + disjoint-owns, serial ready-set (the parallelism seam)

Assignee: _(unassigned)_
Blocked by: #001

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
