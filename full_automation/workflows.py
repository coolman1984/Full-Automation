from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import time
import threading
import uuid

from .common import AutomationError, ROOT, atomic_json, digest, read_json, slug
from .cdp import Browser

TOKEN = re.compile(r"\$\{([^{}]+)\}")
READ_ONLY = {"browser_tabs","browser_snapshot","desktop_windows","desktop_snapshot","browser_wait","system_status","file_wait"}
CONTROL = {"workflow_save","workflow_run","workflow_start","workflow_record_start","workflow_record_stop","workflow_import_recording","workflow_export","schedule_create","schedule_remove","agent_start","agent_stop","recording_start","recording_stop"}


def lookup(context, name):
    if name.startswith("env:"):
        key=name[4:]
        if key not in os.environ:
            raise AutomationError("Required environment variable is missing: "+key)
        return os.environ[key]
    current=context
    for key in name.split("."):
        if isinstance(current,list) and key.isdigit() and int(key)<len(current):
            current=current[int(key)]
        elif isinstance(current,dict) and key in current:
            current=current[key]
        else:
            raise AutomationError("Unresolved workflow variable: "+name)
    return current


def substitute(value, context):
    if isinstance(value,dict):
        return {k:substitute(v,context) for k,v in value.items()}
    if isinstance(value,list):
        return [substitute(v,context) for v in value]
    if not isinstance(value,str):
        return value
    match=TOKEN.fullmatch(value)
    if match:
        return copy.deepcopy(lookup(context,match.group(1)))
    return TOKEN.sub(lambda m: str(lookup(context,m.group(1))),value)


def validate(definition, tools):
    if not isinstance(definition,dict) or definition.get("version")!=1:
        raise AutomationError("Workflow version must be 1.")
    slug(definition.get("name",""))
    if "browser_endpoint" in definition:
        Browser._endpoint(definition["browser_endpoint"])
    required=definition.get("required_inputs",[])
    if not isinstance(required,list) or not all(isinstance(k,str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",k) for k in required):
        raise AutomationError("Invalid required_inputs.")
    total=0
    def check(steps,depth=0):
        nonlocal total
        if not isinstance(steps,list) or depth>8:
            raise AutomationError("Workflow nesting or step list is invalid.")
        for step in steps:
            total+=1
            if total>1000 or not isinstance(step,dict):
                raise AutomationError("Workflow is too large or contains an invalid step.")
            action=step.get("tool")
            if action=="foreach":
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",str(step.get("as","item"))):
                    raise AutomationError("Invalid loop variable.")
                if "items" not in step:
                    raise AutomationError("foreach requires items.")
                check(step.get("steps"),depth+1)
            elif action=="if":
                if not isinstance(step.get("condition"),dict):
                    raise AutomationError("if requires a condition.")
                check(step.get("then",[]),depth+1)
                check(step.get("else",[]),depth+1)
            elif action=="manual":
                pass
            elif action not in {"wait","assert"} and (action not in tools or action in CONTROL):
                raise AutomationError("Tool cannot be embedded in a deterministic workflow: "+str(action))
            if not isinstance(step.get("args",{}),dict):
                raise AutomationError("Step args must be an object.")
            retries=step.get("retries",0)
            if isinstance(retries,bool) or not isinstance(retries,int) or not 0<=retries<=3:
                raise AutomationError("Retries must be an integer from 0 to 3.")
            if retries and action not in READ_ONLY and step.get("idempotent") is not True:
                raise AutomationError("Retrying a write needs an explicit idempotent=true declaration.")
            if "save_as" in step and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",str(step["save_as"])):
                raise AutomationError("Invalid save_as name.")
    check(definition.get("steps"))
    if not definition["steps"]:
        raise AutomationError("Workflow has no steps.")
    return definition


class Workflows:
    def __init__(self, engine):
        self.engine=engine
        self.folder=engine.data/"workflows"
        self.recording=None
        self.aliases={}
        self.jobs={}
        self.jobs_lock=threading.Lock()
        self.threads=[]

    def save(self,definition):
        validate(definition,self.engine.tools)
        path=self.folder/(slug(definition["name"])+".json")
        atomic_json(path,definition)
        return {"path":str(path),"sha256":digest(definition),"steps":len(definition["steps"])}

    def load(self,name):
        return validate(read_json(self.folder/(slug(name)+".json")),self.engine.tools)

    def list(self):
        return [{"name":p.stem,"path":str(p)} for p in sorted(self.folder.glob("*.json"))]

    def run(self,name,variables=None,expected_sha256=None,timeout=1800):
        return self.run_definition(self.load(name),variables,expected_sha256,timeout)

    def start(self,name,variables=None,expected_sha256=None,timeout=1800):
        definition=self.load(name)
        with self.jobs_lock:
            if any(job["status"] in {"pending","running"} for job in self.jobs.values()):
                raise AutomationError("A background workflow is already active in this server.")
            if len(self.jobs)>=20:
                del self.jobs[next(iter(self.jobs))]
            identifier=uuid.uuid4().hex
            self.jobs[identifier]={"job_id":identifier,"name":name,"status":"pending","started":time.time()}
        def worker():
            with self.jobs_lock:self.jobs[identifier]["status"]="running"
            try:
                result=self.run_definition(definition,variables,expected_sha256,timeout)
                update={"status":"completed","result":result}
            except Exception as exc:
                update={"status":"failed","error":str(exc)}
            with self.jobs_lock:
                self.jobs[identifier].update(update)
                self.jobs[identifier]["finished"]=time.time()
        thread=threading.Thread(target=worker,daemon=True)
        self.threads.append(thread)
        thread.start()
        return {"job_id":identifier,"status":"accepted","note":"Poll workflow_status; use system_stop to stop at the next checked boundary."}

    def status(self,job_id):
        with self.jobs_lock:
            if job_id not in self.jobs:
                raise AutomationError("Unknown background workflow job.")
            return copy.deepcopy(self.jobs[job_id])

    def close(self):
        for thread in self.threads:
            thread.join(timeout=5)

    def run_definition(self,definition,variables=None,expected_sha256=None,timeout=1800):
        validate(definition,self.engine.tools)
        if expected_sha256 and digest(definition)!=expected_sha256:
            raise AutomationError("Workflow changed after scheduling. Review and update the scheduled task.")
        if not isinstance(variables or {},dict) or not 1<=timeout<=86400:
            raise AutomationError("Invalid workflow variables or timeout.")
        context=copy.deepcopy(variables or {})
        missing=[key for key in definition.get("required_inputs",[]) if key not in context]
        if missing:
            raise AutomationError("Missing workflow inputs: "+", ".join(missing))
        self.engine.check_stop()
        deadline=time.monotonic()+timeout
        count=0
        outputs={}
        def check():
            self.engine.check_stop()
            if time.monotonic()>deadline:
                raise AutomationError("Workflow exceeded its deadline.")
        def condition(spec):
            if "equals" in spec:
                return spec.get("actual")==spec["equals"]
            if "contains" in spec:
                return spec["contains"] in spec.get("actual",[])
            return bool(spec.get("actual"))
        def execute(steps):
            nonlocal count
            for step in steps:
                check()
                count+=1
                if count>10000:
                    raise AutomationError("Workflow exceeded the expanded step limit.")
                tool=step["tool"]
                if tool=="foreach":
                    items=substitute(step["items"],context)
                    if not isinstance(items,list) or len(items)>1000:
                        raise AutomationError("foreach requires an array of at most 1000 items.")
                    key=step.get("as","item")
                    existed=key in context
                    old=context.get(key)
                    try:
                        for item in items:
                            context[key]=item
                            execute(step["steps"])
                    finally:
                        if existed:
                            context[key]=old
                        else:
                            context.pop(key,None)
                    continue
                if tool=="if":
                    branch="then" if condition(substitute(step["condition"],context)) else "else"
                    execute(step.get(branch,[]))
                    continue
                if tool=="manual":
                    raise AutomationError("Manual step prevents unattended execution: "+str(step.get("description","Review the recording.")))
                args=substitute(step.get("args",{}),context)
                result=None
                for attempt in range(step.get("retries",0)+1):
                    try:
                        if tool=="wait":
                            seconds=args.get("seconds",1)
                            if not isinstance(seconds,(int,float)) or not 0<=seconds<=120:
                                raise AutomationError("Wait must be between 0 and 120 seconds.")
                            end=time.monotonic()+seconds
                            while time.monotonic()<end:
                                check()
                                time.sleep(min(0.1,max(0,end-time.monotonic())))
                            result={"waited":seconds}
                        elif tool=="assert":
                            if not condition(args):
                                raise AutomationError("Workflow assertion failed: "+str(args.get("label","condition")))
                            result={"passed":True}
                        else:
                            result=self.engine.call(tool,args,record=False)
                        break
                    except Exception:
                        if attempt>=step.get("retries",0):
                            raise
                        check()
                        time.sleep(0.2)
                if "save_as" in step:
                    context[step["save_as"]]=result
                    outputs[step["save_as"]]=result
        with self.engine.run_lock:
            self.engine.journal.add({"event":"workflow_start","name":definition["name"],"sha256":digest(definition)})
            try:
                if "browser_endpoint" in definition:
                    self.engine.call("browser_connect", {"endpoint": definition["browser_endpoint"]}, record=False)
                execute(definition["steps"])
            except Exception as exc:
                self.engine.journal.add({"event":"workflow_failed","name":definition["name"],"step":count,"error":str(exc)})
                raise
            self.engine.journal.add({"event":"workflow_complete","name":definition["name"],"steps":count})
        return {"status":"completed","steps":count,"outputs":outputs}

    def record_start(self,name,parameterize_inputs=True):
        if self.recording is not None:
            raise AutomationError("A workflow recording is already active.")
        self.recording={"version":1,"name":slug(name),"required_inputs":[],"steps":[]}
        self.parameterize=bool(parameterize_inputs)
        self.aliases={}
        return {"recording":name}

    def capture(self,tool,args,result):
        if self.recording is None or tool in CONTROL or tool.startswith(("workflow_","schedule_","agent_","system_","recording_")):
            return
        params=copy.deepcopy(args)
        if tool.startswith("browser_") and tool not in {"browser_launch", "browser_connect"}:
            if not any(step["tool"] in {"browser_launch", "browser_connect"} for step in self.recording["steps"]):
                self.recording.setdefault("browser_endpoint", self.engine.browser.endpoint)
        required=self.recording["required_inputs"]
        if "target_id" in params:
            target=params["target_id"]
            if target in self.aliases:
                params["target_id"]=self.aliases[target]
            else:
                name="target_"+str(len(required)+1)
                required.append(name)
                params["target_id"]="${"+name+"}"
                self.aliases[target]=params["target_id"]
        if self.parameterize:
            for key in ("text","value"):
                if key in params:
                    name="input_"+str(len(required)+1)
                    required.append(name)
                    params[key]="${"+name+"}"
        step={"tool":tool,"args":params}
        if tool=="browser_new_tab" and isinstance(result,dict) and "targetId" in result:
            name="tab_"+str(len(self.recording["steps"])+1)
            step["save_as"]=name
            self.aliases[result["targetId"]]="${"+name+".targetId}"
        self.recording["steps"].append(step)

    def record_stop(self):
        if self.recording is None:
            raise AutomationError("No workflow recording is active.")
        definition=self.recording
        result=self.save(definition)
        self.recording=None
        return {**result,"definition":definition}

    def import_recording(self,path,name):
        recorded=self.engine.desktop.call("record_export",{"path":path})
        definition={"version":1,"name":slug(name),"required_inputs":[],"steps":[]}
        for step in recorded["Steps"]:
            target=step.get("Target")
            if not step.get("Replayable") or not target:
                definition["steps"].append({"tool":"manual","description":step.get("Description","Unrecognized action")})
                continue
            selector={"window":{"title":target["Window"],"path":target["AppPath"]},"id":target["Id"],"name":target["Name"],"class_name":target["Class"],"control_type":target["ControlType"],"parent_id":target["ParentId"]}
            if step["Kind"]=="click":
                action="invoke" if step["Pattern"]=="invoke" else "select"
                definition["steps"].append({"tool":"desktop_action","args":{"selector":selector,"action":action}})
            else:
                key="input_"+str(len(definition["required_inputs"])+1)
                definition["required_inputs"].append(key)
                definition["steps"].append({"tool":"desktop_fill","args":{"selector":selector,"text":"${"+key+"}"}})
        return {**self.save(definition),"definition":definition}

    def export(self,name):
        definition=self.load(name)
        path=self.engine.data/"scripts"/(slug(name)+".py")
        path.parent.mkdir(parents=True,exist_ok=True)
        # repr of JSON text is Python-string escaping, never shell escaping or eval.
        source=("# Generated deterministic workflow. Review before running.\nimport json, sys\n"
                +"sys.path.insert(0, "+repr(str(ROOT))+")\nfrom full_automation.engine import Engine\n"
                +"definition=json.loads("+repr(json.dumps(definition,ensure_ascii=False))+")\n"
                +"variables=json.loads(sys.argv[1]) if len(sys.argv)>1 else {}\n"
                +"with Engine("+repr(str(self.engine.data))+") as engine:\n    print(json.dumps(engine.workflows.run_definition(definition, variables),ensure_ascii=False))\n")
        path.write_text(source,encoding="utf-8")
        return {"path":str(path),"sha256":digest(definition)}
