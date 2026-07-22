<!-- labels: wayfinder:grilling -->
# User-in-the-loop contract: scope agreed up front, done declared by user

Assignee: _(unassigned)_
Blocked by: #001

## Question

How does the user agree scope up front and declare "done"? What are the CLI verbs
(`start` / `approve` / `status` / `done` …), where do the review gate(s) sit in the
spine, and what's the sign-off mechanic that makes the user — not the loop — the one who
ends a task? How do unambiguous-gap (decide+record) vs ambiguous (stop-point) branch?

This is destination-defining (done is user-declared). Fold Missions' model (see the
reference): scope-agreed-up-front = the user **approves a plan that carries a validation
contract** (the assertions of #004) before any code; "done" = that contract satisfied
**and** the user's explicit sign-off — never agent self-report. Grill it. Parallelizable
after #001.
