<!-- labels: wayfinder:grilling -->
# Adapter Protocol for the spine: Claude Code + stub only

Assignee: _(unassigned)_
Blocked by: #001

## Question

What is the minimal `run()` Protocol the spine calls — the inputs it's handed (composed
prompt, allowed tools, worktree) and the structured result it must return — such that the
Claude Code adapter (shells out to `claude -p`) and the `stub` adapter (canned responses
for token-free unit tests) both satisfy it, and OpenCode graduates later without changing
the Protocol?

Keep it to claude + stub. One fold from Missions (see the reference): the Protocol should
let a role name its own **model/tier** (planning=careful reasoning, execution=fast
fluency, validation=precise instruction-following, possibly a different provider) so
model-per-role graduates without touching the Protocol — even if the minimal core wires
one model everywhere for now. Grill the boundary. Parallelizable after #001.
