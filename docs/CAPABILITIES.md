# Validation and operating boundaries

| Capability | Evidence |
|---|---|
| Chrome control | Exercised against a real Chrome headless instance and a local page. Tests cover Arabic typing, click, ambiguity and hidden inputs, shadow roots, frame inspection, click, typing and waiting inside a same-origin frame, a refused cross-origin frame, file upload, screenshot and raw CDP. |
| Chrome demonstration | Exercised against real Chrome: a person-style session (trusted CDP input) with Arabic typing, a password field, checkbox, select, a Shadow DOM button and a click-caused navigation is recorded with a visible badge, converted to a workflow, given an assertion and replayed in a fresh engine with different inputs. The password value never appears in results. A confirm dialog answered during the demonstration becomes a dialog step before its click. A hostile page that enumerates its globals finds no callable recording channel and cannot stop the recording; calls from a frame that kept the binding are marked as frame events and never become top-page steps. Dialog handling is tested for dismiss, accept and a prompt answer. Typing and clicks inside a same-origin frame are recorded with a `frame` argument and replayed with different input; focus moving into a cross-origin frame becomes a manual step. Chords, scrolling, drag and right click are not translated. Real mouse and keyboard on a desktop Chrome were not exercised. |
| Workflow replay | A recorded browser task is replayed in a fresh engine with a new target and different input. Unit tests cover branching, required inputs, assertions, manual barriers, stop, digest pinning, and Python export. |
| Agent integration | MCP stdio handshake, discovery and tool calls were exercised locally. Conversation capture was tested with a real subprocess emitting fixture data. Discovery and live reading of saved conversations were exercised on a real Claude Code project log and on synthetic Codex rollout, Cline task and configured-profile files, including credential-file exclusion, reasoning omission and a partly written last line. Codex and Cline file layouts follow their current conventions and need confirmation against the installed versions. `Connect-Codex.cmd` was not run. Live authenticated runs of the four vendor CLIs need their installations and accounts. |
| Windows recorder and control | The Windows executables were compiled. `tests/Windows-Smoke.ps1` checks window discovery, inspection and screenshots in a real interactive Windows session. Native control, password-protected app behavior and locked-screen behavior need target-device validation. |
| Windows scheduling | Schedule definition generation, path handling and workflow digest validation were tested. Task registration and execution need Windows. |
| Preflight and history | Regression checks cover an early write followed by a manual barrier, a malformed later action, history surviving a new engine, retry counts, and cancellation without the global stop. |
| Workflow engine details | Unit tests cover the extended comparisons and their save-time and runtime errors, retry backoff timing, an exported script refusing an edited embedded definition, readable recorded input names and dropped observation steps, receipt pruning that keeps running and recent runs, journal rotation, and parameter descriptions. Shortcut import is unit-tested with a synthetic session; the recorder's new key names were compiled in CI but not recorded on a desktop. |
| Output freshness | A stale file is rejected with a prior timestamp. A newer stable file is accepted and its content hash can be compared. |
| Continuous integration | [GitHub Actions](../.github/workflows/ci.yml) runs the documentation check and all tests with real Chrome on Linux (Python 3.10 and 3.13), and on Windows compiles the bridge and recorder, runs the recorder's non-interactive self-test and the unit tests. It does not exercise native UI control, recording or Task Scheduler. |
| Documentation | A local check compares the generated tool catalog with registered tools, validates project skill metadata and mirrors, and checks relative links. |

The project's automation interface exposes actions and screen context to a reasoning agent. It can operate ordinary accessible apps and pages. Apps may expose only partial text, and protected desktops, policies, native dialogs, ambiguous targets and visual-only controls can require manual input. The tool descriptions and failures distinguish those cases.

## Repeat validation

```powershell
python -m unittest discover -s tests -v
python scripts/docs_check.py --check
$env:CHROME_TEST_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m unittest discover -s tests -v
.\scripts\Build-Windows.ps1 -Test
.\tests\Windows-Smoke.ps1 -WindowTitle 'Exact title of a disposable test window'
```

For a task scheduler smoke test, save a workflow with a zero-duration `wait` and a passing `assert`, inspect `schedule_preview`, register a one-time job under a unique test name, and verify the last result and journal before removing that test job.
