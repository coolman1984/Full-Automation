import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        with self.assertRaisesRegex(AutomationError,"Manual step"):
            self.engine.workflows.run_definition(self.recipe([{"tool":"manual"},{"tool":"echo","args":{"value":1}}]))
        self.assertFalse(self.calls)

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
        self.assertEqual(result["definition"]["required_inputs"],["input_1"])

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
        self.assertIn(str(self.data),result["arguments"])
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
            path.write_text(first+'{"type":')
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
