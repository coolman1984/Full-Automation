---
name: maintain-automation-docs
description: Maintain Full Automation's capability map, tool reference, release history, engineering decisions, lessons, and project skills.
---

# Maintain Automation Docs

1. Begin at the project map. Identify the source owner and evidence for the feature you are changing.
2. Update the map, workflow reference or runbook as appropriate. Change tool descriptions and schemas in code, then regenerate the reference.
3. Record the release change in CHANGELOG. Add a decision only for a lasting trade-off. Add a lesson only when it has a reproducible observation or a clear evidence link.
4. Edit canonical repository skills under .claude/skills. Run python scripts/docs_check.py to regenerate their Codex mirrors and the tool catalog.
5. Run python scripts/docs_check.py --check and the relevant tests. Separate compiled Windows features from features proven on a real Windows desktop.

Read [the documentation index](../../../docs/README.md) and [the project map](../../../docs/PROJECT_MAP_AR.md).
