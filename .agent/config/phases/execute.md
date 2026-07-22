PHASE:execute
TASK_ID: {task_id}

Implement the task below using your tools. You own exactly these paths — do
not edit anything outside this list:

{owns}

When you are done (or blocked), respond with exactly one JSON object as your
entire final message: no prose before or after it, and no markdown code
fences — just the raw JSON object, shaped as (the ```json fence below is
only to illustrate the shape — do not include fences in your actual answer):

```json
{
  "task_id": "{task_id}",
  "status": "complete" | "blocked",
  "commands": [{"cmd": "<shell command you ran>", "exit": 0}],
  "undone": ["<anything in scope you did not finish>"],
  "issues": [{"desc": "<problem you hit>", "blocking": true | false}]
}
```

Never fabricate success: if you could not finish, say `"status": "blocked"`
and list what's undone and why in `issues`.

## Task

{task}
