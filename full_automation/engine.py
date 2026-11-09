from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time

from . import __version__
from .agents import Agents, command_prefix, read_transcript
from .cdp import Browser
from .common import AutomationError, Journal, ROOT, RunLock, data_directory
from .demo import BrowserDemo
from .desktop import Desktop
from .files import inspect_file
from .scheduler import Scheduler
from .validation import check_schema
from .workflows import Workflows

UNLOCKED_CONTROLS = {"system_stop", "system_resume", "workflow_cancel", "agent_stop", "recording_stop", "browser_demo_stop"}


@dataclass
class Tool:
    name: str
    description: str
    handler: object
    schema: dict
    mutates: bool = False

    def public(self):
        return {"name":self.name,"description":self.description,"inputSchema":self.schema,
                "annotations":{"readOnlyHint":not self.mutates,"destructiveHint":self.mutates,"openWorldHint":True}}


# Shown to the agent in each tool's input schema.
PARAMETER_HELP = {
    "target_id": "Chrome tab ID from browser_tabs or browser_new_tab (targetId).",
    "selector": "Unique CSS selector. For open shadow roots pass a list: host selector first, then the inner selector.",
    "frame": "Same-origin iframe selectors from the top document inward, such as [\"#checkout\"]; omit for the top document.",
    "frame_id": "Frame ID from browser_snapshot frame_tree; omit for the top document.",
    "endpoint": "Loopback CDP URL with port, such as http://127.0.0.1:9222.",
    "url": "Address to open.",
    "text": "Exact text to type; use an empty string to clear the field.",
    "value": "Value to use.",
    "expression": "JavaScript expression; it runs in the page and may change it.",
    "timeout": "Maximum seconds to wait.",
    "window": "Exact window match from desktop_windows, such as {\"title\": ..., \"path\": ...}.",
    "keys": "Separate key names pressed together, such as [\"CTRL\", \"S\"].",
    "name": "Name of 1-80 letters, numbers, dots, underscores or hyphens.",
    "variables": "Workflow inputs as an object; see workflow_plan for the required names.",
    "expected_sha256": "Refuse to run unless the workflow still has this digest.",
    "definition": "Workflow definition with version 1, name and steps.",
    "path": "Local file path.",
    "newer_than": "Unix time in seconds; older files are rejected.",
    "cursor": "Value of next_cursor from the previous call; 0 to start.",
    "limit": "Maximum items to return.",
    "session_id": "Managed CLI session ID from agent_start.",
    "job_id": "Background job ID from workflow_start.",
    "run_id": "Run receipt ID from workflow_run, workflow_start or workflow_history.",
    "agent": "CLI profile name such as claude, codex, cline or agy.",
    "prompt": "Prompt passed to the CLI as one argument.",
    "cwd": "Working directory for the CLI.",
    "model": "Model identifier for the CLI; Claude defaults to claude-opus-5-5.",
    "action": "One of the actions named in the tool description.",
    "prompt_text": "Text to answer a prompt() dialog with.",
    "conversation_only": "Return only user and assistant messages and tool names.",
    "older_than_days": "Age in days; receipts started earlier are deleted.",
    "pids": "Process IDs of the applications to record.",
    "at": "Local time HH:MM, or YYYY-MM-DDTHH:MM:SS for once.",
    "frequency": "daily, weekly or once.",
    "weekdays": "Full English weekday names for weekly schedules.",
    "workflow": "Saved workflow name.",
    "save": "Also save the converted workflow.",
}


def schema_for(handler):
    signature=inspect.signature(handler)
    properties={}
    required=[]
    arrays={"files","keys","pids","weekdays","arguments","frame"}
    objects={"params","window","selector","variables","definition"}
    numbers={"timeout","x","y","to_x","to_y","seconds","stable_seconds","newer_than","older_than_days"}
    integers={"port","count","cursor","limit","modifiers","delta"}
    booleans={"headless","clear","capture_text","screenshots","allow_keyboard","parameterize_inputs","save","with_sha256","conversation_only"}
    for name,param in signature.parameters.items():
        if param.kind in {inspect.Parameter.VAR_KEYWORD,inspect.Parameter.VAR_POSITIONAL}:
            continue
        if name in arrays:
            kind={"type":"array"}
        elif name in objects:
            kind={"type":"object"}
        elif name in numbers:
            kind={"type":"number"}
        elif name in integers:
            kind={"type":"integer"}
        elif name in booleans:
            kind={"type":"boolean"}
        elif name=="value":
            kind={}
        else:
            kind={"type":"string"}
        if name=="selector" and handler.__module__.endswith("cdp"):
            kind={"anyOf":[{"type":"string"},{"type":"array","items":{"type":"string"}}]}
        if name in PARAMETER_HELP:
            kind["description"]=PARAMETER_HELP[name]
        if param.default is inspect.Parameter.empty:
            required.append(name)
        elif param.default is not None:
            kind["default"]=param.default
        properties[name]=kind
    return {"type":"object","properties":properties,"required":required,"additionalProperties":False}




class Engine:
    def __init__(self,data=None):
        self.data=Path(data).resolve() if data else data_directory()
        self.data.mkdir(parents=True,exist_ok=True)
        self.closing=False
        self.journal=Journal(self.data/"logs/actions.jsonl")
        self.run_lock=RunLock(self.data/"device.lock")
        self.browser=Browser(self.data)
        self.desktop=Desktop()
        self.demo=BrowserDemo(self)
        self.agents=Agents(self.data)
        self.workflows=Workflows(self)
        self.scheduler=Scheduler(self)
        self.tools={}
        self._register()

    def add(self,name,description,handler,mutates=False):
        self.tools[name]=Tool(name,description,handler,schema_for(handler),mutates)

    def _register(self):
        methods=[
            ("system_status","Detect OS, CPU, Python and bridge availability. Does not start recording.",self.status,False),
            ("system_stop","Set the persistent emergency stop flag. Current OS calls cannot be undone.",self.stop,True),
            ("system_resume","Clear the emergency stop flag after inspecting the device.",self.resume,True),
            ("system_launch","Launch an installed executable with a literal argument array; no shell interpolation.",self.launch,True),
            ("browser_connect","Connect explicitly to a loopback Chrome CDP endpoint.",self.browser.connect,True),
            ("browser_launch","Start installed Chrome with a dedicated automation profile and local debugging port.",self.browser.launch,True),
            ("browser_tabs","List browser targets and their IDs.",self.browser.tabs,False),
            ("browser_new_tab","Create a Chrome tab. Save targetId for subsequent actions.",self.browser.new_tab,True),
            ("browser_navigate","Navigate a tab and wait for document readiness.",self.browser.navigate,True),
            ("browser_snapshot","Read visible text and unique CSS selectors, including open shadow roots. frame_id reads a specific frame.",self.browser.snapshot,False),
            ("browser_click","Click one unique visible CSS selector with trusted CDP mouse input, in the top document, open shadow roots or a same-origin frame (frame).",self.browser.click,True),
            ("browser_fill","Replace an editable field and verify its value, also inside a same-origin frame (frame). Password values are not returned.",self.browser.fill,True),
            ("browser_key","Send a key; modifier mask: Alt=1 Ctrl=2 Meta=4 Shift=8.",self.browser.key,True),
            ("browser_select","Choose a native select option by value.",self.browser.select,True),
            ("browser_scroll","Scroll the top-frame viewport.",self.browser.scroll,True),
            ("browser_screenshot","Return the viewport as an MCP image. It may contain private on-screen information.",self.browser.screenshot,False),
            ("browser_evaluate","Run explicitly supplied JavaScript in a page or selected frame; may change page state.",self.browser.evaluate,True),
            ("browser_wait","Wait for a unique selector or a trusted JavaScript condition. Expressions can change page state.",self.browser.wait,True),
            ("browser_upload","Set a file input to explicit local files.",self.browser.upload,True),
            ("browser_downloads","Set a local download directory and enable progress events.",self.browser.downloads,True),
            ("browser_dialogs","Arm automatic handling of alert/confirm/prompt/beforeunload dialogs on a tab (accept, dismiss or off) and list recent dialog messages. Put it before the action that opens a dialog.",self.browser.dialogs,True),
            ("browser_events","Read buffered CDP notifications. Enable desired domains with browser_cdp first.",self.browser.events,False),
            ("browser_cdp","Call any CDP method with explicit params. Supports flattened session_id for attached targets, frames, network and dialogs.",self.browser.call,True),
            ("browser_demo_start","Watch a person work in one Chrome tab: record their clicks, typing, selections, uploads, Enter/Escape and navigation with unique selectors. Shows a visible badge; passwords are never captured.",self.demo.start,True),
            ("browser_demo_read","Follow a running Chrome demonstration live: read observed events from a cursor.",self.demo.read,False),
            ("browser_demo_stop","Stop the Chrome demonstration and convert it to a workflow: typed values become inputs, passwords become environment variables, untranslatable actions become manual steps.",self.demo.stop,True),
            ("desktop_windows","List visible Windows titles, handles, PIDs and executable paths.",self.desktop_windows,False),
            ("desktop_snapshot","Read Windows UI Automation elements and visible terminal text, where exposed by the application.",self.desktop_snapshot,False),
            ("desktop_screenshot","Capture the desktop or chosen window. Images are returned to the requesting agent.",self.desktop_screenshot,False),
            ("desktop_focus","Bring one exact window into the foreground.",self.desktop_focus,True),
            ("desktop_action","Act on one unique element: invoke, select, toggle, expand, collapse, focus, click, double_click, right_click.",self.desktop_action,True),
            ("desktop_fill","Set a Windows field. Optional explicit Unicode keyboard fallback requires result inspection.",self.desktop_fill,True),
            ("desktop_keys","Send a chord such as [CTRL,S] to an optional exact window.",self.desktop_keys,True),
            ("desktop_mouse","Use physical screenshot coordinates: move, click, right_click, double_click, drag, wheel.",self.desktop_mouse,True),
            ("recording_start","Start visible desktop recording for explicitly selected PIDs. Text and screenshots default off.",self.recording_start,True),
            ("recording_stop","Stop the owned desktop recording and save the encrypted session.",self.recording_stop,True),
            ("recording_status","Read the current desktop recorder state.",self.recording_status,False),
            ("agent_probe","Detect installed Claude, Codex, Cline, agy and configured custom CLI profiles.",self.agents.probe,False),
            ("agent_start","Start a managed non-interactive CLI task and capture its visible conversation. Uses the CLI's own login and permissions.",self.agents.start,True),
            ("agent_read","Poll a managed CLI conversation using its returned cursor.",self.agents.read,False),
            ("agent_list","List CLI sessions owned by this server instance.",self.agents.list,False),
            ("agent_stop","Stop an owned CLI process tree.",self.agents.stop,True),
            ("transcript_list","Find recent saved conversations of Claude Code, Codex, Cline and profiles with configured transcript folders, including CLI sessions the user opened. Returns paths, cwd and the first user message.",self.agents.transcripts,False),
            ("transcript_read","Read what was written in a saved CLI conversation. Poll again with next_cursor to follow a running session live. conversation_only drops metadata rows; private reasoning is never returned.",read_transcript,False),
            ("workflow_save","Validate and save a deterministic workflow; variables use ${name} or ${env:NAME}.",self.workflows.save,True),
            ("workflow_list","List saved deterministic workflows.",self.workflows.list,False),
            ("workflow_plan","Inspect a saved workflow's input requirements, blockers and possible write steps without running it.",self.workflows.plan,False),
            ("workflow_run","Run a saved workflow with checks, variables, branching, loops, bounded retries and an exclusive device lock.",self.workflows.run,True),
            ("workflow_start","Start a long workflow in the background so MCP requests do not need to wait. Poll workflow_status.",self.workflows.start,True),
            ("workflow_status","Read the status and result of an owned background workflow job.",self.workflows.status,False),
            ("workflow_cancel","Request cancellation of one owned background job at a checked boundary.",self.workflows.cancel,True),
            ("workflow_history","List persisted workflow run receipts, optionally by workflow name.",self.workflows.history.list,False),
            ("workflow_history_prune","Delete finished run receipts older than older_than_days, optionally for one workflow name. Running receipts are kept.",self.workflows.history.prune,True),
            ("workflow_run_info","Read one run receipt and its step metadata using a sequence cursor.",self.workflows.history.info,False),
            ("workflow_record_start","Record this agent's subsequent tool calls as a reusable workflow. Input values are parameterized by default.",self.workflows.record_start,True),
            ("workflow_record_stop","Save the agent action recording as an editable workflow definition.",self.workflows.record_stop,True),
            ("workflow_import_recording","Convert an encrypted desktop recording to a workflow. Unsupported actions remain explicit manual barriers.",self.workflows.import_recording,True),
            ("workflow_export","Export a saved workflow to inspectable Python source.",self.workflows.export,True),
            ("schedule_preview","Prepare an exact Windows Task Scheduler definition without registering it.",self.scheduler.preview,False),
            ("schedule_create","Register or replace an own-folder Windows scheduled task, pinned to the workflow digest. Times use the Windows local time zone.",self.scheduler.create,True),
            ("schedule_list","List only FullAutomation Windows scheduled tasks.",self.scheduler.list,False),
            ("schedule_remove","Remove one named FullAutomation scheduled task.",self.scheduler.remove,True),
            ("file_inspect","Read output file existence, size, modification time and optional SHA-256.",inspect_file,False),
            ("file_wait","Wait for a stable regular file. Use newer_than (Unix seconds) to reject an older output.",self.file_wait,False),
        ]
        for method in methods:
            self.add(*method)

    def call(self,name,args=None,record=True):
        if self.closing:
            raise AutomationError("Automation server is closing.")
        if name not in self.tools:
            raise AutomationError("Unknown tool: "+str(name))
        args={} if args is None else args
        tool=self.tools[name]
        check_schema(args,tool.schema)
        device_write = tool.mutates and name not in UNLOCKED_CONTROLS
        if device_write:
            self.check_stop()
        started=time.monotonic()
        try:
            if device_write and name!="workflow_start":
                with self.run_lock:
                    result=tool.handler(**args)
            else:
                result=tool.handler(**args)
            if record:
                self.workflows.capture(name,args,result)
            self.journal.add({"tool":name,"status":"ok","elapsed_ms":round((time.monotonic()-started)*1000)})
            return result
        except Exception as exc:
            self.journal.add({"tool":name,"status":"failed","error":str(exc)[:1500]})
            raise

    def check_stop(self):
        if self.closing:
            raise AutomationError("Automation server is closing.")
        if (self.data/"STOP").exists():
            raise AutomationError("Emergency stop is active. Inspect the device, then call system_resume.")

    def stop(self):
        (self.data/"STOP").touch()
        return {"stopped":True,"note":"Already-issued OS or browser actions cannot be undone."}

    def resume(self):
        (self.data/"STOP").unlink(missing_ok=True)
        return {"stopped":False}

    def status(self):
        return {"version":__version__,"os":platform.platform(),"cpu_count":os.cpu_count(),"python":sys.version.split()[0],
                "python_executable":sys.executable,"data_directory":str(self.data),"desktop_supported":os.name=="nt",
                "bridge_exists":self.desktop.executable.is_file(),"cdp_endpoint":self.browser.endpoint,"emergency_stop":(self.data/"STOP").exists(),
                "model":"External CLI owns model access. Claude profile requests claude-opus-5-5 without fallback.","tool_count":len(self.tools)}

    def launch(self,executable,arguments=None,cwd=None):
        arguments=arguments or []
        if not isinstance(arguments,list) or not all(isinstance(a,str) for a in arguments):
            raise AutomationError("arguments must be an array of strings.")
        directory=Path(cwd).resolve(strict=True) if cwd else ROOT
        process=subprocess.Popen(command_prefix(executable)+arguments,cwd=str(directory),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,shell=False)
        return {"pid":process.pid}

    def desktop_windows(self): return self.desktop.call("windows")
    def desktop_snapshot(self,window,limit=300): return self.desktop.call("snapshot",{"window":window,"limit":limit})
    def desktop_screenshot(self,window=None): return self.desktop.call("screenshot",{"window":window} if window else {})
    def desktop_focus(self,window): return self.desktop.call("focus",{"window":window})
    def desktop_action(self,selector,action="invoke"): return self.desktop.call("action",{"selector":selector,"action":action})
    def desktop_fill(self,selector,text,allow_keyboard=False): return self.desktop.call("fill",{"selector":selector,"text":text,"allow_keyboard":allow_keyboard})
    def desktop_keys(self,keys,window=None): return self.desktop.call("keys",{"keys":keys,**({"window":window} if window else {})})
    def desktop_mouse(self,x,y,action="click",to_x=0,to_y=0,delta=-120): return self.desktop.call("mouse",{"x":x,"y":y,"action":action,"to_x":to_x,"to_y":to_y,"delta":delta})
    def recording_start(self,pids,capture_text=False,screenshots=False): return self.desktop.call("record_start",{"pids":pids,"capture_text":capture_text,"screenshots":screenshots})
    def recording_stop(self): return self.desktop.call("record_stop")
    def recording_status(self): return self.desktop.call("record_status")

    def file_wait(self,path,timeout=60,stable_seconds=1,newer_than=None):
        if not 0<timeout<=120 or not 0<=stable_seconds<=timeout:
            raise AutomationError("Invalid file wait duration.")
        file=Path(path).expanduser().resolve()
        deadline=time.monotonic()+timeout
        previous=None
        since=time.monotonic()
        while time.monotonic()<deadline:
            self.check_stop()
            try:
                info=file.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise AutomationError("Expected a regular file.")
                if newer_than is not None and info.st_mtime <= newer_than:
                    previous=None
                    time.sleep(0.1)
                    continue
                current=(info.st_size,info.st_mtime_ns,info.st_ino,info.st_dev)
                if current==previous and time.monotonic()-since>=stable_seconds:
                    return {"path":str(file),"bytes":info.st_size,"modified_at":info.st_mtime,"stable":True}
                if current!=previous:
                    previous=current
                    since=time.monotonic()
            except FileNotFoundError:
                previous=None
            time.sleep(0.1)
        raise AutomationError("Output file did not become stable before timeout.")

    def close(self):
        self.closing=True
        self.agents.close()
        self.demo.close()
        self.browser.close()
        self.desktop.close()
        self.workflows.close()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()
