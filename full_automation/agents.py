"""Own and observe CLI sessions; never scrape credentials or bypass CLI permissions."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
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


def command_prefix(command: str) -> list[str]:
    path = shutil.which(command) or (command if Path(command).is_file() else None)
    if not path:
        raise AutomationError("CLI not installed or not on PATH: " + command)
    path = str(Path(path).resolve())
    if os.name == "nt" and Path(path).suffix.lower() in {".cmd", ".bat"}:
        # Resolve npm shims directly into node + JS. Do not interpolate prompts in cmd.exe.
        contents = Path(path).read_text(encoding="utf-8-sig", errors="replace")[:32768]
        match = re.search(r"(?:%~dp0|%dp0%)[\\/]?([^\"\r\n]+?\.(?:m?js))", contents, re.I)
        if match:
            script = Path(path).parent / match.group(1).replace("\\", "/")
            local_node = Path(path).parent / "node.exe"
            node = str(local_node) if local_node.is_file() else shutil.which("node")
            if node and script.is_file():
                return [node, str(script.resolve())]
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
    text = value.get("text", value.get("result", value.get("error", "")))
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

    def probe(self):
        result=[]
        for name,profile in self.profiles.items():
            try:
                prefix=command_prefix(profile["command"])
                info=subprocess.run(prefix+["--version"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=10)
                result.append({"agent":name,"available":info.returncode==0,"command":prefix,"version":redact((info.stdout+info.stderr)[:1500]),"configured_args":profile.get("args",[])})
            except (OSError,ValueError,KeyError,subprocess.TimeoutExpired,AutomationError) as exc:
                result.append({"agent":name,"available":False,"error":str(exc)})
        return result

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

    def close(self):
        for session in self.sessions.values():
            session.stop("server_closed")
            for reader in session.readers:
                reader.join(timeout=4)
            session.watcher.join(timeout=4)


def read_transcript(path,cursor=0,limit=100):
    file=Path(path).expanduser().resolve(strict=True)
    if file.suffix.lower() not in {".jsonl",".log",".txt",".json"}:
        raise AutomationError("Choose a transcript JSONL, JSON, LOG or TXT file.")
    if any(token in file.name.lower() for token in ("auth.json","credentials","token","secret")):
        raise AutomationError("This looks like a credential file, not a conversation transcript.")
    if not isinstance(cursor,int) or cursor<0 or not isinstance(limit,int) or not 1<=limit<=500:
        raise AutomationError("Invalid transcript cursor or limit.")
    if file.suffix.lower()==".json":
        data=read_json(file)
        rows=data if isinstance(data,list) else [data]
        selected=rows[cursor:cursor+limit]
        return {"events":[visible_event(e) for e in selected],"next_cursor":cursor+len(selected),"cursor_unit":"items"}
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
                rows.append(visible_event(json.loads(text)))
            except ValueError:
                rows.append(redact({"type":"text","text":text}))
        next_cursor=stream.tell()
    return {"events":rows,"next_cursor":next_cursor,"cursor_unit":"bytes"}
