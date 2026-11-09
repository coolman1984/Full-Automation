from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from .common import AutomationError, ROOT, atomic_json, digest, slug


class Scheduler:
    def __init__(self,engine):
        self.engine=engine

    def invoke(self,operation,**values):
        if os.name!="nt":
            raise AutomationError("Windows Task Scheduler requires Windows. Use schedule_preview here.")
        result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-File",str(ROOT/"scripts/Task-Scheduler.ps1")],
            input=json.dumps({"operation":operation,**values},ensure_ascii=False),capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=30)
        if result.returncode:
            raise AutomationError("Task Scheduler failed: "+result.stderr[-3000:])
        return json.loads(result.stdout.lstrip("\ufeff"))

    def preview(self,name,workflow,at="09:00",frequency="daily",weekdays=None,variables=None):
        slug(name)
        definition=self.engine.workflows.load(workflow)
        if frequency not in {"daily","weekly","once"}:
            raise AutomationError("Frequency must be daily, weekly or once.")
        if not re.fullmatch(r"(?:\d{4}-\d\d-\d\dT)?\d\d:\d\d(?::\d\d)?",at):
            raise AutomationError("Use local HH:MM or YYYY-MM-DDTHH:MM:SS.")
        if frequency=="once" and "T" not in at:
            raise AutomationError("One-time schedules require a full local date and time.")
        import datetime
        try:
            datetime.datetime.fromisoformat(at) if "T" in at else datetime.time.fromisoformat(at)
        except ValueError as exc:
            raise AutomationError("Invalid schedule date or time.") from exc
        days=weekdays or ["Monday"]
        if not isinstance(days,list) or not days or any(d not in {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"} for d in days):
            raise AutomationError("Use full English weekday names.")
        if not isinstance(variables or {},dict):
            raise AutomationError("Schedule variables must be an object.")
        missing=[x for x in definition.get("required_inputs",[]) if x not in (variables or {})]
        if missing:
            raise AutomationError("Schedule is missing required inputs: "+", ".join(missing))
        # Keep a failed replacement from changing the inputs of an existing task.
        variables_revision=digest(variables or {})[:16]
        variables_file=self.engine.data/"schedules"/(name+".variables-"+variables_revision+".json")
        argv=[str(ROOT/"main.py"),"--data-dir",str(self.engine.data),"workflow","run",workflow,"--variables-file",str(variables_file),"--expected-sha256",digest(definition)]
        return {"name":name,"workflow":workflow,"frequency":frequency,"at":at,"weekdays":days,
                "python":sys.executable,"arguments":subprocess.list2cmdline(argv),"directory":str(ROOT),
                "data_directory":str(self.engine.data),"variables_file":str(variables_file),"workflow_sha256":digest(definition),
                "timezone":"Windows local time zone; desktop tasks require a signed-in, unlocked session."}

    def create(self,name,workflow,at="09:00",frequency="daily",weekdays=None,variables=None):
        preview=self.preview(name,workflow,at,frequency,weekdays,variables)
        if os.name!="nt":
            raise AutomationError("Schedule creation requires Windows.")
        atomic_json(Path(preview["variables_file"]),variables or {})
        result=self.invoke("create",**preview)
        atomic_json(self.engine.data/"schedules"/(name+".json"),preview)
        return result

    def list(self):
        return self.invoke("list")

    def remove(self,name):
        return self.invoke("remove",name=slug(name))
