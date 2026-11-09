"""Small RFC 6455/CDP client. No package installation or browser extension required."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request, HTTPRedirectHandler

from .common import AutomationError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def local_url(url: str, schemes=("http", "ws")):
    parts = urlsplit(url)
    if parts.scheme not in schemes or parts.username or parts.password or parts.fragment:
        raise AutomationError("Expected a local CDP URL without credentials.")
    host = parts.hostname
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host or "").is_loopback:
                raise ValueError()
        except ValueError as exc:
            raise AutomationError("CDP is restricted to loopback addresses.") from exc
    if not parts.port:
        raise AutomationError("CDP URL must include a port.")
    return parts


class WebSocket:
    MAX_MESSAGE = 32 * 1024 * 1024

    def __init__(self, url: str, timeout: float = 15):
        parts = local_url(url, ("ws",))
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
        self.sock = socket.create_connection((host, parts.port), timeout)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        request = (f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        try:
            self.sock.sendall(request.encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
                data = self.sock.recv(4096)
                if not data:
                    raise AutomationError("CDP closed during WebSocket handshake.")
                self.buffer.extend(data)
                if len(self.buffer) > 65536:
                    raise AutomationError("WebSocket handshake headers too large.")
            header, remaining = self.buffer.split(b"\r\n\r\n", 1)
            self.buffer = bytearray(remaining)
            lines = header.decode("latin1").split("\r\n")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {k.lower().strip(): v.strip() for k, v in headers.items()}
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0].split()[1] != "101" or headers.get("sec-websocket-accept") != accept or
                    headers.get("upgrade", "").lower() != "websocket" or
                    "upgrade" not in headers.get("connection", "").lower()):
                raise AutomationError("Invalid WebSocket upgrade response.")
        except Exception:
            self.sock.close()
            raise

    def _read(self, length: int) -> bytes:
        while len(self.buffer) < length:
            part = self.sock.recv(max(4096, min(length-len(self.buffer), 65536)))
            if not part:
                raise AutomationError("CDP connection closed.")
            self.buffer.extend(part)
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def send(self, payload: bytes, opcode=1):
        if len(payload) > self.MAX_MESSAGE:
            raise AutomationError("WebSocket message exceeds limit.")
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def receive(self) -> str:
        fragments = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            final, opcode, masked = bool(first & 0x80), first & 15, bool(second & 128)
            if first & 0x70 or masked:
                raise AutomationError("Unsupported or invalid server WebSocket frame.")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size > self.MAX_MESSAGE or len(fragments)+size > self.MAX_MESSAGE:
                raise AutomationError("WebSocket response exceeds limit.")
            if opcode >= 8 and (not final or size > 125):
                raise AutomationError("Invalid WebSocket control frame.")
            payload = self._read(size)
            if opcode == 8:
                raise AutomationError("CDP peer closed the connection.")
            if opcode == 9:
                self.send(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if started:
                    raise AutomationError("Unexpected WebSocket message start.")
                started = True
            elif opcode != 0 or not started:
                raise AutomationError("Invalid WebSocket continuation.")
            fragments.extend(payload)
            if final:
                return fragments.decode("utf-8")

    def close(self):
        self.sock.close()


class CDPConnection:
    def __init__(self, url):
        self.ws = WebSocket(url)
        self.counter = 0
        self.events = deque(maxlen=2000)
        # method -> callable(message) returning True when it consumed the notification.
        self.listeners = {}
        self.lock = threading.RLock()

    def call(self, method, params=None, timeout=20, session_id=None):
        if not isinstance(method, str) or "." not in method:
            raise AutomationError("Invalid CDP method name.")
        if not 0 < timeout <= 120:
            raise AutomationError("Timeout must be in (0, 120].")
        with self.lock:
            self.counter += 1
            identifier = self.counter
            message = {"id": identifier, "method": method, "params": params or {}}
            if session_id:
                message["sessionId"] = session_id
            deadline = time.monotonic() + timeout
            try:
                self.ws.send(json.dumps(message, ensure_ascii=False, allow_nan=False).encode())
                while True:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("CDP command timed out; side effects may already have occurred.")
                    self.ws.sock.settimeout(remaining)
                    response = json.loads(self.ws.receive())
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise AutomationError("CDP: " + str(response["error"]))
                        return response.get("result", {})
                    if "method" in response:
                        listener = self.listeners.get(response["method"])
                        if listener is None or not listener(response):
                            self.events.append(response)
            except (OSError, ValueError) as exc:
                raise AutomationError("CDP transport failed; inspect state before retrying: " + str(exc)) from exc

    def send_nowait(self, method, params=None):
        """Send from inside a notification handler; the reply is read and dropped by the current call."""
        with self.lock:
            self.counter += 1
            self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params or {}},
                                    ensure_ascii=False, allow_nan=False).encode())

    def close(self):
        self.ws.close()


LOCATE = """(selector, frames) => {
 let root=document, element;
 for (const frame of (frames||[])) {
   const owners=root.querySelectorAll(frame);
   if(owners.length!==1) throw new Error('Expected one frame for '+frame+', found '+owners.length);
   if(!owners[0].matches('iframe,frame')) throw new Error('Not a frame: '+frame);
   let inner=null;
   try { inner=owners[0].contentDocument; } catch(e) {}
   if(!inner) throw new Error('Frame '+frame+' is cross-origin or not loaded; it cannot be reached from this page');
   root=inner;
 }
 for (const part of (Array.isArray(selector)?selector:[selector])) {
   if (!root) throw new Error('Shadow root is not accessible');
   const found=root.querySelectorAll(part);
   if(found.length!==1) throw new Error('Expected one element, found '+found.length);
   element=found[0];root=element.shadowRoot;
 }
 if(!element) throw new Error('Empty selector');
 return element;
}"""

# Viewport offset of the innermost frame's content box, after bringing each frame into view.
FRAME_OFFSET = """(frames) => {
 let root=document, x=0, y=0;
 for (const frame of (frames||[])) {
   const owner=root.querySelector(frame);
   owner.scrollIntoView({block:'center',inline:'center'});
   const r=owner.getBoundingClientRect(), style=owner.ownerDocument.defaultView.getComputedStyle(owner);
   x+=r.left+owner.clientLeft+parseFloat(style.paddingLeft); y+=r.top+owner.clientTop+parseFloat(style.paddingTop);
   root=owner.contentDocument;
 }
 return {x,y};
}"""

SNAPSHOT = r"""() => {
 const out=[];
 const css=(el,root)=>{
  if(el.id && root.querySelectorAll('#'+CSS.escape(el.id)).length===1)return '#'+CSS.escape(el.id);
  const chain=[];let node=el;
  while(node && node!==root && node.nodeType===1){
   let part=node.localName;
   const siblings=node.parentNode?Array.from(node.parentNode.children).filter(x=>x.localName===node.localName):[];
   if(siblings.length>1)part+=':nth-of-type('+(siblings.indexOf(node)+1)+')';
   chain.unshift(part);node=node.parentElement;
  }return chain.join(' > ');
 };
 const visit=(root,prefix)=>{
  for(const el of root.querySelectorAll('*')){
   if(out.length>=400) return;
   const rect=el.getBoundingClientRect();
   if(rect.width && rect.height && getComputedStyle(el).visibility!=='hidden' &&
      (el.matches('a,button,input,select,textarea,summary,[role],[contenteditable=true]'))){
    const password=el.matches('input[type=password]');
    const selector=[...prefix,css(el,root)];
    out.push({selector:selector.length===1?selector[0]:selector,tag:el.localName,role:el.getAttribute('role'),
      label:(el.getAttribute('aria-label')||el.labels?.[0]?.innerText||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
      type:el.getAttribute('type'),value:password?'[PROTECTED]':String(el.value??'').slice(0,500),
      disabled:!!el.disabled,checked:!!el.checked,rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height}});
   }
   if(el.shadowRoot)visit(el.shadowRoot,[...prefix,css(el,root)]);
  }
 };
 visit(document,[]);
 return {title:document.title,url:location.href,text:(document.body?.innerText||'').slice(0,20000),
  viewport:{width:innerWidth,height:innerHeight,scale:devicePixelRatio},elements:out,
  frames:Array.from(document.querySelectorAll('iframe')).map(x=>({selector:css(x,document),src:x.src})),truncated:out.length>=400};
}"""


class Browser:
    def __init__(self, data: Path, endpoint="http://127.0.0.1:9222"):
        self.data = data
        self.endpoint = self._endpoint(endpoint)
        self.connections = {}
        self.process = None
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    @staticmethod
    def _endpoint(endpoint):
        p = local_url(endpoint, ("http",))
        if p.path not in {"", "/"} or p.query:
            raise AutomationError("Endpoint must be the CDP host and port only.")
        return endpoint.rstrip("/")

    def connect(self, endpoint="http://127.0.0.1:9222"):
        self.close()
        self.endpoint = self._endpoint(endpoint)
        return self._get("/json/version")

    def _get(self, path):
        with self.http.open(Request(self.endpoint+path), timeout=10) as response:
            content = response.read(4_000_001)
            if len(content)>4_000_000:
                raise AutomationError("CDP discovery response too large.")
            return json.loads(content)

    def tabs(self):
        return [{k: row.get(k) for k in ("id", "type", "title", "url")} for row in self._get("/json/list")]

    def _connection(self, target_id=None):
        key = target_id or "@browser"
        if key not in self.connections:
            if target_id:
                targets = [t for t in self._get("/json/list") if t.get("id")==target_id]
                if len(targets)!=1:
                    raise AutomationError("Target no longer exists. List tabs again.")
                url = targets[0].get("webSocketDebuggerUrl")
            else:
                url = self._get("/json/version").get("webSocketDebuggerUrl")
            if not url:
                raise AutomationError("This target has no CDP WebSocket.")
            local_url(url, ("ws",))
            # Do not follow a discovery response to another local service.
            if urlsplit(url).port != urlsplit(self.endpoint).port:
                raise AutomationError("Unexpected CDP WebSocket port.")
            self.connections[key] = CDPConnection(url)
        return self.connections[key]

    def call(self, method, params=None, target_id=None, session_id=None, timeout=20):
        return self._connection(target_id).call(method, params, timeout, session_id)

    def launch(self, executable=None, port=9222, headless=False):
        if isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535:
            raise AutomationError("Choose a port between 1024 and 65535.")
        candidates = [executable, os.environ.get("CHROME_PATH"), shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium")]
        if os.name=="nt":
            for folder in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
                if folder:
                    candidates.append(str(Path(folder)/"Google/Chrome/Application/chrome.exe"))
        found = next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)
        if not found:
            raise AutomationError("Chrome not found. Supply its executable path.")
        # Refuse attaching to a pre-existing unknown debugging listener by accident.
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port))==0:
                raise AutomationError("Port already in use. Use browser_connect explicitly or choose another port.")
        profile = self.data/"chrome-profile"
        profile.mkdir(parents=True,exist_ok=True)
        argv=[found,f"--remote-debugging-port={port}","--remote-debugging-address=127.0.0.1",f"--user-data-dir={profile}","--no-first-run","--no-default-browser-check"]
        if headless:
            argv.append("--headless=new")
        argv.append("about:blank")
        self.process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.endpoint=f"http://127.0.0.1:{port}"
        for _ in range(60):
            if self.process.poll() is not None:
                raise AutomationError("Chrome exited before CDP became available.")
            try:
                return {"pid":self.process.pid,"endpoint":self.endpoint,"profile":str(profile),"browser":self._get("/json/version")}
            except (OSError,ValueError):
                time.sleep(0.2)
        raise AutomationError("Chrome started but CDP did not become ready.")

    def new_tab(self,url="about:blank"):
        return self.call("Target.createTarget",{"url":url})

    def evaluate(self,target_id,expression,frame_id=None):
        args={"expression":expression,"returnByValue":True,"awaitPromise":True,"userGesture":True}
        if frame_id:
            args["contextId"]=self.call("Page.createIsolatedWorld",{"frameId":frame_id,"worldName":"FullAutomation"},target_id)["executionContextId"]
        response=self.call("Runtime.evaluate",args,target_id)
        if "exceptionDetails" in response:
            info=response["exceptionDetails"]
            raise AutomationError(info.get("exception",{}).get("description",info.get("text","JavaScript failed")))
        return response.get("result",{}).get("value")

    def navigate(self,target_id,url,timeout=30):
        result=self.call("Page.navigate",{"url":url},target_id)
        if result.get("errorText"):
            raise AutomationError(result["errorText"])
        # readyState may still refer to the old document immediately after navigation.
        if result.get("loaderId"):
            deadline=time.monotonic()+timeout
            while True:
                tree=self.call("Page.getFrameTree",target_id=target_id)
                if tree["frameTree"]["frame"].get("loaderId")==result["loaderId"]:
                    break
                if time.monotonic()>=deadline:
                    raise AutomationError("Navigation did not commit before timeout.")
                time.sleep(0.1)
        self.wait(target_id,expression="document.readyState === 'complete'",timeout=timeout)
        return self.evaluate(target_id,"({url:location.href,title:document.title})")

    def snapshot(self,target_id,frame_id=None):
        result=self.evaluate(target_id,"("+SNAPSHOT+")()",frame_id)
        if not frame_id:
            result["frame_tree"]=self.call("Page.getFrameTree",target_id=target_id)
        return result

    @staticmethod
    def _frames(frame):
        if frame is None:
            return []
        if not isinstance(frame,list) or not 1<=len(frame)<=8 or not all(isinstance(f,str) and f for f in frame):
            raise AutomationError("frame must be a list of 1-8 iframe CSS selectors, outermost first.")
        return frame

    def _element(self,target_id,selector,body,frame=None):
        frames=json.dumps(self._frames(frame),ensure_ascii=False)
        code=("((el,frames)=>{"+body+"})(("+LOCATE+")("+json.dumps(selector,ensure_ascii=False)+","+frames+"),"+frames+")")
        return self.evaluate(target_id,code)

    def click(self,target_id,selector,button="left",count=1,frame=None):
        if button not in {"left","right","middle"} or count not in {1,2}:
            raise AutomationError("Invalid click options.")
        position=self._element(target_id,selector,"if(el.disabled)throw new Error('Element disabled');el.scrollIntoView({block:'center',inline:'center'});const offset=("+FRAME_OFFSET+")(frames);const r=el.getBoundingClientRect();if(!r.width||!r.height)throw new Error('Element hidden');const x=r.x+r.width/2,y=r.y+r.height/2;const hit=el.getRootNode().elementFromPoint(x,y);if(hit!==el&&!el.contains(hit))throw new Error('Element is covered');const X=x+offset.x,Y=y+offset.y;if(frames.length){const top=document.elementFromPoint(X,Y);if(!top||!top.matches('iframe,frame'))throw new Error('Frame is covered or outside the viewport');}return {x:X,y:Y};",frame)
        for click_count in range(1,count+1):
            self.call("Input.dispatchMouseEvent",{"type":"mousePressed","button":button,"clickCount":click_count,**position},target_id)
            self.call("Input.dispatchMouseEvent",{"type":"mouseReleased","button":button,"clickCount":click_count,**position},target_id)
        return {"clicked":selector}

    def fill(self,target_id,selector,text,frame=None):
        if not isinstance(text,str):
            raise AutomationError("Text must be a string.")
        self._element(target_id,selector,"if(el.disabled||el.readOnly)throw new Error('Field not editable');if(!el.matches('input,textarea,[contenteditable=true]')||el.matches('input[type=file],input[type=button],input[type=submit],input[type=reset],input[type=checkbox],input[type=radio]'))throw new Error('Not a text field');const view=el.ownerDocument.defaultView;const r=el.getBoundingClientRect();if(!r.width||!r.height||view.getComputedStyle(el).visibility==='hidden')throw new Error('Field hidden');("+FRAME_OFFSET+")(frames);el.scrollIntoView({block:'center',behavior:'instant'});el.focus();if(el.getRootNode().activeElement!==el)throw new Error('Focus failed');if(frames.length&&!document.activeElement.matches('iframe,frame'))throw new Error('Frame did not receive focus');if(el.select)el.select();else {const s=view.getSelection(),range=el.ownerDocument.createRange();range.selectNodeContents(el);s.removeAllRanges();s.addRange(range);}return true;",frame)
        if text:
            self.call("Input.insertText",{"text":text},target_id)
        else:
            self.key(target_id,"Backspace")
        result=self._element(target_id,selector,"return {matches:(el.value??el.textContent)==="+json.dumps(text,ensure_ascii=False)+",protected:el.matches('input[type=password]')};",frame)
        if not result["matches"]:
            raise AutomationError("Field contents did not match after typing.")
        return {"verified":True,"protected":result["protected"]}

    def key(self,target_id,key,modifiers=0):
        codes={"Enter":13,"Tab":9,"Escape":27,"Backspace":8,"Delete":46,"ArrowLeft":37,"ArrowUp":38,"ArrowRight":39,"ArrowDown":40,"Home":36,"End":35,"PageUp":33,"PageDown":34," ":32}
        if not isinstance(key,str) or not key or len(key)>40 or not isinstance(modifiers,int) or not 0<=modifiers<=15:
            raise AutomationError("Invalid key or CDP modifier mask.")
        vk=codes.get(key,ord(key.upper()) if len(key)==1 else 0)
        args={"key":key,"modifiers":modifiers,"windowsVirtualKeyCode":vk,"nativeVirtualKeyCode":vk}
        self.call("Input.dispatchKeyEvent",{"type":"keyDown",**args},target_id)
        self.call("Input.dispatchKeyEvent",{"type":"keyUp",**args},target_id)
        return {"sent":key}

    def select(self,target_id,selector,value,frame=None):
        return self._element(target_id,selector,"if(!el.matches('select')||el.disabled)throw new Error('Not an enabled select');const value="+json.dumps(value)+";if(!Array.from(el.options).some(o=>o.value===value))throw new Error('Option missing');el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return {value:el.value};",frame)

    def scroll(self,target_id,x=0,y=600):
        self.call("Input.dispatchMouseEvent",{"type":"mouseWheel","x":100,"y":100,"deltaX":x,"deltaY":y},target_id)
        return {"scroll_requested":True}

    def screenshot(self,target_id):
        data=self.call("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False},target_id)
        return {"_image":data["data"],"mimeType":"image/png"}

    def upload(self,target_id,selector,files,frame=None):
        paths=[str(Path(p).expanduser().resolve(strict=True)) for p in files]
        if not paths or not all(Path(p).is_file() for p in paths):
            raise AutomationError("Upload requires existing files.")
        expression="("+LOCATE+")("+json.dumps(selector)+","+json.dumps(self._frames(frame))+")"
        response=self.call("Runtime.evaluate",{"expression":expression,"returnByValue":False},target_id)
        if "exceptionDetails" in response or "objectId" not in response.get("result",{}):
            raise AutomationError("File input selector is not unique or unavailable.")
        object_id=response["result"]["objectId"]
        try:
            return self.call("DOM.setFileInputFiles",{"files":paths,"objectId":object_id},target_id)
        finally:
            self.call("Runtime.releaseObject",{"objectId":object_id},target_id)

    def downloads(self,directory):
        path=Path(directory).expanduser().resolve()
        path.mkdir(parents=True,exist_ok=True)
        self.call("Browser.setDownloadBehavior",{"behavior":"allow","downloadPath":str(path),"eventsEnabled":True})
        return {"directory":str(path)}

    def wait(self,target_id,selector=None,expression=None,timeout=20,frame=None):
        if not 0<timeout<=120:
            raise AutomationError("Wait timeout must be in (0,120].")
        if selector is not None:
            expression="(()=>{try{return !!("+LOCATE+")("+json.dumps(selector)+","+json.dumps(self._frames(frame))+")}catch{return false}})()"
        if not expression:
            raise AutomationError("Supply a selector or expression to wait for.")
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.evaluate(target_id,expression):
                return {"matched":True}
            time.sleep(0.15)
        raise AutomationError("Browser condition timed out.")

    def dialogs(self,target_id,action="accept",prompt_text=None):
        """Arm a policy for alert/confirm/prompt/beforeunload dialogs; a blocked page cannot run other commands."""
        if action not in {"accept","dismiss","off"}:
            raise AutomationError("Dialog action must be accept, dismiss or off.")
        if prompt_text is not None and (not isinstance(prompt_text,str) or len(prompt_text)>10000):
            raise AutomationError("prompt_text must be a string of at most 10000 characters.")
        connection=self._connection(target_id)
        seen=getattr(connection,"dialogs",None)
        if seen is None:
            seen=connection.dialogs=deque(maxlen=50)
        if action=="off":
            connection.listeners.pop("Page.javascriptDialogOpening",None)
            return {"policy":"off","recent":list(seen)}
        def handle(message):
            params=message.get("params",{})
            reply={"accept":action=="accept"}
            if action=="accept" and prompt_text is not None and params.get("type")=="prompt":
                reply["promptText"]=prompt_text
            seen.append({"type":params.get("type"),"message":str(params.get("message",""))[:2000],"url":params.get("url"),
                         "handled":action,"time":time.time()})
            connection.send_nowait("Page.handleJavaScriptDialog",reply)
            return True
        connection.listeners["Page.javascriptDialogOpening"]=handle
        self.call("Page.enable",{},target_id)
        return {"policy":action,"recent":list(seen),
                "note":"Dialogs on this tab are now handled automatically while commands run. Use action=off to stop."}

    def events(self,target_id=None,clear=True):
        connection=self._connection(target_id)
        # A harmless command also drains queued transport notifications.
        connection.call("Browser.getVersion" if target_id is None else "Runtime.evaluate",{} if target_id is None else {"expression":"void 0"})
        rows=list(connection.events)
        if clear:
            connection.events.clear()
        return rows

    def close(self):
        for connection in list(self.connections.values()):
            connection.close()
        self.connections.clear()
        # Disconnecting a tool does not close the user's Chrome window.
