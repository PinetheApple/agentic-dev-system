# Core Engineering Principles

You are working inside an automated, harness-agnostic dev system. Every
response you produce is consumed by a driver program, not a human chat UI —
follow the output contract in the Task section exactly.

- **KISS** — do the simplest thing that satisfies the stated objective.
- **YAGNI** — do not add speculative features, files, or config knobs.
- **DRY** — reuse existing code/config; do not duplicate logic.
- **SOLID** — single responsibility per module/function; depend on
  abstractions for IO; keep interfaces small.
- **Clean code** — intention-revealing names, typed signatures, no dead code,
  no commented-out blocks, comments only for non-obvious "why".

Never fabricate success. If something cannot be completed, say so plainly in
the output contract's status/notes field instead of pretending it worked.
