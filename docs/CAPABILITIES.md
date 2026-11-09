# Validation and operating boundaries

| Capability | Evidence |
|---|---|
| Chrome control | Exercised against a real Chrome headless instance and a local page. Tests cover Arabic typing, click, ambiguity and hidden inputs, shadow roots, frame inspection, file upload, screenshot and raw CDP. |
| Workflow replay | A recorded browser task is replayed in a fresh engine with a new target and different input. Unit tests cover branching, required inputs, assertions, manual barriers, stop, digest pinning, and Python export. |
| Agent integration | MCP stdio handshake, discovery and tool calls were exercised locally. Conversation capture was tested with a real subprocess emitting fixture data. Live authenticated runs of the four vendor CLIs need their installations and accounts. |
| Windows recorder and control | The Windows executables were compiled. `tests/Windows-Smoke.ps1` checks window discovery, inspection and screenshots in a real interactive Windows session. Native control, password-protected app behavior and locked-screen behavior need target-device validation. |
| Windows scheduling | Schedule definition generation, path handling and workflow digest validation were tested. Task registration and execution need Windows. |

The project's automation interface exposes actions and screen context to a reasoning agent. It can operate ordinary accessible apps and pages. Apps may expose only partial text, and protected desktops, policies, native dialogs, ambiguous targets and visual-only controls can require manual input. The tool descriptions and failures distinguish those cases.

## Repeat validation

```powershell
python -m unittest discover -s tests -v
$env:CHROME_TEST_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m unittest discover -s tests -v
.\scripts\Build-Windows.ps1 -Test
.\tests\Windows-Smoke.ps1 -WindowTitle 'Exact title of a disposable test window'
```

For a task scheduler smoke test, save a workflow with a zero-duration `wait` and a passing `assert`, inspect `schedule_preview`, register a one-time job under a unique test name, and verify the last result and journal before removing that test job.
