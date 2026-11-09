---
name: operate-windows
description: Inspect and control a selected Windows application or import an explicit WorkObserver demonstration through Full Automation.
---

# Operate Windows

1. Call system_status, list desktop_windows, and select one exact window by title, process and handle. Confirm an unlocked interactive session.
2. Read desktop_snapshot. Prefer the returned UI Automation selector. If the control exposes no selector, take a fresh desktop_screenshot and use its physical coordinates only in the matching window.
3. Check the visible result after invoke, fill, keys, or mouse actions. Reinspect changed window handles and modal dialogs before continuing.
4. Record demonstrations only for explicit process IDs. Use recording_stop and workflow_import_recording, then resolve manual steps before replay.
5. Preserve Windows and company controls. For protected Office files, use the authorized Office application and company-supported interfaces. Confirm the final business output separately.

Read [validation boundaries](../../../docs/CAPABILITIES.md) and [the runbook](../../../docs/operations/RUNBOOK_AR.md).
