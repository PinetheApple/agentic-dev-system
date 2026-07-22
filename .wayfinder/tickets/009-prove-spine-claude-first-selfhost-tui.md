<!-- labels: wayfinder:task -->
# Prove the spine end-to-end on Claude Code — first self-hosted task: the curses TUI

Assignee: _(unassigned)_
Blocked by: #008

## Question

Run the green spine for real: swap the stub for the Claude Code adapter and drive **one
real task end-to-end** — the first self-hosted feature, ADS's own **curses TUI** (reads
`state.json`, mutates nothing, so a botched run can't corrupt the loop). intake → plan →
execute → validate → **user declares done**.

This resolving = the destination reached. Answer records what the loop built, the
observed live-feed behaviour, where (if anywhere) it halted, and the user's done-call.
HITL — user signs off. Once green, the deferred backlog in **Not yet specified** starts
graduating (sandbox, parallelism, OpenCode…).
