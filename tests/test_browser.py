import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from full_automation.cdp import Browser,local_url
from full_automation.common import AutomationError
from full_automation.engine import Engine

HTML=b'''<!doctype html><html><head><meta charset="utf-8"><title>Automation fixture</title></head>
<body><input id="name"><input id="secret" type="password" value="never-read-this"><button id="go" onclick="document.querySelector('#result').textContent=document.querySelector('#name').value">Go</button>
<div id="result"></div><select id="choice"><option value="a">A</option><option value="b">B</option></select>
<input id="file" type="file"><button class="duplicate">One</button><button class="duplicate">Two</button>
<div id="shadow"></div><iframe id="frame" src="/frame"></iframe>
<script>const r=document.querySelector('#shadow').attachShadow({mode:'open'});r.innerHTML='<input id="inside"><button id="shadow-button" onclick="this.textContent=String(2+2)">Shadow</button>';</script></body></html>'''

DEMO='''<!doctype html><html><head><meta charset="utf-8"><title>Demo form</title></head><body>
<input id="customer"><input name="pin" type="password"><select id="branch"><option value="cairo">Cairo</option><option value="alex">Alex</option></select>
<label><input type="checkbox" id="urgent"> urgent</label><div id="host"></div>
<button id="send" onclick="location.href='/demo-done?c='+encodeURIComponent(customer.value)+'&b='+branch.value+'&u='+urgent.checked">Send</button>
<script>document.querySelector('#host').attachShadow({mode:'open'}).innerHTML='<button class="inner" onclick="this.textContent=1">Inner</button>'</script></body></html>'''.encode()
DONE='''<!doctype html><html><head><meta charset="utf-8"><title>Done</title></head><body><p id="summary"></p>
<button id="confirm" onclick="if(!confirm('Send?'))return;const q=new URLSearchParams(location.search);summary.textContent='تم: '+q.get('c')+' / '+q.get('b')+' / '+q.get('u')">Confirm</button>
<button id="ask" onclick="summary.textContent='prompt: '+prompt('Your name?')">Ask</button></body></html>'''.encode()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        pages={"/frame":'<html><head><meta charset="utf-8"></head><body><input id="frame-input"><button id="frame-button" onclick="this.textContent=document.querySelector(\'#frame-input\').value">Frame button</button></body></html>'.encode(),"/demo":DEMO}
        body=DONE if self.path.startswith("/demo-done") else pages.get(self.path,HTML)
        self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass


class URLTest(unittest.TestCase):
    def test_remote_cdp_rejected(self):
        for url in ("http://example.com:9222","http://127.0.0.1:9222@evil.com:80","ws://192.168.1.3:9222/x","http://127.0.0.1"):
            with self.subTest(url=url),self.assertRaises(AutomationError):local_url(url)
        self.assertEqual(local_url("ws://127.0.0.1:9222/devtools/page/1").port,9222)


@unittest.skipUnless(os.environ.get("CHROME_TEST_PATH"),"Set CHROME_TEST_PATH to run the real Chrome integration tests")
class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.folder=Path(cls.temp.name)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url="http://127.0.0.1:"+str(cls.server.server_port)
        with socket.socket() as probe:probe.bind(("127.0.0.1",0));cls.port=probe.getsockname()[1]
        command=[os.environ["CHROME_TEST_PATH"],"--headless=new",f"--remote-debugging-port={cls.port}","--remote-debugging-address=127.0.0.1",f"--user-data-dir={cls.folder/'profile'}","--no-first-run","--disable-dev-shm-usage","about:blank"]
        # CI-only root container flag; production browser_launch never adds this.
        if os.name!="nt" and os.geteuid()==0:command.insert(1,"--no-sandbox")
        cls.process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.browser=Browser(cls.folder,f"http://127.0.0.1:{cls.port}")
        # A cold CI runner can take well over ten seconds to open the debugging port.
        deadline=time.monotonic()+45
        while True:
            if cls.process.poll() is not None:raise RuntimeError("Test Chrome exited before starting")
            try:cls.browser.tabs();break
            except OSError:
                if time.monotonic()>deadline:raise RuntimeError("Test Chrome never became ready within 45 seconds")
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.process.terminate();cls.process.wait(timeout=10)
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def setUp(self):
        self.target=self.browser.new_tab()["targetId"]
        self.browser.navigate(self.target,self.url)
        self.browser.wait(self.target,selector="#name")

    def tearDown(self):
        self.browser.call("Target.closeTarget",{"targetId":self.target})

    def test_snapshot_masks_password_and_exposes_selectors(self):
        snap=self.browser.snapshot(self.target)
        self.assertEqual(snap["title"],"Automation fixture")
        self.assertNotIn("never-read-this",json.dumps(snap))
        self.assertTrue(any(x["selector"]==["#shadow","#inside"] for x in snap["elements"]))

    def test_arabic_fill_click_and_clear(self):
        self.browser.fill(self.target,"#name","تقرير المصنع")
        self.browser.click(self.target,"#go")
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#result').textContent"),"تقرير المصنع")
        self.browser.fill(self.target,"#name","")
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#name').value"),"")

    def test_ambiguous_selector_has_no_effect(self):
        with self.assertRaises(AutomationError):self.browser.click(self.target,".duplicate")

    def test_hidden_field_does_not_type_into_previous_focus(self):
        self.browser.fill(self.target,"#name","keep this")
        self.browser.evaluate(self.target,"document.querySelector('#secret').style.display='none'")
        with self.assertRaises(AutomationError):self.browser.fill(self.target,"#secret","unwanted text")
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#name').value"),"keep this")

    def test_shadow_dom_fill_and_click(self):
        self.browser.fill(self.target,["#shadow","#inside"],"shadow text")
        self.browser.click(self.target,["#shadow","#shadow-button"])
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#shadow').shadowRoot.querySelector('button').textContent"),"4")

    def test_select_upload_and_screenshot(self):
        self.assertEqual(self.browser.select(self.target,"#choice","b")["value"],"b")
        file=self.folder/"upload.txt";file.write_text("fixture")
        self.browser.upload(self.target,"#file",[str(file)])
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#file').files[0].name"),"upload.txt")
        image=self.browser.screenshot(self.target)
        self.assertTrue(base64.b64decode(image["_image"]).startswith(b"\x89PNG"))

    def test_frame_snapshot_and_raw_cdp(self):
        tree=self.browser.call("Page.getFrameTree",target_id=self.target)
        frame=tree["frameTree"]["childFrames"][0]["frame"]["id"]
        snap=self.browser.snapshot(self.target,frame_id=frame)
        self.assertIn("Frame button",snap["text"])
        result=self.browser.call("Runtime.evaluate",{"expression":"6*7","returnByValue":True},target_id=self.target)
        self.assertEqual(result["result"]["value"],42)

    def test_click_fill_select_and_wait_inside_a_frame(self):
        self.browser.wait(self.target,selector="#frame-input",frame=["#frame"])
        self.browser.fill(self.target,"#frame-input","داخل الإطار",frame=["#frame"])
        self.browser.click(self.target,"#frame-button",frame=["#frame"])
        frame=self.browser.call("Page.getFrameTree",target_id=self.target)["frameTree"]["childFrames"][0]["frame"]["id"]
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#frame-button').textContent",frame_id=frame),"داخل الإطار")
        self.assertEqual(self.browser.evaluate(self.target,"document.querySelector('#name').value"),"")
        with self.assertRaisesRegex(AutomationError,"Expected one frame"):
            self.browser.click(self.target,"#frame-button",frame=["iframe.missing"])
        with self.assertRaisesRegex(AutomationError,"frame must be"):
            self.browser.click(self.target,"#frame-button",frame="#frame")

    def test_cross_origin_frame_is_reported_not_guessed(self):
        other=self.url.replace("127.0.0.1","localhost")+"/frame"
        self.browser.evaluate(self.target,"(()=>{const f=document.createElement('iframe');f.id='remote';f.src="+json.dumps(other)+";document.body.appendChild(f);return new Promise(r=>f.onload=()=>r(true))})()")
        with self.assertRaisesRegex(AutomationError,"cross-origin"):
            self.browser.click(self.target,"#frame-button",frame=["#remote"])

    def test_demonstration_reports_cross_origin_frame_nested_in_same_origin_frame(self):
        with Engine(self.folder/"nested-demo") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            engine.browser.wait(self.target,selector="#frame-input",frame=["#frame"])
            other=self.url.replace("127.0.0.1","localhost")+"/frame"
            engine.browser.evaluate(self.target,"(()=>{const d=document.querySelector('#frame').contentDocument;const f=d.createElement('iframe');f.id='inner';f.style.cssText='width:200px;height:80px';f.src="+json.dumps(other)+";d.body.prepend(f);return new Promise(r=>f.onload=()=>r(true))})()")
            engine.call("browser_demo_start",{"target_id":self.target,"name":"nested"})
            engine.browser.click(self.target,"#frame-button",frame=["#frame"])
            box=engine.browser.evaluate(self.target,"(()=>{const o=document.querySelector('#frame'),r=o.getBoundingClientRect(),i=o.contentDocument.querySelector('#inner').getBoundingClientRect();return {x:r.x+o.clientLeft+i.x+i.width/2,y:r.y+o.clientTop+i.y+i.height/2}})()")
            for kind in ("mousePressed","mouseReleased"):
                engine.browser.call("Input.dispatchMouseEvent",{"type":kind,"button":"left","clickCount":1,**box},self.target)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and not any(e["kind"]=="frame_focus" for e in engine.call("browser_demo_read",{})["events"]):
                time.sleep(0.1)
            result=engine.call("browser_demo_stop",{"save":False})
            manual=[s for s in result["definition"]["steps"] if s["tool"]=="manual"]
            self.assertEqual(len(manual),1,json.dumps(result["definition"]["steps"]))
            self.assertIn("cross-origin",manual[0]["description"])

    def test_recorded_browser_workflow_replays(self):
        with Engine(self.folder/"workflow-data") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            engine.workflows.record_start("demo")
            tab=engine.call("browser_new_tab",{})["targetId"]
            engine.call("browser_navigate",{"target_id":tab,"url":self.url})
            engine.call("browser_fill",{"target_id":tab,"selector":"#name","text":"original"})
            engine.call("browser_click",{"target_id":tab,"selector":"#go"})
            recorded=engine.workflows.record_stop()
            with Engine(engine.data) as fresh:
                result=fresh.workflows.run("demo",{"name":"replayed"})
                replayed=result["outputs"]["tab_1"]["targetId"]
                self.assertNotEqual(tab,replayed)
                self.assertEqual(fresh.browser.evaluate(replayed,"document.querySelector('#result').textContent"),"replayed")
            engine.browser.call("Target.closeTarget",{"targetId":tab})
            engine.browser.call("Target.closeTarget",{"targetId":replayed})

    def test_human_demonstration_becomes_replayable_workflow(self):
        with Engine(self.folder/"demo-data") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            tab=engine.call("browser_new_tab",{})["targetId"]
            engine.call("browser_navigate",{"target_id":tab,"url":self.url+"/demo"})
            engine.call("browser_demo_start",{"target_id":tab,"name":"demo-person"})
            self.assertTrue(engine.browser.evaluate(tab,"!!document.querySelector('full-automation-recording')"))
            # Trusted CDP input stands in for the person's mouse and keyboard.
            engine.browser.fill(tab,"#customer","شركة النور")
            engine.browser.fill(tab,"input[name=pin]","4321")
            engine.browser.click(tab,"#urgent")
            engine.browser.select(tab,"#branch","alex")
            engine.browser.click(tab,["#host","button.inner"])
            engine.browser.click(tab,"#send")
            engine.browser.wait(tab,selector="#confirm")
            live=engine.call("browser_demo_read",{})
            self.assertEqual([e["kind"] for e in live["events"]][:6],["fill","fill","click","select","click","click"])
            engine.browser.dialogs(tab,"accept")
            engine.browser.click(tab,"#confirm")
            self.assertIn("تم:",engine.browser.evaluate(tab,"summary.textContent"))
            engine.browser.dialogs(tab,"off")
            result=engine.call("browser_demo_stop",{})
            self.assertFalse(engine.browser.evaluate(tab,"!!document.querySelector('full-automation-recording')"))
            text=json.dumps(result,ensure_ascii=False)
            self.assertNotIn("4321",text)
            self.assertEqual(result["example_inputs"],{"customer":"شركة النور","branch":"alex"})
            definition=result["definition"]
            self.assertEqual(definition["required_inputs"],["customer","branch"])
            tools=[step["tool"] for step in definition["steps"]]
            self.assertNotIn("manual",tools)
            self.assertEqual(tools.count("browser_navigate"),1, "a click-caused navigation is not replayed separately")
            confirm_click=next(i for i,s in enumerate(definition["steps"]) if s["tool"]=="browser_click" and s["args"]["selector"]=="#confirm")
            self.assertIn({"tool":"browser_dialogs","args":{"target_id":"${tab.targetId}","action":"accept"}},definition["steps"][confirm_click-2:confirm_click])
            self.assertIn("${env:FA_PIN}",text)
            # The agent adds the business assertion, as the notes require, then replays with new inputs.
            definition["steps"]+=[{"tool":"browser_snapshot","args":{"target_id":"${tab.targetId}"},"save_as":"page"},
                                  {"tool":"assert","args":{"actual":"${page.text}","contains":"تم: مصنع الأمل / cairo / true","label":"confirmation"}}]
            engine.workflows.save(definition)
            with Engine(engine.data) as fresh, unittest.mock.patch.dict(os.environ,{"FA_PIN":"9999"}):
                replay=fresh.workflows.run("demo-person",{"customer":"مصنع الأمل","branch":"cairo"})
                self.assertEqual(replay["status"],"completed")
                replayed=replay["outputs"]["tab"]["targetId"]
            engine.browser.call("Target.closeTarget",{"targetId":tab})
            engine.browser.call("Target.closeTarget",{"targetId":replayed})

    def test_demonstration_reset_by_reconnect_can_restart(self):
        with Engine(self.folder/"demo-reset") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            engine.call("browser_demo_start",{"target_id":self.target,"name":"reset"})
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            with self.assertRaisesRegex(AutomationError,"reset"):
                engine.call("browser_demo_read",{})
            self.assertFalse(engine.browser.evaluate(self.target,"!!document.querySelector('full-automation-recording')"))
            engine.call("browser_demo_start",{"target_id":self.target,"name":"reset"})
            self.assertEqual(engine.call("browser_demo_stop",{"save":False})["events"],0)

    def test_dialog_policy_accepts_dismisses_and_answers_prompt(self):
        with Engine(self.folder/"dialog-data") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            tab=engine.call("browser_new_tab",{})["targetId"]
            engine.call("browser_navigate",{"target_id":tab,"url":self.url+"/demo-done?c=x&b=y&u=z"})
            engine.call("browser_dialogs",{"target_id":tab,"action":"dismiss"})
            engine.call("browser_click",{"target_id":tab,"selector":"#confirm"})
            self.assertEqual(engine.browser.evaluate(tab,"summary.textContent"),"")
            engine.call("browser_dialogs",{"target_id":tab,"action":"accept","prompt_text":"سارة"})
            engine.call("browser_click",{"target_id":tab,"selector":"#ask"})
            self.assertEqual(engine.browser.evaluate(tab,"summary.textContent"),"prompt: سارة")
            recent=engine.call("browser_dialogs",{"target_id":tab,"action":"off"})["recent"]
            self.assertEqual([(d["type"],d["message"],d["handled"]) for d in recent],[("confirm","Send?","dismiss"),("prompt","Your name?","accept")])
            engine.browser.call("Target.closeTarget",{"targetId":tab})

    def test_page_cannot_forge_or_stop_a_demonstration(self):
        with Engine(self.folder/"forge-data") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            engine.call("browser_demo_start",{"target_id":self.target,"name":"forge"})
            hostile="""(()=>{let hits=0;for(const k of Object.getOwnPropertyNames(window)){const v=window[k];
              try{if(typeof v==='function'&&k.startsWith('__'))
                {v(JSON.stringify({kind:'load',url:'https://evil.example/',time:Date.now()+1e9}));hits++}
              if(k.startsWith('__')&&v&&typeof v.stop==='function'){v.stop();v.stop(undefined);try{v.stop=()=>0}catch(e){};try{delete window[k]}catch(e){}}}catch(e){}}return hits})()"""
            self.assertEqual(engine.browser.evaluate(self.target,hostile),0)
            # A frame that existed before recording got the script too, so its binding is private as well.
            in_frame=hostile.replace("Object.getOwnPropertyNames(window)","Object.getOwnPropertyNames(frames[0])").replace("window[k]","frames[0][k]")
            self.assertEqual(engine.browser.evaluate(self.target,in_frame),0)
            # A call from a context the recorder did not secure is never trusted as a page step.
            connection=engine.browser.connections[self.target]
            secret=engine.demo.active["secret"]["binding"]
            connection.listeners["Runtime.bindingCalled"]({"params":{"name":secret,"executionContextId":987654,
                "payload":json.dumps({"kind":"click","selector":"#go","frame":False,"frame_path":[]})}})
            forged=engine.call("browser_demo_read",{})["events"]
            self.assertEqual(len(forged),1);self.assertTrue(forged[0]["frame"])
            self.assertTrue(engine.browser.evaluate(self.target,"!!document.querySelector('full-automation-recording')"))
            engine.browser.click(self.target,"#go")
            result=engine.call("browser_demo_stop",{"save":False})
            text=json.dumps(result)
            self.assertNotIn("evil.example",text)
            clicks=[s for s in result["definition"]["steps"] if s["tool"]=="browser_click"]
            self.assertEqual([c["args"]["selector"] for c in clicks],["#go"])
            self.assertEqual(sum(s["tool"]=="manual" for s in result["definition"]["steps"]),1)

    def test_demonstration_inside_frames(self):
        with Engine(self.folder/"frame-demo") as engine:
            engine.browser.connect(f"http://127.0.0.1:{self.port}")
            engine.browser.wait(self.target,selector="#frame-input",frame=["#frame"])
            other=self.url.replace("127.0.0.1","localhost")+"/frame"
            engine.browser.evaluate(self.target,"(()=>{const f=document.createElement('iframe');f.id='remote';f.style.cssText='width:300px;height:120px';f.src="+json.dumps(other)+";document.body.prepend(f);return new Promise(r=>f.onload=()=>r(true))})()")
            engine.call("browser_demo_start",{"target_id":self.target,"name":"framed"})
            engine.browser.fill(self.target,"#frame-input","طلب ٤٢",frame=["#frame"])
            engine.browser.click(self.target,"#frame-button",frame=["#frame"])
            # The person clicks into a cross-origin frame the page cannot see into.
            box=engine.browser.evaluate(self.target,"(()=>{const r=document.querySelector('#remote').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
            for kind in ("mousePressed","mouseReleased"):
                engine.browser.call("Input.dispatchMouseEvent",{"type":kind,"button":"left","clickCount":1,**box},self.target)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and not any(e["kind"]=="frame_focus" for e in engine.call("browser_demo_read",{})["events"]):
                time.sleep(0.1)
            result=engine.call("browser_demo_stop",{})
            steps=result["definition"]["steps"]
            framed=[s for s in steps if s["tool"] in {"browser_fill","browser_click"}]
            self.assertEqual([(s["tool"],s["args"].get("frame")) for s in framed],[("browser_fill",["#frame"]),("browser_click",["#frame"])],
                             json.dumps({"steps":steps,"events":result["events"]},ensure_ascii=False))
            self.assertEqual(result["example_inputs"],{"frame_input":"طلب ٤٢"})
            self.assertFalse(any(s["args"].get("selector") in {"#remote","#frame"} for s in steps if s["tool"]=="browser_click"))
            remote=[s for s in steps if s["tool"]=="manual"]
            self.assertEqual(len(remote),1);self.assertIn("cross-origin",remote[0]["description"])
            # Replace the cross-origin note with nothing (it was only a focus change) and replay with new input.
            definition=result["definition"];definition["steps"]=[s for s in steps if s["tool"]!="manual"]
            definition["steps"]+=[{"tool":"browser_evaluate","args":{"target_id":"${tab.targetId}","expression":"document.querySelector('#frame').contentDocument.querySelector('#frame-button').textContent"},"save_as":"label"},
                                  {"tool":"assert","args":{"actual":"${label}","equals":"طلب ٤٣"}}]
            engine.workflows.save(definition)
            with Engine(engine.data) as fresh:
                replay=fresh.workflows.run("framed",{"frame_input":"طلب ٤٣"})
                fresh.browser.call("Target.closeTarget",{"targetId":replay["outputs"]["tab"]["targetId"]})

if __name__=="__main__":unittest.main()
