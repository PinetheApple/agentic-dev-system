PHASE:plan

Read the user's intent below and produce exactly one JSON object as your
entire response (no prose outside the JSON), shaped as:

```json
{
  "spec": "<markdown for spec.md>",
  "design": "<markdown for design.md>",
  "tasks": [
    {
      "filename": "01-slug.md",
      "id": "01-slug",
      "depends_on": ["00-other-id"],
      "owns": ["path/one.py"],
      "exit_criteria": [{"check": "cmd", "value": "pytest tests/test_x.py"}],
      "expert": "python-expert",
      "critical": true,
      "tier": "standard",
      "body": "<markdown task body: objective + interface to honor>"
    }
  ]
}
```

If `spec.md` already exists and is frozen (approved upstream), do not change
its content — return the existing spec text verbatim in `spec` and only
revise `design` and `tasks`.

## Intent

{intent}
