"""End-to-end showcase of Full Automation, driven exactly the way an agent CLI drives it: over MCP stdio.

It runs on fixtures only (a local web page and a throw-away Windows form), never on the owner's apps:

1. Environment: system_status and agent_probe.
2. Browser (headless Chrome, dedicated profile): record an agent session that fills a form with Arabic text,
   a number and a dropdown, then turn it into a saved workflow with a business assertion.
3. Replay: the static plan blocks missing inputs; the workflow replays with different inputs in a new tab,
   the assertion proves the page result, and the workflow is exported to Python and fingerprinted.
4. Windows desktop: a small form is opened, read through UI Automation, filled, its button pressed and the
   result read back, then the form is closed.

A self-contained HTML report with screenshots is written to %LOCALAPPDATA%\\FullAutomation\\showcase.
Usage:  python scripts/showcase.py [--skip-desktop] [--visible-browser]
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Shipment request</title>
<style>body{font:15px Segoe UI,Arial;margin:32px;background:#f4f6fa;color:#1d2433}
form{background:#fff;padding:24px;border-radius:10px;max-width:460px;box-shadow:0 2px 8px #0002}
label{display:block;margin-top:12px;font-weight:600}input,select{width:100%;padding:8px;margin-top:4px;box-sizing:border-box}
button{margin-top:18px;padding:10px 22px;background:#1f5eff;color:#fff;border:0;border-radius:6px;font-size:15px}
#result{margin-top:18px;padding:12px;border-radius:6px;background:#e7f7ec;display:none;max-width:436px}</style></head>
<body><h2>Shipment request (local test page)</h2>
<form onsubmit="event.preventDefault();const q=+qty.value;const r=document.querySelector('#result');
r.style.display='block';r.textContent='Request accepted: '+customer.value+' / '+q+' units / plant '+plant.value+' / total '+(q*12.5).toFixed(2)+' USD';">
<label for="customer">Customer</label><input id="customer" required>
<label for="qty">Quantity</label><input id="qty" type="number" min="1" required>
<label for="plant">Plant</label><select id="plant"><option value="cairo">Cairo</option><option value="alex">Alexandria</option><option value="suez">Suez</option></select>
<button id="submit" type="submit">Submit request</button></form><div id="result"></div></body></html>""".encode()

FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form; $f.Text = 'Full Automation Showcase'; $f.Width = 480; $f.Height = 230; $f.StartPosition = 'CenterScreen'; $f.TopMost = $true
$l = New-Object Windows.Forms.Label; $l.Text = 'Batch number:'; $l.Left = 20; $l.Top = 28; $l.Width = 120
$t = New-Object Windows.Forms.TextBox; $t.Name = 'batchBox'; $t.Left = 150; $t.Top = 25; $t.Width = 280
$b = New-Object Windows.Forms.Button; $b.Name = 'approveButton'; $b.Text = 'Approve'; $b.Left = 150; $b.Top = 65; $b.Width = 120
$r = New-Object Windows.Forms.Label; $r.Name = 'statusLabel'; $r.Text = 'Waiting for approval'; $r.Left = 20; $r.Top = 120; $r.Width = 420
$b.Add_Click({ $r.Text = 'Approved batch ' + $t.Text })
$f.Controls.AddRange(@($l, $t, $b, $r)); [void]$f.ShowDialog()
"""


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class MCPClient:
    """Minimal MCP stdio client: the same JSON-RPC an agent CLI sends."""

    def __init__(self, data_dir: Path):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.stderr = open(data_dir / "showcase-server.log", "w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "mcp"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8",
                                        bufsize=1, creationflags=flags)
        self.counter = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "showcase", "version": "1"}})
        self.tool_names = [tool["name"] for tool in self.request("tools/list", {})["tools"]]

    def request(self, method, params):
        self.counter += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("MCP server exited; see showcase-server.log")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']['message']}")
        return reply["result"]

    def call(self, tool, **args):
        result = self.request("tools/call", {"name": tool, "arguments": args})
        texts = [c["text"] for c in result["content"] if c["type"] == "text"]
        images = [c["data"] for c in result["content"] if c["type"] == "image"]
        if result.get("isError"):
            raise RuntimeError(f"{tool}: {texts[0] if texts else 'failed'}")
        value = json.loads(texts[0]) if texts else None
        return (value, images[0]) if images else value

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=15)
        except Exception:
            self.process.kill()
        self.stderr.close()


class Report:
    def __init__(self, folder: Path):
        self.folder = folder
        self.sections = []
        self.passed = 0
        self.failed = 0

    def step(self, title, detail, ok=True, image=None, data=None):
        self.passed += ok
        self.failed += not ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {title}: {detail}", flush=True)
        self.sections.append((title, detail, ok, image, data))

    def write(self):
        rows = []
        for index, (title, detail, ok, image, data) in enumerate(self.sections, 1):
            block = [f'<section class="{"ok" if ok else "bad"}"><h3>{index}. {html.escape(title)} '
                     f'<span>{"PASS" if ok else "FAIL"}</span></h3><p>{html.escape(detail)}</p>']
            if image:
                name = f"shot-{index}.png"
                (self.folder / name).write_bytes(base64.b64decode(image))
                block.append(f'<img alt="{html.escape(title)}" src="data:image/png;base64,{image}">')
            if data is not None:
                block.append("<details><summary>Raw data</summary><pre>"
                             + html.escape(json.dumps(data, ensure_ascii=False, indent=2)[:20000]) + "</pre></details>")
            rows.append("".join(block) + "</section>")
        page = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<title>Full Automation Showcase</title><style>"
                ":root{--bg:#f5f7fb;--fg:#1c2230;--card:#fff;--ok:#1f8a4c;--bad:#c62828}"
                "@media(prefers-color-scheme:dark){:root{--bg:#14171f;--fg:#e6e9ef;--card:#1d2230}}"
                "body{font:15px Segoe UI,Arial;background:var(--bg);color:var(--fg);margin:0 auto;padding:16px;max-width:980px}"
                "section{background:var(--card);border-left:5px solid var(--ok);border-radius:8px;padding:12px 18px;margin:14px 0}"
                "section.bad{border-color:var(--bad)}h3 span{float:right;color:var(--ok)}section.bad h3 span{color:var(--bad)}"
                "img{max-width:100%;border:1px solid #8884;border-radius:6px}pre{white-space:pre-wrap;font-size:12px}</style></head><body>"
                f"<h1>Full Automation showcase</h1><p>{time.strftime('%Y-%m-%d %H:%M')} — {self.passed} passed, {self.failed} failed. "
                "Every step below was executed through the MCP server, the same interface Claude Code uses.</p>"
                + "".join(rows) + "</body></html>")
        path = self.folder / "report.html"
        path.write_text(page, encoding="utf-8")
        return path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def launched_browser(mcp: MCPClient):
    return getattr(mcp, "browser_launched", False)


def browser_part(mcp: MCPClient, report: Report, url: str, visible: bool):
    launched = mcp.call("browser_launch", port=free_port(), headless=not visible)
    mcp.browser_launched = True
    report.step("Chrome started", f"Dedicated automation profile, debugging port on loopback only: {launched['endpoint']}",
                data={"pid": launched["pid"], "browser": launched["browser"].get("Browser")})

    first = {"customer": "مصنع العاشر للتغليف", "qty": "40", "plant": "alex"}
    mcp.call("workflow_record_start", name="showcase-shipment")
    tab = mcp.call("browser_new_tab")["targetId"]
    mcp.call("browser_navigate", target_id=tab, url=url)
    snap = mcp.call("browser_snapshot", target_id=tab)
    report.step("Page read", f"Title '{snap['title']}', {len(snap['elements'])} interactive elements with unique selectors",
                data=[{k: e[k] for k in ("selector", "tag", "label")} for e in snap["elements"]])
    mcp.call("browser_fill", target_id=tab, selector="#customer", text=first["customer"])
    mcp.call("browser_fill", target_id=tab, selector="#qty", text=first["qty"])
    mcp.call("browser_select", target_id=tab, selector="#plant", value=first["plant"])
    mcp.call("browser_click", target_id=tab, selector="#submit")
    mcp.call("browser_wait", target_id=tab, expression="document.querySelector('#result').textContent.length > 0")
    text = mcp.call("browser_evaluate", target_id=tab, expression="document.querySelector('#result').textContent")
    _, shot = mcp.call("browser_screenshot", target_id=tab)
    ok = first["customer"] in text and "500.00" in text
    report.step("Form filled by the agent (Arabic text, number, dropdown, click)", text, ok, shot)

    recorded = mcp.call("workflow_record_stop")
    definition = recorded["definition"]
    examples = recorded["example_inputs"]
    report.step("Actions recorded as a workflow",
                f"{len(definition['steps'])} steps; typed values became inputs {sorted(examples)}", data=definition)

    # Turn the recording into a checked recipe: read the business result and assert on it.
    target_ref = next(s["args"]["target_id"] for s in definition["steps"] if s["tool"] == "browser_navigate")
    by_value = {value: name for name, value in examples.items()}
    customer_input = by_value[first["customer"]]
    definition["steps"] += [
        {"tool": "browser_wait", "args": {"target_id": target_ref, "expression": "document.querySelector('#result').textContent.length > 0"}},
        {"tool": "browser_evaluate", "args": {"target_id": target_ref, "expression": "document.querySelector('#result').textContent"}, "save_as": "result"},
        {"tool": "assert", "args": {"actual": "${result}", "contains": "${" + customer_input + "}", "label": "Page shows the requested customer"}},
        {"tool": "assert", "args": {"actual": "${result}", "matches": "total [0-9]+\\.[0-9]{2} USD", "label": "Page shows a computed total"}},
    ]
    saved = mcp.call("workflow_save", definition=definition)
    report.step("Business checks added and saved", f"Two assertions added; workflow digest {saved['sha256'][:16]}…", data=saved)

    blocked = mcp.call("workflow_plan", name="showcase-shipment", variables={})
    report.step("Static plan refuses a run with missing inputs", "; ".join(blocked["blockers"]), not blocked["ready"])

    second = {"customer": "Delta Steel Works", "qty": "125", "plant": "suez"}
    variables = {by_value[first[key]]: second[key] for key in first}
    plan = mcp.call("workflow_plan", name="showcase-shipment", variables=variables)
    report.step("Plan with new inputs is ready", f"ready={plan['ready']}, {sum(s['may_write'] for s in plan['steps'])} steps may change the page",
                plan["ready"], data=plan)

    started = time.monotonic()
    run = mcp.call("workflow_run", name="showcase-shipment", variables=variables, expected_sha256=saved["sha256"])
    elapsed = time.monotonic() - started
    replay_tab = next(t for t in reversed(mcp.call("browser_tabs")) if t["url"].startswith(url) and t["id"] != tab)["id"]
    replay_text = mcp.call("browser_evaluate", target_id=replay_tab, expression="document.querySelector('#result').textContent")
    _, replay_shot = mcp.call("browser_screenshot", target_id=replay_tab)
    ok = second["customer"] in replay_text and "1562.50" in replay_text
    report.step("Replayed with different inputs, no model involved",
                f"{elapsed:.1f}s in a new tab, assertions passed: {replay_text}", ok, replay_shot, data=run)

    exported = mcp.call("workflow_export", name="showcase-shipment")
    info = mcp.call("file_inspect", path=exported["path"], with_sha256=True)
    report.step("Exported to inspectable Python", f"{exported['path']} ({info['bytes']} bytes, sha256 {info['sha256'][:16]}…)", data=info)
    history = mcp.call("workflow_history", name="showcase-shipment")
    report.step("Durable run receipt written", f"{len(history)} receipt(s); latest status '{history[0].get('status')}'",
                bool(history), data=history[:1])


def desktop_part(mcp: MCPClient, report: Report):
    flags = subprocess.CREATE_NO_WINDOW
    encoded = base64.b64encode(FORM.encode("utf-16-le")).decode()
    legacy = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    form = subprocess.Popen([str(legacy), "-NoProfile", "-STA", "-EncodedCommand", encoded], creationflags=flags)
    try:
        window = None
        for _ in range(100):
            matches = [w for w in mcp.call("desktop_windows") if w["title"] == "Full Automation Showcase" and w["pid"] == form.pid]
            if matches:
                window = {"hwnd": matches[0]["hwnd"]}
                break
            time.sleep(0.2)
        if window is None:
            report.step("Windows form opened", "The test form did not appear", False)
            return
        count = len(mcp.call("desktop_windows"))
        report.step("Windows listed", f"{count} visible top-level windows found; test form PID {form.pid}")
        snap = mcp.call("desktop_snapshot", window=window)
        rows = snap.get("elements", snap) if isinstance(snap, dict) else snap
        report.step("Form read through UI Automation", f"{len(rows)} elements exposed", data=rows)
        base = {"window": window}
        mcp.call("desktop_fill", selector={**base, "id": "batchBox"}, text="B-2026-1109")
        mcp.call("desktop_action", selector={**base, "id": "approveButton"}, action="invoke")
        time.sleep(0.5)
        after = mcp.call("desktop_snapshot", window=window)
        rows = after.get("elements", after) if isinstance(after, dict) else after
        status = next((r.get("name") for r in rows if r.get("selector", {}).get("id") == "statusLabel"), None)
        _, shot = mcp.call("desktop_screenshot", window=window)
        report.step("Field filled and button pressed in a native app", f"Status label now reads: {status!r}",
                    status == "Approved batch B-2026-1109", shot)
    finally:
        form.kill()
        form.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-desktop", action="store_true")
    parser.add_argument("--visible-browser", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the HTML report when finished")
    args = parser.parse_args()
    data = Path(os.environ.get("FULL_AUTOMATION_DATA") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FullAutomation")
    folder = data / "showcase" / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    report = Report(folder)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/shipment"
    mcp = MCPClient(folder)
    try:
        status = mcp.call("system_status")
        report.step("MCP server connected", f"{len(mcp.tool_names)} tools, version {status['version']}, bridge built: {status['bridge_exists']}", data=status)
        agents = mcp.call("agent_probe")
        report.step("Agent CLIs detected", ", ".join(f"{a['agent']}={'yes' if a['available'] else 'no'}" for a in agents), data=agents)
        try:
            browser_part(mcp, report, url, args.visible_browser)
        except Exception as exc:
            report.step("Browser part stopped", str(exc), False)
        if os.name == "nt" and not args.skip_desktop:
            try:
                desktop_part(mcp, report)
            except Exception as exc:
                report.step("Desktop part stopped", str(exc), False)
    finally:
        if launched_browser(mcp):
            try:
                mcp.call("browser_cdp", method="Browser.close")  # the showcase's own headless Chrome only
            except Exception:
                pass
        mcp.close()
        server.shutdown()
    path = report.write()
    print(f"\n{report.passed} passed, {report.failed} failed. Report: {path}")
    if args.open and os.name == "nt":
        os.startfile(path)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
