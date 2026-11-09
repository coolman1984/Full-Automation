---
name: manage-cli-session
description: Connect, diagnose, or read managed Claude Code, Codex, Cline, Antigravity, and custom CLI sessions in Full Automation.
---

# Manage Cli Session

1. Use system_status and agent_probe before starting a session. Select the requested installed agent and exact model. Its own login and permissions apply.
2. To use Full Automation from the current agent, load the MCP configuration and call its tools directly. Use agent_start only when a separate managed headless process is required.
3. Read events with agent_read and its returned cursor. Inspect the exit code, not only the final chat text.
4. Read a specific saved transcript with transcript_read. Existing terminal windows expose only text the application makes available through Windows accessibility.
5. Use agent_stop for the owned process. Keep the host connected while polling, and avoid putting prompts, screenshots, and transcripts in commits.

Read [configuration](../../../docs/ARCHITECTURE.md) and [agent handoff](../../../docs/AGENT_HANDOFF.md).
