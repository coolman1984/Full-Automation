from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading

from .common import AutomationError, ROOT


class Desktop:
    def __init__(self, executable=None):
        self.executable=Path(executable) if executable else ROOT/"bin/WindowsBridge.exe"
        self.process=None
        self.responses=queue.Queue()
        self.counter=0
        self.lock=threading.Lock()

    def _start(self):
        if os.name!="nt":
            raise AutomationError("Desktop control requires Windows 11. Browser and workflow tests can run on other systems.")
        if not self.executable.is_file():
            raise AutomationError("Windows bridge is missing. Run scripts/Build-Windows.ps1.")
        self.responses=queue.Queue()
        self.process=subprocess.Popen([str(self.executable)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            encoding="utf-8",text=True,bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW)
        current=self.process
        responses=self.responses
        def reader():
            try:
                for line in current.stdout:
                    if len(line)>64*1024*1024:
                        responses.put(AutomationError("Bridge response exceeded size limit."))
                        break
                    try:
                        responses.put(json.loads(line))
                    except ValueError:
                        responses.put(AutomationError("Bridge returned invalid JSON."))
            finally:
                responses.put(AutomationError("Windows bridge disconnected."))
        threading.Thread(target=reader,daemon=True).start()

    def call(self,method,params=None,timeout=30):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            self.counter+=1
            request={"id":self.counter,"method":method,"params":params or {}}
            try:
                self.process.stdin.write(json.dumps(request,ensure_ascii=False,allow_nan=False)+"\n")
                self.process.stdin.flush()
                response=self.responses.get(timeout=timeout)
                if isinstance(response,Exception):
                    raise response
                if response.get("id")!=self.counter:
                    raise AutomationError("Bridge response ID mismatch.")
                if "error" in response:
                    raise AutomationError(response["error"])
                return response.get("result")
            except (queue.Empty,BrokenPipeError) as exc:
                self.close(force=True)
                raise AutomationError("Windows operation timed out. Inspect state before retrying; the action may have occurred.") from exc

    def close(self,force=False):
        if self.process and self.process.poll() is None:
            if force:
                self.process.kill()
            else:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process.wait(timeout=5)
        self.process=None
