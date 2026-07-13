PHASE:validate-judgment

Judge whether the following exit criterion is satisfied, using ONLY the spec
and the diff below as evidence — never any scratch notes or self-reported
summary from the task's own run (you were not given those; this is a cold,
author-agnostic review). Respond with exactly one JSON object as your entire
response: no prose before or after it, and no markdown code fences — just
the raw JSON object (the ```json fence below is only to illustrate the
shape), shaped as:

```json
{"pass": true | false, "evidence": "<what in the diff satisfies or fails this>", "cited_paths": ["<path>", ...]}
```

A `pass: true` verdict with an empty `cited_paths` list is automatically
treated as a failure — you must cite the specific file(s) your verdict rests
on. Do not be generous: if the evidence is missing or ambiguous, judge it as
not satisfied and say why.

## Criterion

{criterion}

## Diff (this task's owns paths, ground truth)

{diff}
