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

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'<html><body><button id="frame-button">Frame button</button></body></html>' if self.path=="/frame" else HTML
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
        for _ in range(100):
            if cls.process.poll() is not None:raise RuntimeError("Test Chrome exited before starting")
            try:cls.browser.tabs();break
            except OSError:time.sleep(0.1)
        else:raise RuntimeError("Test Chrome never became ready")

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
                result=fresh.workflows.run("demo",{"input_1":"replayed"})
                replayed=result["outputs"]["tab_1"]["targetId"]
                self.assertNotEqual(tab,replayed)
                self.assertEqual(fresh.browser.evaluate(replayed,"document.querySelector('#result').textContent"),"replayed")
            engine.browser.call("Target.closeTarget",{"targetId":tab})
            engine.browser.call("Target.closeTarget",{"targetId":replayed})

if __name__=="__main__":unittest.main()
