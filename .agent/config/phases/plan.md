PHASE:plan

Read the user's intent below and produce exactly one JSON object as your
entire response: no prose before or after it, and no markdown code fences —
just the raw JSON object, shaped as (the ```json fence below is only to
illustrate the shape — do not include fences in your actual answer):

```json
{
  "spec": "<markdown for spec.md>",
  "design": "<markdown for design.md, or null if this work needs no design doc>",
  "tasks": [
    {
      "id": "01-slug",
      "depends_on": ["00-other-id"],
      "owns": ["path/one.py"],
      "exit_criteria": [
        {"check": "cmd", "value": "pytest tests/test_x.py"},
        {"check": "judgment", "value": "<natural-language assertion an LLM will judge later>"}
      ],
      "body": "TASK_ID: 01-slug\n\n<markdown task body: objective + interface to honor>"
    }
  ]
}
```

`exit_criteria[].check` must be exactly `"cmd"` (a shell command; pass means
exit code 0) or `"judgment"` (a natural-language criterion an LLM judges
later) — no other values are recognized. Every task must carry at least one
`judgment` criterion.

Every task's `body` must include the literal line `TASK_ID: <that task's id>`
so the execution phase can identify itself from its own prompt.

If the intent below contains a SPEC gap — something the spec must decide but
the intent doesn't answer — resolve it yourself when the decision is safe and
record it, or stop and ask when it's genuinely ambiguous. Signal either case
with an optional top-level `gap` object:

```json
{"gap": {"ambiguous": false, "question": "", "decision": "<what you decided and why>"}}
```

or, for a genuinely ambiguous gap (the loop halts and asks the user):

```json
{"gap": {"ambiguous": true, "question": "<the question to ask the user>", "decision": ""}}
```

Omit `gap` entirely when the intent has no such gap.

If `spec.md` already exists and is frozen (approved upstream), do not change
its content — return the existing spec text verbatim in `spec` and only
revise `design` and `tasks`.

## Intent

{intent}
