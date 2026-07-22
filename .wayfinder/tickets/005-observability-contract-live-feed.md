<!-- labels: wayfinder:grilling -->
# Observability contract: loop_fmt-style live feed + pinned status footer + jsonl tee

Assignee: _(unassigned)_
Blocked by: #001

## Question

What is the core observability surface — modelled on `music_app`'s `loop_fmt.py`: a
colored tag per event streamed live to the terminal, plus a **pinned footer showing
current project/task status**, with raw events tee'd to `events.jsonl`? What events does
the loop emit, what does the footer show, and do we adapt `loop_fmt.py` or write fresh?

Basic observability is a *core* pillar (not deferred). The interactive curses TUI is the
deferred #009 feature — this is the lighter always-on feed. Missions' "mission control"
(see the reference) says the footer must answer two glance-questions for a long run: **how
much is complete** and **how much budget/tokens is burned** — fold token/budget burn into
the footer, not just current task. Parallelizable after #001.
