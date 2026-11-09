# Workflow format

[Project map](../PROJECT_MAP_AR.md) · [Tool catalog](TOOLS.md)

A version 1 definition contains a safe file name in `name`, a nonempty `steps` array, optional `required_inputs`, and an optional loopback `browser_endpoint`. Store one recipe with the workflow save command or `workflow_save`.

| Key | Meaning |
|---|---|
| `tool` | A registered action or one of the workflow operations below |
| `args` | Arguments passed to the action |
| `save_as` | Result name for later steps and returned outputs |
| `retries` | Integer from zero to three. Potential writes require explicit `idempotent: true` to retry |
| `idempotent` | A declaration that repeating the action has the same intended effect. Verify this property in the app before using it |

A complete placeholder preserves the referenced JSON value type. A placeholder within text inserts its string representation. Reference a previous result using a dotted path such as `${tab.targetId}` or an explicitly supplied value such as `${report_date}`. Resolve `${env:NAME}` from the current process environment. Keep secrets out of recipe files and scheduler inputs.

| Operation | Fields and behavior |
|---|---|
| `wait` | `args.seconds` from zero to 120. Checks stop and cancellation while waiting |
| `assert` | `args.actual` plus `equals`, `contains`, or a truthiness check. `label` identifies the evidence expected |
| `if` | `condition` uses the same comparison fields. Execute `then` or `else` |
| `foreach` | `items` is a list or variable. Bind each item under `as` and execute nested `steps` |
| `manual` | A review barrier preserved from demonstrations. Every branch is checked before replay or scheduling |

Run this first for an existing definition:

```powershell
python main.py workflow plan browser-form --variables-file examples/browser-inputs.json
```

Planning returns the digest, missing declared inputs, manual blockers, and possible writes for each source step. It validates literal argument shapes across the definition. Live selectors, values produced by earlier actions, environment variables used during execution, and output assertions are checked at runtime.

Run the reviewed recipe from the CLI or MCP. Use `workflow_start` for long MCP jobs, then `workflow_status` or `workflow_cancel` on the same host. `workflow_history` and `workflow_run_info` read durable receipts from later processes. A receipt records a run ID, digest, status, source path within the recipe, step ordinal, attempts, timing, and error class. It excludes values and response payloads.

For details of a failed run, use the latest receipt and its last attempted step. A `running` receipt after a process exit means completion was never recorded. Inspect the real effect before retrying.

See the [complete browser recipe](../../examples/browser-form.json) and [example inputs](../../examples/browser-inputs.json).
