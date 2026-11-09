---
name: build-repeatable-workflow
description: Teach, validate, replay, export, cancel, or schedule a Full Automation workflow and inspect its run evidence.
---

# Build Repeatable Workflow

1. Define inputs and expected outputs, then record authorized actions or write the recipe. Parameterize dates and paths, and add an assertion after material operations.
2. Run workflow_plan with representative inputs. Resolve every blocker, including manual steps in branches, before executing.
3. Test on a disposable target. Verify output freshness and business content. Save the run_id and inspect workflow_run_info after failures before retrying.
4. Use workflow_start and workflow_status for a long owned job. Cancel that job with workflow_cancel and wait for its terminal status. The in-flight OS command can still complete.
5. Export a verified recipe as needed. Preview and register only the schedule authorized by the user. Keep the exact digest and test the Windows account and desktop conditions.

Read [the workflow format](../../../docs/reference/WORKFLOW_FORMAT.md) and [the runbook](../../../docs/operations/RUNBOOK_AR.md).
