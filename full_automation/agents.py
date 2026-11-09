"""Own and observe CLI sessions; never scrape credentials or bypass CLI permissions."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .common import AutomationError, atomic_json, read_json, redact

BUILTINS = {
    "claude": {"command":"claude", "args":["--print","--output-format","stream-json","--verbose","--model","claude-opus-5-5","{prompt}"], "format":"jsonl"},
    "codex": {"command":"codex", "args":["exec","--json","--skip-git-repo-check","{prompt}"], "format":"jsonl"},
    "cline": {"command":"cline", "args":["--json","{prompt}"], "format":"jsonl"},
    "agy": {"command":"agy", "args":["--print","{prompt}","--output-format","stream-json"], "format":"jsonl"},
}


def _is_node_script(script: Path) -> bool:
    """A JS file, or an extension-less file whose shebang names node."""
    if script.suffix.lower() in {".js", ".mjs", ".cjs"}:
        return True
    try:
        with script.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"node" in first


def command_prefix(command: str) -> list[str]:
    path = shutil.which(command) or (command if Path(command).is_file() else None)
    if not path:
        raise AutomationError("CLI not installed or not on PATH: " + command)
    path = str(Path(path).resolve())
    if os.name == "nt" and Path(path).suffix.lower() in {".cmd", ".bat"}:
        # Resolve npm shims directly into node + JS. Do not interpolate prompts in cmd.exe.
        contents = Path(path).read_text(encoding="utf-8-sig", errors="replace")[:32768]
        match = re.search(r"(?:%~dp0|%dp0%)[\\/]?([^\"\r\n]+?\.(?:m?js))", contents, re.I)
        if not match:
            # npm also emits shims for extension-less bin scripts, e.g. node_modules\cline\bin\cline.
            match = re.search(r"\"(?:%~dp0|%dp0%)[\\/]?(node_modules[\\/][^\"\r\n%*]+)\"", contents, re.I)
        if match:
            folder = Path(path).parent.resolve()
            script = (folder / match.group(1).replace("\\", "/")).resolve()
            local_node = folder / "node.exe"
            node = str(local_node) if local_node.is_file() else shutil.which("node")
            if node and script.is_file() and script.is_relative_to(folder) and _is_node_script(script):
                return [node, str(script)]
        raise AutomationError("Cannot safely launch this batch wrapper. Configure its native executable or node script explicitly.")
    return [path]


def visible_event(value):
    """Normalize visible conversation text, not private reasoning or authentication blobs."""
    if not isinstance(value, dict):
        return {"type":"text","text":str(value)}
    kind = str(value.get("type", "event"))
    if kind in {"thinking", "reasoning", "reasoning_delta"}:
        return {"type":kind,"text":"[Reasoning block omitted]"}
    message = value.get("message") if isinstance(value.get("message"), dict) else value
    role = message.get("role", value.get("role", kind))
    content = message.get("content", value.get("content"))
    if isinstance(content, list):
        text = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") in {"text", "input_text", "output_text"})
        tools = [{"name":x.get("name"),"id":x.get("id")} for x in content if isinstance(x,dict) and x.get("type")=="tool_use"]
        return redact({"type":kind,"role":role,"text":text[:200000],"tools":tools})
    if isinstance(content, str):
        return redact({"type":kind,"role":role,"text":content[:200000]})
    if isinstance(value.get("item"), dict):
        item = value["item"]
        return redact({"type":kind,"role":item.get("type"),"text":str(item.get("text", item.get("aggregated_output", "")))[:200000],"status":item.get("status")})
    event = value.get("event")
    if isinstance(event, dict):
        return redact({"type":kind,"role":event.get("role", "assistant"),"text":str(event.get("text", ""))[:200000],"event_type":event.get("type")})
    if isinstance(value.get("payload"), dict):
        return visible_event(value["payload"])
    text = value.get("text", value.get("result", value.get("error", value.get("message") if isinstance(value.get("message"), str) else "")))
    return redact({"type":kind,"role":role,"text":str(text)[:200000],"session_id":value.get("session_id",value.get("thread_id"))})


class ManagedSession:
    def __init__(self, identifier, process, folder, prompt, timeout):
        self.id, self.process, self.folder = identifier, process, folder
        self.created = time.time()
        self.reason = None
        self.events = deque(maxlen=5000)
        self.counter = 0
        self.bytes = 0
        self.lock = threading.Lock()
        self.add({"type":"user","role":"user","text":prompt})
        self.readers = []
        for stream, name in ((process.stdout,"stdout"),(process.stderr,"stderr")):
            reader=threading.Thread(target=self.pump,args=(stream,name),daemon=True)
            reader.start()
            self.readers.append(reader)
        self.watcher=threading.Thread(target=self.watch,args=(timeout,),daemon=True)
        self.watcher.start()

    def add(self, event):
        with self.lock:
            self.counter += 1
            record = {"seq":self.counter,"time":time.time(),**redact(event)}
            self.events.append(record)
            with (self.folder/"events.jsonl").open("a",encoding="utf-8") as file:
                file.write(json.dumps(record,ensure_ascii=False)+"\n")

    def pump(self, stream, name):
        try:
            for raw in iter(lambda: stream.readline(262144), ""):
                self.bytes += len(raw.encode("utf-8"))
                if self.bytes > 32*1024*1024:
                    self.stop("output_limit")
                    return
                raw = raw.rstrip("\r\n")
                try:
                    event = visible_event(json.loads(raw))
                except ValueError:
                    event = {"type":name,"text":raw}
                self.add({"stream":name,**event})
        except (OSError, ValueError) as exc:
            self.add({"type":"reader_error","text":str(exc)})
        finally:
            stream.close()

    def watch(self, timeout):
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop("timeout")
        for reader in self.readers:
            if reader is not threading.current_thread():
                reader.join(timeout=3)
        atomic_json(self.folder/"status.json",self.status())

    def stop(self, reason="stopped"):
        if self.process.poll() is not None:
            return self.status()
        self.reason=reason
        if os.name=="nt":
            subprocess.run(["taskkill.exe","/PID",str(self.process.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        else:
            try:
                os.killpg(self.process.pid,signal.SIGTERM)
                self.process.wait(timeout=2)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL)
        return self.status()

    def status(self):
        return {"session_id":self.id,"pid":self.process.pid,"running":self.process.poll() is None,
                "exit_code":self.process.poll(),"reason":self.reason,"events":self.counter,"created":self.created}

    def read(self, cursor=0, limit=100):
        if not isinstance(cursor,int) or cursor<0 or not isinstance(limit,int) or not 1<=limit<=500:
            raise AutomationError("Invalid transcript cursor or limit.")
        with self.lock:
            rows = [e for e in self.events if e["seq"]>cursor][:limit]
            oldest = self.events[0]["seq"] if self.events else 1
        return {**self.status(),"events":rows,"next_cursor":rows[-1]["seq"] if rows else cursor,"gap":cursor<oldest-1}


class Agents:
    def __init__(self, data: Path):
        self.data=data
        self.profiles={name:dict(profile) for name,profile in BUILTINS.items()}
        config=data/"agents.json"
        if config.exists():
            overrides=read_json(config)
            if not isinstance(overrides,dict):
                raise AutomationError("agents.json must contain an object of CLI profiles.")
            self.profiles.update(overrides)
        self.sessions={}

    PROBE_TIMEOUT=45

    def probe(self):
        # Some CLIs (Cline behind a corporate proxy: ~28 s) are slow to print a version, so the
        # profiles are probed in parallel with a generous limit instead of serially at 10 s each.
        def one(name,profile):
            try:
                prefix=command_prefix(profile["command"])
                flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
                info=subprocess.run(prefix+["--version"],capture_output=True,text=True,encoding="utf-8",errors="replace",
                                    timeout=self.PROBE_TIMEOUT,stdin=subprocess.DEVNULL,creationflags=flags)
                return {"agent":name,"available":info.returncode==0,"command":prefix,"version":redact((info.stdout+info.stderr)[:1500]),"configured_args":profile.get("args",[])}
            except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired,AutomationError) as exc:
                return {"agent":name,"available":False,"error":str(exc)}
        items=list(self.profiles.items())
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(8,len(items))) as pool:
            return list(pool.map(lambda item: one(*item),items))

    def start(self,agent,prompt,cwd,timeout=1800,model=None):
        if agent not in self.profiles:
            raise AutomationError("Unknown agent profile. Configure agents.json or use a built-in profile.")
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>100000:
            raise AutomationError("Prompt must contain 1–100000 characters.")
        if not 1<=timeout<=86400:
            raise AutomationError("CLI timeout must be between 1 second and 24 hours.")
        if sum(s.process.poll() is None for s in self.sessions.values())>=2:
            raise AutomationError("Two managed CLI sessions are already running.")
        folder=Path(cwd).expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise AutomationError("cwd must be a directory.")
        profile=self.profiles[agent]
        template=profile.get("args")
        if not isinstance(template,list) or not all(isinstance(a,str) for a in template) or "{prompt}" not in template:
            raise AutomationError("Agent args must be a string array containing a separate {prompt} argument.")
        args=list(template)
        if model is not None:
            if not isinstance(model,str) or not model or model.startswith("-"):
                raise AutomationError("Invalid model identifier.")
            if "--model" in args:
                args[args.index("--model")+1]=model
            else:
                args[0:0]=["--model",model]
        # Avoid a prompt beginning with a flag being reinterpreted by a CLI parser.
        prompt_argument = " " + prompt if prompt.startswith("-") else prompt
        args=[prompt_argument if a=="{prompt}" else a for a in args]
        command=command_prefix(profile["command"])+args
        env=os.environ.copy()
        # The user's existing CLI login/config is used. No API credentials are read or added.
        identifier=uuid.uuid4().hex
        session_folder=self.data/"agents"/identifier
        session_folder.mkdir(parents=True)
        atomic_json(session_folder/"meta.json",{"session_id":identifier,"agent":agent,"cwd":str(folder),"created":time.time()})
        options={"cwd":str(folder),"env":env,"stdin":subprocess.DEVNULL,"stdout":subprocess.PIPE,"stderr":subprocess.PIPE,"text":True,"encoding":"utf-8","errors":"replace","bufsize":1,"shell":False}
        if os.name=="nt":
            options["creationflags"]=subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"]=True
        process=subprocess.Popen(command,**options)
        session=ManagedSession(identifier,process,session_folder,prompt,timeout)
        self.sessions[identifier]=session
        return session.status()

    def read(self,session_id,cursor=0,limit=100):
        if session_id not in self.sessions:
            raise AutomationError("Session is not owned by this server. Read a saved transcript file explicitly.")
        return self.sessions[session_id].read(cursor,limit)

    def stop(self,session_id):
        if session_id not in self.sessions:
            raise AutomationError("Session is not owned by this server.")
        return self.sessions[session_id].stop()

    def list(self):
        return [s.status() for s in self.sessions.values()]

    def transcripts(self,agent=None,limit=20):
        return list_transcripts(self.profiles,agent,limit)

    def close(self):
        for session in self.sessions.values():
            session.stop("server_closed")
            for reader in session.readers:
                reader.join(timeout=4)
            session.watcher.join(timeout=4)


CREDENTIAL_NAMES = ("auth.json", "credentials", "token", "secret", "oauth", "keychain")
TRANSCRIPT_SUFFIXES = {".jsonl", ".log", ".txt", ".json"}
CLINE_ROLES = {"task": "user", "user_feedback": "user", "text": "assistant", "completion_result": "assistant", "followup": "assistant"}
MAX_TRANSCRIPT_FILES = 5000


def _credential_like(path: Path) -> bool:
    return any(token in path.name.lower() for token in CREDENTIAL_NAMES)


def _vscode_storage(home: Path):
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return base / "Code/User/globalStorage/saoudrizwan.claude-dev/tasks"


def transcript_roots(profiles=None, home=None):
    """Known conversation folders only. Login stores in the same parent folders are never matched."""
    home = Path(home) if home else Path.home()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    roots = [("claude", claude / "projects", "*/*.jsonl"),
             ("codex", codex / "sessions", "**/rollout-*.jsonl"),
             ("cline", home / ".cline/data/tasks", "*/ui_messages.json"),
             ("cline", _vscode_storage(home), "*/ui_messages.json")]
    for name, profile in (profiles or {}).items():
        entries = profile.get("transcripts", []) if isinstance(profile, dict) else []
        if not isinstance(entries, list):
            raise AutomationError("Profile transcripts must be a list of {root, pattern} objects: " + str(name))
        for entry in entries:
            root = Path(str(entry.get("root", ""))).expanduser() if isinstance(entry, dict) else None
            pattern = entry.get("pattern") if isinstance(entry, dict) else None
            if (root is None or not root.is_absolute() or not isinstance(pattern, str) or ".." in pattern
                    or Path(pattern).suffix.lower() not in TRANSCRIPT_SUFFIXES):
                raise AutomationError("Invalid transcript root or pattern for profile: " + str(name))
            roots.append((name, root, pattern))
    return roots


def _preview(file: Path):
    """First visible user message and working folder, read from the start of the file only."""
    title, cwd = None, None
    try:
        if file.suffix.lower() == ".json":
            if file.stat().st_size > 8_000_000:
                return None, None
            rows = read_json(file)
            rows = rows if isinstance(rows, list) else [rows]
        else:
            with file.open("rb") as stream:
                chunk = stream.read(262144)
            rows = []
            for line in chunk.splitlines()[:400]:
                try:
                    rows.append(json.loads(line.decode("utf-8-sig", errors="replace")))
                except ValueError:
                    continue
    except (OSError, ValueError, AutomationError):
        return None, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if cwd is None:
            payload = row.get("payload")
            cwd = row.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
        if title is None:
            event = conversation_event(row)
            if event and event.get("role") == "user" and event.get("text", "").strip():
                title = event["text"].strip()[:200]
        if title and cwd:
            break
    return title, cwd


def list_transcripts(profiles=None, agent=None, limit=20, home=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise AutomationError("Transcript list limit must be 1 to 200.")
    found = []
    for name, root, pattern in transcript_roots(profiles, home):
        if agent is not None and name != agent:
            continue
        if not root.is_dir():
            continue
        for file in root.glob(pattern):
            if len(found) >= MAX_TRANSCRIPT_FILES:
                break
            if file.is_file() and not file.is_symlink() and not _credential_like(file):
                found.append((name, file, file.stat()))
    found.sort(key=lambda item: item[2].st_mtime, reverse=True)
    rows = []
    for name, file, info in found[:limit]:
        title, cwd = _preview(file)
        rows.append(redact({"agent": name, "path": str(file), "modified_at": info.st_mtime, "bytes": info.st_size,
                            "first_user_message": title, "cwd": cwd}))
    return {"transcripts": rows, "scanned": len(found),
            "note": "Follow one live with transcript_read: pass next_cursor back on each poll. Only known conversation folders are searched."}


def conversation_event(row):
    """Normalize one saved row of a Claude Code, Codex or Cline conversation; None for metadata rows."""
    if not isinstance(row, dict):
        return None
    kind = row.get("type")
    if "sessionId" in row or kind in {"user", "assistant"} and isinstance(row.get("message"), dict):
        # Claude Code project log: only the user/assistant messages are the conversation.
        if kind not in {"user", "assistant"} or not isinstance(row.get("message"), dict):
            return None
        event = visible_event(row)
        if row.get("isSidechain"):
            event["sidechain"] = True
        time_value = row.get("timestamp")
    elif isinstance(row.get("payload"), dict) and "timestamp" in row:
        # Codex rollout: chat text is in event_msg rows, tool calls in response_item rows.
        payload = row["payload"]
        subtype = payload.get("type")
        if kind == "event_msg" and subtype in {"user_message", "agent_message"}:
            event = redact({"type": subtype, "role": "user" if subtype == "user_message" else "assistant",
                            "text": str(payload.get("message", ""))[:200000]})
        elif kind == "response_item" and subtype in {"function_call", "custom_tool_call", "local_shell_call"}:
            event = {"type": "tool_call", "role": "assistant", "text": "",
                     "tools": [{"name": payload.get("name") or subtype, "id": payload.get("call_id")}]}
        else:
            return None
        time_value = row.get("timestamp")
    elif "ts" in row and ("say" in row or "ask" in row):
        subtype = row.get("say") or row.get("ask")
        if subtype not in CLINE_ROLES:
            return None
        event = redact({"type": kind, "role": CLINE_ROLES[subtype], "subtype": subtype, "text": str(row.get("text", ""))[:200000]})
        time_value = row.get("ts")
    else:
        event = visible_event(row)
        time_value = row.get("timestamp")
    if not str(event.get("text", "")).strip() and not event.get("tools"):
        return None
    if time_value is not None:
        event["time"] = time_value
    return event


def read_transcript(path,cursor=0,limit=100,conversation_only=True):
    file=Path(path).expanduser().resolve(strict=True)
    if file.suffix.lower() not in TRANSCRIPT_SUFFIXES:
        raise AutomationError("Choose a transcript JSONL, JSON, LOG or TXT file.")
    if _credential_like(file):
        raise AutomationError("This looks like a credential file, not a conversation transcript.")
    if isinstance(cursor,bool) or not isinstance(cursor,int) or cursor<0 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=500:
        raise AutomationError("Invalid transcript cursor or limit.")
    normalize=(lambda row: conversation_event(row)) if conversation_only else visible_event
    if file.suffix.lower()==".json":
        data=read_json(file)
        rows=data if isinstance(data,list) else [data]
        events=[]
        position=cursor
        while position<len(rows) and len(events)<limit:
            event=normalize(rows[position])
            position+=1
            if event is not None:
                events.append(event)
        return {"events":events,"next_cursor":position,"cursor_unit":"items","has_more":position<len(rows)}
    rows=[]
    with file.open("rb") as stream:
        stream.seek(cursor)
        budget=0
        while len(rows)<limit:
            before=stream.tell()
            raw=stream.readline(512001)
            if not raw:
                break
            if len(raw)>512000:
                raise AutomationError("A transcript line exceeds the supported limit.")
            # Don't consume a partially written JSON line; let a later poll retry it.
            if file.suffix.lower()==".jsonl" and not raw.endswith(b"\n"):
                stream.seek(before)
                break
            budget+=len(raw)
            if budget>2_000_000:
                stream.seek(before)
                break
            text=raw.decode("utf-8-sig",errors="replace").rstrip("\r\n")
            try:
                event=normalize(json.loads(text))
            except ValueError:
                event=redact({"type":"text","text":text}) if text.strip() else None
            if event is not None:
                rows.append(event)
        next_cursor=stream.tell()
        has_more=bool(stream.read(1))
    return {"events":rows,"next_cursor":next_cursor,"cursor_unit":"bytes","has_more":has_more}
