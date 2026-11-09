"""Record a person's work in one Chrome tab and convert it to a reviewable workflow.

The page script reports through a CDP binding, so events survive navigation. Recording is
explicit (one tool call), visible (an in-page badge) and never captures password values.
"""
from __future__ import annotations

import json
import re

from .common import AutomationError, slug

BINDING = "__fullAutomationDemo"
MAX_EVENTS = 10000
CAUSED_LOAD_MS = 5000

SCRIPT = r"""(() => {
 const BIND = "__BINDING__";
 if (window.__faDemo) { window.__faDemo.resume(); return; }
 let badgeHost = null;
 const state = {active: true};
 Object.defineProperty(window, '__faDemo', {value: state, configurable: true});
 const send = (event) => {
  if (!state.active) return;
  try { window[BIND](JSON.stringify({...event, time: Date.now(), frame: window !== top, url: location.href})); } catch (e) {}
 };
 const unique = (root, sel) => { try { return root.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
 const quote = (v) => '"' + String(v).replace(/["\\]/g, '\\$&') + '"';
 const local = (el, root) => {
  if (el.id && unique(root, '#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
  for (const attr of ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label']) {
   const v = el.getAttribute(attr);
   if (v) { const s = el.localName + '[' + attr + '=' + quote(v) + ']'; if (unique(root, s)) return s; }
  }
  const chain = []; let node = el;
  while (node && node.nodeType === 1) {
   if (node !== el && node.id && unique(root, '#' + CSS.escape(node.id))) { chain.unshift('#' + CSS.escape(node.id)); break; }
   let part = node.localName;
   const parent = node.parentNode;
   const same = parent ? Array.from(parent.children).filter(x => x.localName === node.localName) : [];
   if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
   chain.unshift(part);
   if (parent === root || !node.parentElement) break;
   node = node.parentElement;
  }
  return chain.join(' > ');
 };
 const selectorFor = (el) => {
  const parts = []; let node = el;
  while (node) {
   const root = node.getRootNode();
   const s = local(node, root);
   if (!s || !unique(root, s)) return null;
   parts.unshift(s);
   node = root instanceof ShadowRoot ? root.host : null;
  }
  return parts.length === 1 ? parts[0] : parts;
 };
 const labelOf = (el) => String(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) ||
  el.getAttribute('placeholder') || el.innerText || el.getAttribute('name') || el.id || '').trim().slice(0, 120);
 const plain = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'range', 'color'];
 const textField = (el) => el.isContentEditable || el.matches('textarea') ||
  (el.matches('input') && !plain.includes(String(el.type || '').toLowerCase()));
 const describe = (el) => ({selector: selectorFor(el), tag: el.localName, type: el.getAttribute('type'),
  name: el.getAttribute('name'), id: el.id || null, label: labelOf(el), text_field: textField(el)});
 const recorded = new WeakMap();
 const fill = (el) => {
  if (!el || el.nodeType !== 1 || !textField(el)) return;
  const value = el.isContentEditable ? el.textContent : el.value;
  if (recorded.get(el) === value) return;
  recorded.set(el, value);
  const secret = el.matches('input[type=password]');
  send({kind: 'fill', ...describe(el), protected: secret, value: secret ? null : value});
 };
 const own = (e) => !!badgeHost && e.composedPath().includes(badgeHost);
 const actionable = 'a,button,input,select,textarea,summary,label,option,[role],[onclick],[contenteditable=""],[contenteditable=true]';
 const origin = (e) => {
  for (const n of e.composedPath()) { if (n.nodeType === 1 && n.matches(actionable)) return n; }
  const first = e.composedPath()[0];
  return first && first.nodeType === 1 ? first : null;
 };
 addEventListener('click', (e) => {
  if (!e.isTrusted || own(e) || e.detail > 1) return;
  const el = origin(e);
  if (!el || textField(el) || el.matches('select,option,input[type=file]')) return;
  send({kind: 'click', button: e.button, ...describe(el)});
 }, true);
 addEventListener('dblclick', (e) => {
  if (!e.isTrusted || own(e)) return;
  const el = origin(e);
  if (el && !textField(el)) send({kind: 'double_click', ...describe(el)});
 }, true);
 addEventListener('change', (e) => {
  if (own(e)) return;
  const el = e.composedPath()[0];
  if (!el || el.nodeType !== 1) return;
  if (el.matches('select')) send({kind: 'select', ...describe(el), value: el.value});
  else if (el.matches('input[type=file]')) send({kind: 'upload', ...describe(el), count: el.files ? el.files.length : 0});
  else fill(el);
 }, true);
 addEventListener('focusout', (e) => { const el = e.composedPath()[0]; if (el && el.nodeType === 1 && el.isContentEditable) fill(el); }, true);
 addEventListener('keydown', (e) => {
  if (!e.isTrusted || own(e) || !['Enter', 'Escape'].includes(e.key)) return;
  const el = e.composedPath()[0];
  const target = el && el.nodeType === 1 && el !== document.body && el !== document.documentElement ? el : null;
  if (target) fill(target);
  send({kind: 'key', key: e.key, modifiers: (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0),
   ...(target ? describe(target) : {})});
 }, true);
 const mount = () => {
  if (window !== top || !state.active || (badgeHost && badgeHost.isConnected) || !document.documentElement) return;
  badgeHost = document.createElement('full-automation-recording');
  const shadow = badgeHost.attachShadow({mode: 'closed'});
  shadow.innerHTML = '<div style="position:fixed;top:8px;inset-inline-end:8px;z-index:2147483647;background:#b00020;' +
   'color:#fff;font:600 13px system-ui,sans-serif;padding:4px 10px;border-radius:12px;pointer-events:none">' +
   '&#9679; Recording &middot; &#1580;&#1575;&#1585;&#1610; &#1575;&#1604;&#1578;&#1587;&#1580;&#1610;&#1604;</div>';
  document.documentElement.appendChild(badgeHost);
 };
 state.resume = () => { state.active = true; mount(); };
 state.stop = () => { state.active = false; if (badgeHost) badgeHost.remove(); };
 if (document.documentElement) mount(); else document.addEventListener('DOMContentLoaded', mount);
 if (__ANNOUNCE__) send({kind: 'load', title: document.title});
})()""".replace("__BINDING__", BINDING)


def page_script(announce: bool) -> str:
    return SCRIPT.replace("__ANNOUNCE__", "true" if announce else "false")


class BrowserDemo:
    def __init__(self, engine):
        self.engine = engine
        self.active = None

    @property
    def browser(self):
        return self.engine.browser

    def start(self, target_id, name):
        if self.active is not None:
            raise AutomationError("A browser demonstration is already being recorded. Stop it first.")
        slug(name)
        connection = self.browser._connection(target_id)
        events = []

        def sink(message):
            params = message.get("params", {})
            if params.get("name") != BINDING:
                return False
            if len(events) < MAX_EVENTS:
                try:
                    value = json.loads(params.get("payload", ""))
                except ValueError:
                    return True
                if isinstance(value, dict):
                    events.append(value)
            return True

        connection.listeners["Runtime.bindingCalled"] = sink
        script_id = None
        try:
            # Both domains must be enabled for the script and the binding to reach documents created later.
            self.browser.call("Page.enable", {}, target_id)
            self.browser.call("Runtime.enable", {}, target_id)
            self.browser.call("Runtime.addBinding", {"name": BINDING}, target_id)
            script_id = self.browser.call("Page.addScriptToEvaluateOnNewDocument",
                                          {"source": page_script(True)}, target_id)["identifier"]
            page = self.browser.evaluate(target_id, page_script(False) + ";({url:location.href,title:document.title})")
        except Exception:
            connection.listeners.pop("Runtime.bindingCalled", None)
            self._cleanup(target_id, script_id)
            raise
        self.active = {"name": name, "target_id": target_id, "connection": connection, "events": events,
                       "script_id": script_id, "start_url": page["url"], "endpoint": self.browser.endpoint}
        return {"recording": name, "target_id": target_id, "start_url": page["url"],
                "note": "A visible recording badge is shown in the tab. Ask the person to work in this tab, then call browser_demo_stop."}

    def _require(self):
        if self.active is None:
            raise AutomationError("No browser demonstration is being recorded.")
        current = self.browser.connections.get(self.active["target_id"])
        if current is not self.active["connection"]:
            stale, self.active = self.active, None
            self._cleanup(stale["target_id"], stale["script_id"])
            raise AutomationError("The browser connection was reset during the demonstration; later events were not observed.")
        return self.active

    def _drain(self, active):
        # Any response forces queued binding notifications to be read first.
        self.browser.call("Runtime.evaluate", {"expression": "void 0"}, active["target_id"])

    def read(self, cursor=0, limit=100):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or \
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AutomationError("Invalid demonstration cursor or limit.")
        active = self._require()
        self._drain(active)
        page = active["events"][cursor:cursor + limit]
        return {"events": [summary(e) for e in page], "next_cursor": cursor + len(page),
                "total": len(active["events"]), "truncated": len(active["events"]) >= MAX_EVENTS}

    def _cleanup(self, target_id, script_id):
        for method, params in (("Runtime.evaluate", {"expression": "window.__faDemo && window.__faDemo.stop()"}),
                               ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id} if script_id else None),
                               ("Runtime.removeBinding", {"name": BINDING})):
            if params is None:
                continue
            try:
                self.browser.call(method, params, target_id)
            except Exception:
                pass

    def stop(self, save=True):
        active = self._require()
        try:
            self._drain(active)
        finally:
            self._cleanup(active["target_id"], active["script_id"])
            active["connection"].listeners.pop("Runtime.bindingCalled", None)
            self.active = None
        definition, examples, notes = convert(active["name"], active["start_url"], active["endpoint"], active["events"])
        result = {"definition": definition, "example_inputs": examples, "events": len(active["events"]), "notes": notes}
        if save:
            result.update(self.engine.workflows.save(definition))
        return result

    def close(self):
        if self.active is not None:
            try:
                self._cleanup(self.active["target_id"], self.active["script_id"])
            finally:
                self.active = None


def summary(event):
    keys = ("kind", "selector", "label", "tag", "type", "key", "url", "frame", "protected", "time")
    row = {k: event.get(k) for k in keys if event.get(k) is not None}
    if event.get("kind") in {"fill", "select"} and not event.get("protected"):
        row["value"] = event.get("value")
    return row


def _identifier(event, used, fallback):
    for source in (event.get("name"), event.get("id"), event.get("label")):
        base = re.sub(r"[^A-Za-z0-9]+", "_", str(source or "")).strip("_").lower()[:40]
        if base:
            break
    else:
        base = fallback
    if not re.match(r"[A-Za-z_]", base):
        base = fallback + "_" + base
    name, index = base, 2
    while name in used:
        name, index = f"{base}_{index}", index + 1
    used.add(name)
    return name


def convert(name, start_url, endpoint, events):
    tab = "${tab.targetId}"
    steps = [{"tool": "browser_new_tab", "save_as": "tab"}]
    if start_url and start_url != "about:blank":
        steps.append({"tool": "browser_navigate", "args": {"target_id": tab, "url": start_url}})
    required, examples, used, notes = [], {}, set(), []
    environment = []
    last_action = None
    previous = None  # (kind, selector, step) of the last emitted action

    def manual(text):
        steps.append({"tool": "manual", "description": text})

    def wait(selector):
        steps.append({"tool": "browser_wait", "args": {"target_id": tab, "selector": selector}})

    for event in events:
        kind = event.get("kind")
        selector = event.get("selector")
        label = event.get("label") or event.get("tag") or "element"
        if event.get("frame"):
            if kind != "load":
                manual(f"Demonstrated {kind} on '{label}' inside a frame ({event.get('url')}). Replace with a frame-aware step.")
            continue
        if kind == "load":
            if last_action is None or not isinstance(event.get("time"), (int, float)) or \
                    event["time"] - last_action > CAUSED_LOAD_MS:
                steps.append({"tool": "browser_navigate", "args": {"target_id": tab, "url": event.get("url")}})
                previous = None
            continue
        if isinstance(event.get("time"), (int, float)):
            last_action = event["time"]
        if kind in {"click", "double_click", "fill", "select", "upload"} and selector is None:
            manual(f"Demonstrated {kind} on '{label}', but no unique selector exists. Inspect the page and add a step.")
            previous = None
            continue
        same = previous is not None and previous[1] == selector
        if kind == "click":
            if event.get("button", 0) != 0:
                manual(f"Demonstrated a non-left click on '{label}'. Review and add a step.")
                continue
            wait(selector)
            step = {"tool": "browser_click", "args": {"target_id": tab, "selector": selector}}
        elif kind == "double_click":
            if same and previous[0] == "click":
                previous[2]["args"]["count"] = 2
                previous = ("double_click", selector, previous[2])
                continue
            wait(selector)
            step = {"tool": "browser_click", "args": {"target_id": tab, "selector": selector, "count": 2}}
        elif kind == "fill":
            if same and previous[0] == "fill":
                # A later correction of the same field replaces the earlier value.
                if not event.get("protected"):
                    key = previous[2]["args"]["text"][2:-1]
                    if key in examples:
                        examples[key] = event.get("value", "")
                continue
            wait(selector)
            key = _identifier(event, used, "input")
            if event.get("protected"):
                variable = "FA_" + key.upper()
                environment.append(variable)
                text = "${env:" + variable + "}"
            else:
                required.append(key)
                examples[key] = event.get("value", "")
                text = "${" + key + "}"
            step = {"tool": "browser_fill", "args": {"target_id": tab, "selector": selector, "text": text}}
        elif kind == "select":
            wait(selector)
            key = _identifier(event, used, "choice")
            required.append(key)
            examples[key] = event.get("value", "")
            step = {"tool": "browser_select", "args": {"target_id": tab, "selector": selector, "value": "${" + key + "}"}}
        elif kind == "upload":
            wait(selector)
            key = _identifier(event, used, "files")
            required.append(key)
            notes.append(f"Input '{key}' is a list of local file paths for the upload field '{label}'.")
            step = {"tool": "browser_upload", "args": {"target_id": tab, "selector": selector, "files": "${" + key + "}"}}
        elif kind == "key":
            if selector is not None and event.get("text_field") and not same:
                wait(selector)
                steps.append({"tool": "browser_click", "args": {"target_id": tab, "selector": selector}})
            step = {"tool": "browser_key", "args": {"target_id": tab, "key": event.get("key")}}
            if event.get("modifiers"):
                step["args"]["modifiers"] = event["modifiers"]
        else:
            continue
        steps.append(step)
        previous = (kind, selector, step)

    if environment:
        notes.append("Protected fields were not captured. Set these environment variables before replay: " + ", ".join(environment) + ".")
    if any(step["tool"] == "manual" for step in steps):
        notes.append("Manual steps block execution until they are replaced.")
    notes.append("Add an assert for the expected business result before scheduling; a replayed click is not proof of success.")
    definition = {"version": 1, "name": name, "browser_endpoint": endpoint, "required_inputs": required, "steps": steps}
    return definition, examples, notes
