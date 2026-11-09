from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .common import ROOT, atomic_json, read_json
from .engine import Engine
from .mcp import MCPServer


def write_integrations(engine):
    folder=engine.data/"integrations"
    args=[str(ROOT/"main.py"),"--data-dir",str(engine.data),"mcp"]
    server={"command":sys.executable,"args":args}
    atomic_json(folder/"claude.mcp.json",{"mcpServers":{"full-automation":server}})
    atomic_json(folder/"cline.mcp.json",{"mcpServers":{"full-automation":{**server,"disabled":False,"autoApprove":[]}}})
    atomic_json(folder/"agy.settings.fragment.json",{"mcpServers":{"full-automation":server}})
    toml="[mcp_servers.full-automation]\ncommand = "+json.dumps(sys.executable)+"\nargs = "+json.dumps(args)+"\ntool_timeout_sec = 120\n"
    (folder/"codex.config.fragment.toml").write_text(toml,encoding="utf-8")
    return {"directory":str(folder),"note":"Configuration fragments are generated; existing agent settings are not overwritten."}


def main(argv=None):
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    parser=argparse.ArgumentParser(description="Full Automation — local Windows/Chrome tools for agent CLIs")
    parser.add_argument("--data-dir")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("mcp")
    sub.add_parser("doctor")
    sub.add_parser("tools")
    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("integrations")
    call=sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("--args",default="{}",help="JSON arguments")
    call.add_argument("--args-file")
    workflow=sub.add_parser("workflow")
    worksub=workflow.add_subparsers(dest="action",required=True)
    save=worksub.add_parser("save");save.add_argument("file")
    run=worksub.add_parser("run");run.add_argument("name");run.add_argument("--variables",default="{}");run.add_argument("--variables-file");run.add_argument("--expected-sha256")
    export=worksub.add_parser("export");export.add_argument("name")
    agent=sub.add_parser("agent")
    agent.add_argument("name");agent.add_argument("prompt");agent.add_argument("--cwd",default=str(Path.cwd()));agent.add_argument("--model");agent.add_argument("--timeout",type=int,default=1800)
    args=parser.parse_args(argv)
    if args.data_dir:
        os.environ["FULL_AUTOMATION_DATA"]=str(Path(args.data_dir).expanduser().resolve())
    try:
        with Engine() as engine:
            if args.command=="mcp":
                MCPServer(engine).serve()
                return 0
            if args.command=="doctor":
                result={**engine.status(),"agents":engine.agents.probe()}
            elif args.command=="tools":
                result=[tool.public() for tool in engine.tools.values()]
            elif args.command=="stop": result=engine.stop()
            elif args.command=="resume": result=engine.resume()
            elif args.command=="integrations": result=write_integrations(engine)
            elif args.command=="call":
                values=read_json(Path(args.args_file)) if args.args_file else json.loads(args.args)
                if args.tool in {"recording_start","agent_start","workflow_record_start","workflow_start"}:
                    raise ValueError("This stateful tool requires the persistent MCP connection. For a CLI agent run use the agent subcommand.")
                result=engine.call(args.tool,values)
            elif args.command=="workflow":
                if args.action=="save": result=engine.workflows.save(read_json(Path(args.file)))
                elif args.action=="export": result=engine.workflows.export(args.name)
                else:
                    variables=read_json(Path(args.variables_file)) if args.variables_file else json.loads(args.variables)
                    result=engine.call("workflow_run",{"name":args.name,"variables":variables,**({"expected_sha256":args.expected_sha256} if args.expected_sha256 else {})})
            else:
                values={"agent":args.name,"prompt":args.prompt,"cwd":args.cwd,"timeout":args.timeout}
                if args.model: values["model"]=args.model
                session=engine.call("agent_start",values)
                cursor=0
                while True:
                    response=engine.agents.read(session["session_id"],cursor)
                    for row in response["events"]:
                        print(json.dumps(row,ensure_ascii=False),flush=True)
                    cursor=response["next_cursor"]
                    if not response["running"]:
                        # Allow pipe readers to drain before the final poll.
                        for reader in engine.agents.sessions[session["session_id"]].readers: reader.join(timeout=3)
                        response=engine.agents.read(session["session_id"],cursor,500)
                        for row in response["events"]: print(json.dumps(row,ensure_ascii=False),flush=True)
                        return response["exit_code"] or (1 if response["reason"] else 0)
                    time.sleep(0.2)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
