import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from full_automation.agents import Agents, read_transcript, visible_event
from full_automation.common import AutomationError, RunLock, atomic_json, digest, redact, slug
from full_automation.engine import Engine
from full_automation.mcp import MCPServer
from full_automation.workflows import substitute, validate


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.data=Path(self.temp.name)
        self.engine=Engine(self.data)
        self.calls=[]
        def echo(value):
            self.calls.append(value)
            return {"value":value}
        self.engine.add("echo","test-only deterministic action",echo,True)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def recipe(self,steps,**extra):
        return {"version":1,"name":"test","steps":steps,**extra}

    def test_workflow_loop_branch_typed_variables(self):
        recipe=self.recipe([
            {"tool":"foreach","items":"${rows}","as":"row","steps":[
                {"tool":"if","condition":{"actual":"${row.enabled}","equals":True},"then":[
                    {"tool":"echo","args":{"value":"${row.value}"},"save_as":"last"}]}]},
            {"tool":"assert","args":{"actual":"${last.value}","equals":7}}
        ],required_inputs=["rows"])
        result=self.engine.workflows.run_definition(recipe,{"rows":[{"enabled":False,"value":4},{"enabled":True,"value":7}]})
        self.assertEqual(self.calls,[7])
        self.assertEqual(result["status"],"completed")

    def test_missing_input_stops_before_effect(self):
        with self.assertRaises(AutomationError):
            self.engine.workflows.run_definition(self.recipe([{"tool":"echo","args":{"value":1}}],required_inputs=["missing"]))
        self.assertFalse(self.calls)

    def test_manual_step_fails_not_silently_skipped(self):
        definition = self.recipe([{"tool":"echo","args":{"value":1}}, {"tool":"manual"}])
        self.engine.workflows.save(definition)
        self.assertFalse(self.engine.call("workflow_plan", {"name": "test"})["ready"])
        with self.assertRaisesRegex(AutomationError,"Manual step"):
            self.engine.workflows.run_definition(definition)
        with self.assertRaisesRegex(AutomationError,"Manual step"):
            self.engine.scheduler.preview("morning", "test")
        self.assertFalse(self.calls)

    def test_workflow_rejects_bad_later_args_before_any_write(self):
        definition = self.recipe([{"tool":"echo","args":{"value":1}}, {"tool":"browser_click","args":{"target_id":"x"}}])
        with self.assertRaisesRegex(AutomationError, "missing selector"):
            self.engine.workflows.run_definition(definition)
        self.assertFalse(self.calls)

    def test_run_history_persists_retry_and_failure_without_payloads(self):
        def flaky(value):
            self.calls.append(value)
            if len(self.calls) == 1:
                raise AutomationError("synthetic failure")
            return {"value": value}
        self.engine.add("flaky", "test retry", flaky, True)
        recipe = self.recipe([
            {"tool":"flaky","args":{"value":"private output"},"idempotent":True,"retries":1},
            {"tool":"assert","args":{"actual":False,"label":"private condition"}},
        ])
        with self.assertRaises(AutomationError):
            self.engine.workflows.run_definition(recipe)
        with Engine(self.data) as fresh:
            run, = fresh.call("workflow_history", {"name": "test"})
            page = fresh.call("workflow_run_info", {"run_id": run["run_id"], "limit": 1})
            next_page = fresh.call("workflow_run_info", {"run_id": run["run_id"], "cursor": page["next_cursor"]})
        self.assertEqual(run["status"], "failed")
        self.assertTrue(page["has_more"])
        self.assertEqual(page["steps"][0]["attempts"], 2)
        self.assertEqual(next_page["steps"][0]["status"], "failed")
        self.assertNotIn("private", json.dumps([page, next_page]))

    def test_cancel_only_owned_workflow_and_persists_cancelled_status(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"seconds":20}},
                                               {"tool":"echo","args":{"value":"too late"}}]))
        job_id = self.engine.call("workflow_start", {"name":"test"})["job_id"]
        self.assertTrue(self.engine.call("workflow_cancel", {"job_id":job_id})["cancellation_requested"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.engine.call("workflow_status", {"job_id":job_id})
            if job["status"] in {"failed", "cancelled", "completed"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self.engine.call("workflow_run_info", {"run_id":job_id})["run"]["status"], "cancelled")
        self.assertFalse(self.engine.status()["emergency_stop"])
        self.assertFalse(self.calls)

    def test_device_write_fails_fast_while_background_workflow_runs(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"seconds":20}}]))
        job_id = self.engine.call("workflow_start", {"name":"test"})["job_id"]
        deadline = time.monotonic() + 3
        while self.engine.call("workflow_status", {"job_id":job_id})["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        started = time.monotonic()
        with self.assertRaisesRegex(AutomationError, "Another automation owns the device"):
            self.engine.call("echo", {"value":"blocked"})
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.calls)
        self.engine.call("workflow_cancel", {"job_id":job_id})

    def test_condition_fields_are_strict(self):
        for args in ({"actual":"Error page","equal":"Saved"}, {"actual":1,"equals":1,"contains":1},
                     {"equals":"Saved"}, {"actual":1,"label":5}):
            with self.subTest(args=args), self.assertRaises(AutomationError):
                self.engine.workflows.save(self.recipe([{"tool":"assert","args":args}]))
        with self.assertRaisesRegex(AutomationError, "unknown field"):
            self.engine.workflows.save(self.recipe([{"tool":"if","condition":{"actual":True,"equal":False},"then":[]}]))
        with self.assertRaisesRegex(AutomationError, "only seconds"):
            self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"second":5}}]))
        with self.assertRaisesRegex(AutomationError, "assertion failed"):
            self.engine.workflows.run_definition(self.recipe([{"tool":"assert","args":{"actual":"Error page","equals":"Saved"}}]))

    def test_digest_change_rejects_scheduled_recipe(self):
        recipe=self.recipe([{"tool":"echo","args":{"value":1}}])
        before=digest(recipe)
        recipe["steps"][0]["args"]["value"]=2
        with self.assertRaisesRegex(AutomationError,"changed"):
            self.engine.workflows.run_definition(recipe,expected_sha256=before)
        self.assertFalse(self.calls)

    def test_write_retry_requires_idempotence(self):
        with self.assertRaisesRegex(AutomationError,"idempotent"):
            validate(self.recipe([{"tool":"echo","args":{"value":1},"retries":1}]),self.engine.tools)

    def test_nested_workflow_cannot_recurse(self):
        with self.assertRaises(AutomationError):
            self.engine.workflows.save(self.recipe([{"tool":"workflow_run","args":{"name":"test"}}]))

    def test_stop_persists_across_instances(self):
        self.engine.stop()
        with Engine(self.data) as other:
            with self.assertRaisesRegex(AutomationError,"Emergency stop"):
                other.call("workflow_save",{"definition":self.recipe([{"tool":"wait"}])})
            self.assertTrue(other.call("system_status")["emergency_stop"])
            other.resume()
        self.assertFalse(self.engine.status()["emergency_stop"])

    def test_recording_parameterizes_text_and_new_tab_id(self):
        self.engine.workflows.record_start("teaching")
        self.engine.workflows.capture("browser_new_tab",{"url":"about:blank"},{"targetId":"old-runtime-id"})
        self.engine.workflows.capture("browser_fill",{"target_id":"old-runtime-id","selector":"#name","text":"private-input"},{"verified":True})
        result=self.engine.workflows.record_stop()
        raw=Path(result["path"]).read_text()
        self.assertNotIn("private-input",raw)
        self.assertNotIn("old-runtime-id",raw)
        self.assertEqual(result["definition"]["steps"][1]["args"]["target_id"],"${tab_1.targetId}")
        self.assertEqual(result["definition"]["required_inputs"],["name"])
        self.assertEqual(result["example_inputs"],{"name":"private-input"})

    def test_recording_names_inputs_and_skips_observations(self):
        self.engine.workflows.record_start("names")
        for tool,args in (("browser_tabs",{}),("browser_screenshot",{"target_id":"t"}),
                          ("browser_fill",{"target_id":"t","selector":"#customer","text":"a"}),
                          ("browser_fill",{"target_id":"t","selector":["#host","input[name=\"customer\"]"],"text":"b"}),
                          ("browser_select",{"target_id":"t","selector":"div > select","value":"x"})):
            self.engine.workflows.capture(tool,args,{})
        result=self.engine.workflows.record_stop()
        self.assertEqual([s["tool"] for s in result["definition"]["steps"]],["browser_fill","browser_fill","browser_select"])
        self.assertEqual(result["definition"]["required_inputs"],["target_1","customer","customer_2","input_4"])

    def test_extended_comparisons(self):
        passing=[{"actual":"Total: 1,250 EGP","matches":"Total: [\\d,]+"},{"actual":"1,250","greater_than":1000},
                 {"actual":3,"less_than":"${limit}"},{"actual":"ok","not_equals":"error"},{"actual":["a"],"not_contains":"b"}]
        steps=[{"tool":"assert","args":args} for args in passing]
        self.assertEqual(self.engine.workflows.run_definition(self.recipe(steps),{"limit":4})["status"],"completed")
        for args in ({"actual":"x","matches":"("},{"actual":1,"greater_than":"many"},{"actual":1,"less_than":True}):
            with self.subTest(args=args),self.assertRaises(AutomationError):
                self.engine.workflows.save(self.recipe([{"tool":"assert","args":args}]))
        for args in ({"actual":"abc","greater_than":1},{"actual":5,"matches":"5"},{"actual":"999","less_than":10}):
            with self.subTest(args=args),self.assertRaises(AutomationError):
                self.engine.workflows.run_definition(self.recipe([{"tool":"assert","args":args}]))

    def test_retries_back_off_between_attempts(self):
        stamps=[]
        def unstable(value):
            stamps.append(time.monotonic())
            if len(stamps)<3: raise AutomationError("not yet")
            return {"value":value}
        self.engine.add("unstable","test backoff",unstable,True)
        self.engine.workflows.run_definition(self.recipe([{"tool":"unstable","args":{"value":1},"retries":2,"idempotent":True}]))
        self.assertGreaterEqual(stamps[1]-stamps[0],0.45)
        self.assertGreaterEqual(stamps[2]-stamps[1],0.95)

    def test_exported_script_refuses_an_edited_definition(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"seconds":0}}]))
        path=Path(self.engine.workflows.export("test")["path"])
        source=path.read_text(encoding="utf-8").replace('"seconds": 0','"seconds": 1')
        self.assertIn('"seconds": 1',source)
        path.write_text(source,encoding="utf-8")
        run=subprocess.run([sys.executable,str(path)],capture_output=True,text=True,timeout=20)
        self.assertNotEqual(run.returncode,0)
        self.assertIn("changed",run.stderr)

    def test_history_prune_keeps_recent_and_running(self):
        history=self.engine.workflows.history
        for run_id,age,status in (("old",40,"completed"),("recent",1,"failed"),("stuck",40,"running")):
            history.start(run_id,"test","x")
            history.step(run_id,1,"1","echo","completed",1,1,None)
            with history.database() as db:
                db.execute("UPDATE runs SET started_at=?,status=? WHERE run_id=?",(time.time()-age*86400,status,run_id))
        self.assertEqual(self.engine.call("workflow_history_prune",{"older_than_days":30})["deleted_runs"],1)
        self.assertEqual({r["run_id"] for r in history.list()},{"recent","stuck"})
        with self.assertRaises(AutomationError): history.info("old")
        with self.assertRaises(AutomationError): self.engine.call("workflow_history_prune",{"older_than_days":0})

    def test_journal_rotates_and_keeps_three_files(self):
        from full_automation.common import Journal
        journal=Journal(self.data/"logs/rotate.jsonl")
        journal.MAX_BYTES=200
        for index in range(60):
            journal.add({"tool":"x","index":index})
        files=sorted(p.name for p in (self.data/"logs").glob("rotate*"))
        self.assertEqual(files,["rotate.1.jsonl","rotate.2.jsonl","rotate.3.jsonl","rotate.jsonl"])
        self.assertTrue(all(p.stat().st_size<400 for p in (self.data/"logs").glob("rotate*")))

    def test_tool_inputs_are_described_for_agents(self):
        schema=self.engine.tools["browser_fill"].schema["properties"]
        self.assertIn("shadow",schema["selector"]["description"])
        self.assertIn("description",schema["target_id"])

    def test_recorded_shortcuts_replay_except_clipboard_and_system_keys(self):
        target={"Window":"Report - Notepad","AppPath":"C:\\Windows\\notepad.exe","Id":"","Name":"","Class":"","ControlType":0,"ParentId":""}
        session={"Steps":[{"Kind":"key","Value":"CTRL+S","Target":target,"Replayable":False,"Description":"Ctrl+S"},
                          {"Kind":"key","Value":"CTRL+V","Target":target,"Replayable":False,"Description":"Ctrl+V"},
                          {"Kind":"key","Value":"WIN+R","Target":target,"Replayable":False,"Description":"Win+R"},
                          {"Kind":"key","Value":"","Target":target,"Replayable":False,"Description":"older recorder"},
                          {"Kind":"key","Value":"CTRL+Return","Target":target,"Replayable":False,"Description":"Ctrl+Enter"}]}
        with patch.object(self.engine.desktop,"call",return_value=session):
            steps=self.engine.workflows.import_recording("unused.wos","shortcuts")["definition"]["steps"]
        self.assertEqual(steps[0],{"tool":"desktop_keys","args":{"keys":["CTRL","S"],"window":{"title":"Report - Notepad","path":"C:\\Windows\\notepad.exe"}}})
        self.assertEqual([s["tool"] for s in steps],["desktop_keys","manual","manual","manual","desktop_keys"])
        field={**target,"Id":"amount","Name":"Amount"}
        session={"Steps":[{"Kind":"key","Value":"Return","Target":field,"Replayable":False,"Description":"Enter"},
                          {"Kind":"key","Value":"Delete","Target":target,"Replayable":False,"Description":"Delete"}]}
        with patch.object(self.engine.desktop,"call",return_value=session):
            steps=self.engine.workflows.import_recording("unused.wos","control-keys")["definition"]["steps"]
        self.assertEqual(steps[0]["tool"],"desktop_action")
        self.assertEqual((steps[0]["args"]["action"],steps[0]["args"]["selector"]["id"]),("focus","amount"))
        self.assertEqual(steps[1]["args"]["keys"],["Return"])
        self.assertEqual(steps[2]["tool"],"manual",
                         "a control key without a control locator could act on the wrong control")

    def test_recorded_input_names_never_shadow_result_aliases(self):
        self.engine.workflows.record_start("aliases")
        self.engine.workflows.capture("browser_new_tab",{},{"targetId":"t1"})
        self.engine.workflows.capture("browser_fill",{"target_id":"t1","selector":"#tab_1","text":"x"},{})
        self.engine.workflows.capture("browser_fill",{"target_id":"t1","selector":"#tab_3","text":"y"},{})
        self.engine.workflows.capture("browser_new_tab",{},{"targetId":"t2"})
        definition=self.engine.workflows.record_stop()["definition"]
        aliases=[s["save_as"] for s in definition["steps"] if "save_as" in s]
        self.assertFalse(set(aliases)&set(definition["required_inputs"]),(aliases,definition["required_inputs"]))
        from full_automation.demo import convert
        demo,_,_=convert("demo","about:blank","http://127.0.0.1:9222",[{"kind":"fill","selector":"#tab","name":"tab","value":"v","time":1}])
        self.assertNotIn("tab",demo["required_inputs"])

    def test_unknown_tool_and_arguments_fail(self):
        with self.assertRaises(AutomationError): self.engine.call("does_not_exist")
        with self.assertRaises(AutomationError): self.engine.call("system_status",{"extra":1})

    def test_number_schema_rejects_bool(self):
        with self.assertRaises(AutomationError): self.engine.call("browser_launch",{"port":True})
        with self.assertRaises(AutomationError): self.engine.call("file_wait",{"path":"unused","timeout":float("nan")})

    def test_background_workflow_is_pollable(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"seconds":0.1}},{"tool":"echo","args":{"value":"done"}}]))
        started=self.engine.call("workflow_start",{"name":"test"})
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            result=self.engine.call("workflow_status",{"job_id":started["job_id"]})
            if result["status"] in {"completed","failed"}:break
            time.sleep(0.02)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(self.calls,["done"])

    def test_schedule_preview_pins_hash_and_data_path(self):
        definition=self.recipe([{"tool":"wait","args":{"seconds":0}}])
        self.engine.workflows.save(definition)
        result=self.engine.scheduler.preview("morning","test")
        self.assertEqual(result["workflow_sha256"],digest(definition))
        self.assertIn("--data-dir",result["arguments"])
        self.assertIn(str(self.engine.data),result["arguments"])
        self.assertIn("--expected-sha256",result["arguments"])

    def test_schedule_invalid_times(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait"}]))
        for at in ("99:99","tomorrow","09:00;echo hi"):
            with self.subTest(at=at),self.assertRaises(AutomationError):
                self.engine.scheduler.preview("morning","test",at=at)

    def test_export_is_valid_python_and_embeds_definition(self):
        self.engine.workflows.save(self.recipe([{"tool":"wait","args":{"seconds":0}}]))
        result=self.engine.workflows.export("test")
        source=Path(result["path"]).read_text()
        compile(source,result["path"],"exec")
        run=subprocess.run([sys.executable,result["path"]],capture_output=True,text=True,env={**os.environ,"FULL_AUTOMATION_DATA":str(self.data)},timeout=20)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"],"completed")

    def test_mcp_lifecycle_error_and_image_content(self):
        server=MCPServer(self.engine)
        self.assertEqual(server.handle({"jsonrpc":"2.0","id":1,"method":"tools/list"})["error"]["code"],-32002)
        response=server.handle({"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-11-25"}})
        self.assertEqual(response["result"]["protocolVersion"],"2025-11-25")
        self.assertIsNone(server.handle({"jsonrpc":"2.0","method":"notifications/initialized"}))
        response=server.handle({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"browser_launch","arguments":{"port":True}}})
        self.assertTrue(response["result"]["isError"])
        self.engine.add("picture","test",lambda: {"_image":"aGVsbG8=","mimeType":"image/png","bounds":{"x":0}})
        response=server.handle({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"picture"}})
        self.assertEqual(response["result"]["content"][0]["type"],"image")

    def test_mcp_stdio_only_json_messages(self):
        source=io.StringIO('bad json\n'+json.dumps({"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05"}})+'\n'+json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"})+'\n')
        target=io.StringIO()
        MCPServer(self.engine).serve(source,target)
        lines=[json.loads(x) for x in target.getvalue().splitlines()]
        self.assertEqual(len(lines),2)
        self.assertEqual(lines[0]["error"]["code"],-32700)
        self.assertEqual(lines[1]["result"]["protocolVersion"],"2024-11-05")

    def test_file_wait(self):
        output=self.data/"file.txt";output.write_text("complete")
        result=self.engine.file_wait(str(output),timeout=2,stable_seconds=0.1)
        self.assertTrue(result["stable"])

    def test_file_evidence_rejects_stale_output_and_verifies_new_hash(self):
        output = self.data / "output.txt"
        self.assertFalse(self.engine.call("file_inspect", {"path": str(output)})["exists"])
        output.write_text("old")
        old = self.engine.call("file_inspect", {"path": str(output), "with_sha256": True})
        with self.assertRaises(AutomationError):
            self.engine.call("file_wait", {"path":str(output), "newer_than":old["modified_at"], "timeout":0.15, "stable_seconds":0})
        output.write_text("new")
        os.utime(output, (old["modified_at"] + 2, old["modified_at"] + 2))
        self.assertTrue(self.engine.call("file_wait", {"path":str(output), "newer_than":old["modified_at"], "timeout":1, "stable_seconds":0})["stable"])
        new = self.engine.call("file_inspect", {"path": str(output), "with_sha256": True})
        self.assertNotEqual(old["sha256"], new["sha256"])


class CommonTest(unittest.TestCase):
    def test_names_reject_paths_and_windows_devices(self):
        for name in ("../escape","x/y","CON","LPT1.txt","", "a"*81):
            with self.subTest(name=name),self.assertRaises(AutomationError): slug(name)
        self.assertEqual(slug("تقرير_يومي"),"تقرير_يومي")

    def test_secret_redaction(self):
        value=redact({"password":"secret","message":"api_key=secret-value Bearer abcdefghijklmnop","nested":[{"access_token":"abc"}]})
        self.assertNotIn("secret-value",json.dumps(value))
        self.assertEqual(value["password"],"[REDACTED]")
        self.assertNotIn("abcdefghijklmnop",json.dumps(value))

    def test_typed_substitution_and_missing_environment(self):
        self.assertEqual(substitute("${rows}",{"rows":[1,2]}),[1,2])
        self.assertEqual(substitute("sum=${x}",{"x":3}),"sum=3")
        with patch.dict(os.environ,{},clear=True),self.assertRaises(AutomationError): substitute("${env:UNSET}",{})

    def test_lock_reentrant_and_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"device.lock"
            lock=RunLock(path)
            with lock:
                with lock:
                    with self.assertRaises(AutomationError):
                        with RunLock(path): pass
            with RunLock(path): pass

    def test_lock_held_by_another_thread_does_not_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            lock=RunLock(Path(temp)/"device.lock")
            held=threading.Event(); release=threading.Event()
            def owner():
                with lock:
                    held.set(); release.wait(5)
            thread=threading.Thread(target=owner); thread.start()
            try:
                self.assertTrue(held.wait(5))
                started=time.monotonic()
                with self.assertRaisesRegex(AutomationError,"Another automation"):
                    with lock: pass
                self.assertLess(time.monotonic()-started,1)
            finally:
                release.set(); thread.join(5)
            with lock: pass


class TranscriptDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        def lines(path,rows):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows)).encode())
            return path
        self.claude=lines(self.home/".claude/projects/-work-app/s1.jsonl",[
            {"type":"queue-operation","sessionId":"s1","content":"duplicate prompt"},
            {"type":"user","sessionId":"s1","cwd":"/work/app","message":{"role":"user","content":"اعمل تقرير المبيعات"},"timestamp":"t1"},
            {"type":"assistant","sessionId":"s1","message":{"role":"assistant","content":[{"type":"thinking","thinking":"private plan"},{"type":"text","text":"حاضر"},{"type":"tool_use","name":"Bash","id":"x1"}]}},
            {"type":"user","sessionId":"s1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"x1","content":"ok"}]}},
            {"type":"ai-title","sessionId":"s1","title":"Sales"}])
        self.codex=lines(self.home/".codex/sessions/2026/11/09/rollout-1.jsonl",[
            {"timestamp":"t0","type":"session_meta","payload":{"cwd":"/work/codex"}},
            {"timestamp":"t1","type":"event_msg","payload":{"type":"user_message","message":"fix the test"}},
            {"timestamp":"t2","type":"response_item","payload":{"type":"reasoning","summary":[{"text":"hidden"}]}},
            {"timestamp":"t3","type":"response_item","payload":{"type":"function_call","name":"shell","call_id":"c1"}},
            {"timestamp":"t4","type":"event_msg","payload":{"type":"agent_message","message":"done, token sk-ant-abcdefghijklmnopqrstu"}}])
        cline=self.home/".cline/data/tasks/42/ui_messages.json";cline.parent.mkdir(parents=True)
        cline.write_text(json.dumps([{"ts":1,"type":"say","say":"task","text":"rename files"},
            {"ts":2,"type":"say","say":"api_req_started","text":"{}"},{"ts":3,"type":"say","say":"completion_result","text":"renamed"}]))
        (self.home/".codex/auth.json").write_text('{"secret":1}')
        lines(self.home/".claude/projects/-work-app/credentials.jsonl",[{"type":"user","sessionId":"x","message":{"role":"user","content":"no"}}])
        self.custom=lines(self.home/"agy-logs/chat.jsonl",[{"type":"message","role":"user","content":"agy hello"}])
        self.env=patch.dict(os.environ,{"CLAUDE_CONFIG_DIR":"","CODEX_HOME":"","APPDATA":str(self.home/"none"),"XDG_CONFIG_HOME":str(self.home/"none")})
        self.env.start()

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def test_finds_known_conversations_only_with_preview(self):
        from full_automation.agents import list_transcripts
        profiles={"agy":{"command":"agy","args":["{prompt}"],"transcripts":[{"root":str(self.home/"agy-logs"),"pattern":"*.jsonl"}]}}
        result=list_transcripts(profiles,home=self.home)
        found={row["agent"]:row for row in result["transcripts"]}
        self.assertEqual(set(found),{"claude","codex","cline","agy"})
        self.assertEqual(found["claude"]["first_user_message"],"اعمل تقرير المبيعات")
        self.assertEqual(found["claude"]["cwd"],"/work/app")
        self.assertEqual(found["codex"]["cwd"],"/work/codex")
        self.assertEqual(found["cline"]["first_user_message"],"rename files")
        paths=json.dumps(result)
        self.assertNotIn("auth.json",paths);self.assertNotIn("credentials",paths)
        self.assertEqual([r["agent"] for r in list_transcripts(profiles,"codex",home=self.home)["transcripts"]],["codex"])
        for bad in ({"root":"relative","pattern":"*.jsonl"},{"root":str(self.home),"pattern":"../*.jsonl"},{"root":str(self.home),"pattern":"*.db"}):
            with self.subTest(bad=bad),self.assertRaises(AutomationError):
                list_transcripts({"x":{"transcripts":[bad]}},home=self.home)

    def test_conversation_rows_from_each_cli(self):
        claude=read_transcript(str(self.claude))
        self.assertEqual([(e["role"],e["text"]) for e in claude["events"]],[("user","اعمل تقرير المبيعات"),("assistant","حاضر")])
        self.assertEqual(claude["events"][1]["tools"],[{"name":"Bash","id":"x1"}])
        self.assertNotIn("private plan",json.dumps(claude,ensure_ascii=False))
        codex=read_transcript(str(self.codex))
        self.assertEqual([e["role"] for e in codex["events"]],["user","assistant","assistant"])
        self.assertEqual(codex["events"][1]["tools"][0]["name"],"shell")
        self.assertNotIn("hidden",json.dumps(codex));self.assertNotIn("sk-ant-",json.dumps(codex))
        cline=read_transcript(str(self.home/".cline/data/tasks/42/ui_messages.json"))
        self.assertEqual([(e["role"],e["text"]) for e in cline["events"]],[("user","rename files"),("assistant","renamed")])
        raw=read_transcript(str(self.claude),conversation_only=False)
        self.assertEqual(len(raw["events"]),5)

    def test_follow_a_running_conversation_with_cursor(self):
        first=read_transcript(str(self.codex),limit=1)
        self.assertTrue(first["has_more"])
        rest=read_transcript(str(self.codex),cursor=first["next_cursor"])
        self.assertFalse(rest["has_more"])
        with self.codex.open("ab") as stream:
            stream.write((json.dumps({"timestamp":"t5","type":"event_msg","payload":{"type":"user_message","message":"next"}})+"\n").encode())
            stream.write(b'{"timestamp":"t6","type":')
        new=read_transcript(str(self.codex),cursor=rest["next_cursor"])
        self.assertEqual([e["text"] for e in new["events"]],["next"])
        self.assertTrue(new["has_more"], "a partly written line stays for the next poll")


class DemoConversionTest(unittest.TestCase):
    def test_corrections_frames_unknown_targets_and_navigation(self):
        from full_automation.demo import convert
        events=[{"kind":"fill","selector":"#q","name":"q","value":"draft","time":0},
                {"kind":"fill","selector":"#q","name":"q","value":"final","time":10},
                {"kind":"key","key":"Enter","selector":"#q","text_field":True,"time":20},
                {"kind":"load","url":"http://x/results","time":30},
                {"kind":"click","selector":"#row","label":"row","time":40},
                {"kind":"double_click","selector":"#row","time":45},
                {"kind":"click","selector":None,"label":"canvas","time":50},
                {"kind":"click","selector":"#f","frame":True,"url":"http://x/frame","time":60},
                {"kind":"load","url":"http://x/typed","time":20000},
                {"kind":"fill","selector":"#d3","label":"تاريخ","value":"2026","time":20010}]
        definition,examples,notes=convert("demo","http://x/start","http://127.0.0.1:9222",events)
        steps=definition["steps"]
        tools=[s["tool"] for s in steps]
        self.assertEqual(examples,{"q":"final","input":"2026"})
        self.assertEqual(definition["required_inputs"],["q","input"])
        self.assertEqual(tools.count("browser_fill"),2)
        self.assertEqual(tools.count("manual"),2)
        clicks=[s for s in steps if s["tool"]=="browser_click"]
        self.assertEqual([c["args"].get("count",1) for c in clicks],[2])
        navigations=[s["args"]["url"] for s in steps if s["tool"]=="browser_navigate"]
        self.assertEqual(navigations,["http://x/start","http://x/typed"])
        self.assertIn("browser_key",tools)
        self.assertTrue(any("assert" in note for note in notes))
        with tempfile.TemporaryDirectory() as temp, Engine(temp) as engine:
            validate(definition,engine.tools)


class AgentTest(unittest.TestCase):
    def test_visible_chat_shapes_and_redaction(self):
        result=visible_event({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"أهلًا"},{"type":"thinking","thinking":"not-visible"}]}})
        self.assertEqual(result["text"],"أهلًا")
        self.assertNotIn("not-visible",json.dumps(result))
        self.assertEqual(visible_event({"type":"item.completed","item":{"type":"agent_message","text":"done"}})["text"],"done")

    def test_partial_transcript_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"chat.jsonl"
            first=json.dumps({"type":"assistant","text":"one"})+'\n'
            path.write_bytes((first+'{"type":').encode())
            result=read_transcript(str(path))
            self.assertEqual(result["next_cursor"],len(first.encode()))
            self.assertEqual(len(result["events"]),1)

    def test_credential_file_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"auth.json";path.write_text('{}')
            with self.assertRaises(AutomationError): read_transcript(str(path))

    def test_managed_process_captures_arabic_and_literal_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            fixture=folder/"cli.py"
            fixture.write_text("import json,sys\nprint(json.dumps({'type':'assistant','text':'أهلًا '+sys.argv[1]}),flush=True)\nprint('api_key=hidden-value',file=sys.stderr,flush=True)\n",encoding="utf-8")
            atomic_json(folder/"agents.json",{"fixture":{"command":sys.executable,"args":[str(fixture),"{prompt}"]}})
            agents=Agents(folder)
            try:
                started=agents.start("fixture","hello & echo SHOULD_NOT_EXECUTE",str(folder),timeout=10)
                session=agents.sessions[started["session_id"]]
                session.process.wait(timeout=10)
                for reader in session.readers:reader.join(timeout=3)
                result=agents.read(started["session_id"])
                text=json.dumps(result,ensure_ascii=False)
                self.assertIn("أهلًا",text)
                self.assertIn("hello & echo SHOULD_NOT_EXECUTE",text)
                self.assertNotIn("hidden-value",text)
            finally:agents.close()


if __name__=="__main__":unittest.main()
