---
name: operate-browser
description: Operate Chrome through Full Automation for page inspection, navigation, forms, downloads, and browser workflows.
---

# Operate Browser

1. Use system_status, then browser_connect for an explicitly selected loopback endpoint or browser_launch for a separate Chrome profile.
2. Read browser_tabs and browser_snapshot. Prefer a unique stable selector and verify the page URL and state before each material action.
3. Use browser_fill and browser_click, then verify the result with a page assertion or file_wait. Check the pre-existing file first when verifying a download. Use the newer_than filter and file_inspect when you need freshness and a hash.
4. For frames, inspect with frame_id. Use explicit raw CDP only when the high-level action cannot address the target and the session is known.
5. After a timeout, inspect the tab and output. A click might already have executed. Record evidence, not screen contents, in project documentation.

Read [the tool catalog](../../../docs/reference/TOOLS.md) for argument shapes and [the runbook](../../../docs/operations/RUNBOOK_AR.md) for failures.
