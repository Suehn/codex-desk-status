/* A smoke run of ui/dist/panel.html's script in node, against snapshots the real server built
 * from SYNTHETIC fixtures — no browser. A minimal DOM stands in for the page and a minimal
 * MCP Apps host answers ui/initialize and read_status. It proves the script runs every card
 * for every fixture without throwing and prints what each card says; layout, colour, focus
 * rings and screen readers still need a real host.
 *
 *   node tests/ui_smoke.cjs snapshots.json        (written by tests/test_ui_smoke.py)
 *
 * Prints one JSON object: {role: {tab: {text, ribbon, errors}}}.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// ------------------------------------------------------------------ a small DOM
class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.getAttribute("class") || "").split(/\s+/).filter(Boolean); }
  _set(v) { this.el.setAttribute("class", v.join(" ")); }
  contains(c) { return this._get().includes(c); }
  add(c) { const v = this._get(); if (!v.includes(c)) { v.push(c); this._set(v); } }
  remove(c) { this._set(this._get().filter(x => x !== c)); }
  toggle(c, on) { const has = this.contains(c); const want = on === undefined ? !has : !!on; if (want && !has) this.add(c); if (!want && has) this.remove(c); return want; }
}
class Node_ {
  constructor(type) { this.nodeType = type; this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); this.childNodes.push(c); c.parentNode = this; return c; }
  removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  get textContent() { return this.childNodes.map(c => c.textContent).join(""); }
  set textContent(v) { this.childNodes.forEach(c => { c.parentNode = null; }); this.childNodes = []; if (v !== "" && v !== null && v !== undefined) this.appendChild(new Text_(String(v))); }
}
class Text_ extends Node_ {
  constructor(t) { super(3); this.data = t; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}
class Element_ extends Node_ {
  constructor(tag) {
    super(1); this.tagName = tag.toUpperCase(); this.attrs = {}; this.listeners = {};
    this.classList = new ClassList(this);
    const el = this;
    this.style = { setProperty(k, v) { this[k] = v; }, getPropertyValue(k) { return this[k]; } };
    this.dataset = new Proxy({}, {
      get: (_, k) => el.getAttribute("data-" + String(k).replace(/[A-Z]/g, m => "-" + m.toLowerCase())) ?? undefined,
      set: (_, k, v) => { el.setAttribute("data-" + String(k).replace(/[A-Z]/g, m => "-" + m.toLowerCase()), v); return true; }
    });
    this.value = ""; this.disabled = false; this.checked = false;
    this.offsetWidth = 40; this.clientWidth = 320; this.scrollWidth = 320; this.scrollLeft = 0; this.offsetLeft = 0; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get tabIndex() { return Number(this.getAttribute("tabindex") || -1); }
  set tabIndex(v) { this.setAttribute("tabindex", v); }
  get id() { return this.attrs.id || ""; }
  set id(v) { this.attrs.id = String(v); }
  get name() { return this.attrs.name || ""; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  // events bubble, as the ribbon's delegated click needs
  dispatch(t, ev) {
    const target = this;
    for (let n = this; n && n.listeners; n = n.parentNode) {
      (n.listeners[t] || []).forEach(f => f(Object.assign({ currentTarget: n, target, preventDefault() {}, stopPropagation() {} }, ev || {})));
    }
  }
  focus() { global.__doc.activeElement = this; }
  blur() {}
  click() { this.dispatch("click"); }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode; } return null; }
  getBoundingClientRect() { return { left: 0, right: 300, top: 0, bottom: 20, width: 300, height: 20 }; }
  scrollTo() {}
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) { const out = []; walk(this, n => { if (n !== this && matches(n, sel)) out.push(n); }); return out; }
  matches(sel) { return matches(this, sel); }
  get options() { return this.querySelectorAll("option"); }
  get innerText() { return this.textContent; }
}
function walk(n, f) { n.childNodes.forEach(c => { if (c.nodeType === 1) { f(c); walk(c, f); } }); }
function matchesOne(el, sel) {
  const re = /([#.]?[\w-]+|\[[^\]]+\]|:[\w-]+|\*)/g;
  let m;
  while ((m = re.exec(sel))) {
    const t = m[1];
    if (t === "*") continue;
    if (t[0] === "#") { if (el.id !== t.slice(1)) return false; }
    else if (t[0] === ".") { if (!el.classList.contains(t.slice(1))) return false; }
    else if (t[0] === "[") {
      const a = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(t);
      if (!a) return false;
      if (a[2] === undefined ? !el.hasAttribute(a[1]) : el.getAttribute(a[1]) !== a[2]) return false;
    } else if (t[0] === ":") { if (t === ":checked" && !el.checked) return false; }
    else if (el.tagName !== t.toUpperCase()) return false;
  }
  return true;
}
function matches(el, sel) {
  return sel.split(",").some(part => {
    const chain = part.trim().split(/\s+/);
    if (!matchesOne(el, chain[chain.length - 1])) return false;
    let n = el.parentNode;
    for (let i = chain.length - 2; i >= 0; i--) {
      while (n && n.nodeType === 1 && !matchesOne(n, chain[i])) n = n.parentNode;
      if (!n || n.nodeType !== 1) return false;
      n = n.parentNode;
    }
    return true;
  });
}

// ------------------------------------------------------------------ parse the bundle's body
function parseHtml(html) {
  const doc = new Element_("html");
  const body = new Element_("body");
  doc.appendChild(body);
  const bodySrc = html.slice(html.indexOf("<body>") + 6, html.indexOf("<script>"));
  const stack = [body];
  const re = /<!--[\s\S]*?-->|<\/([\w-]+)>|<([\w-]+)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  const voids = new Set(["use", "path", "circle", "rect", "stop", "input", "br", "meta"]);
  while ((m = re.exec(bodySrc))) {
    if (m[0].startsWith("<!--")) continue;
    if (m[1]) { stack.pop(); continue; }
    if (m[2]) {
      const el = new Element_(m[2]);
      const ar = /([\w:-]+)(?:="([^"]*)")?/g; let a;
      while ((a = ar.exec(m[3] || ""))) el.setAttribute(a[1], a[2] === undefined ? "" : a[2]);
      stack[stack.length - 1].appendChild(el);
      if (!m[4] && !voids.has(m[2])) stack.push(el);
      continue;
    }
    if (m[5] && m[5].trim()) stack[stack.length - 1].appendChild(new Text_(m[5]));
  }
  return { doc, body, script: html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>")) };
}

// ------------------------------------------------------------------ run
const snaps = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const html = fs.readFileSync(path.join(__dirname, "..", "ui", "dist", "panel.html"), "utf8");
const TABS = ["context", "quota", "cache", "speed", "session"];
const out = {};

for (const role of Object.keys(snaps)) {
  const { doc, body, script } = parseHtml(html);
  const errors = [];
  const listeners = {};
  const parent = {};
  const document = {
    documentElement: doc, body, hidden: false, activeElement: body,
    createElement: t => new Element_(t),
    createElementNS: (_, t) => new Element_(t),
    createTextNode: t => new Text_(String(t)),
    getElementById: id => { let f = null; walk(doc, n => { if (!f && n.id === id) f = n; }); return f; },
    querySelector: s => doc.querySelector(s),
    querySelectorAll: s => doc.querySelectorAll(s),
    addEventListener: () => {},
    execCommand: () => false
  };
  global.__doc = document;
  const timers = [], intervals = [];
  // a clock the run can move on, for the ticker
  const clock = { offset: 0 };
  class Clock extends Date { static now() { return Date.now() + clock.offset; } }
  const window = {
    parent, document, CSS: { escape: s => s },
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    removeEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    sessionStorage: { getItem: () => null, setItem() {} },
    requestAnimationFrame: f => { timers.push(f); return 0; }, cancelAnimationFrame() {}
  };
  window.window = window;
  const snap = snaps[role];
  parent.postMessage = msg => {
    const reply = r => (listeners.message || []).forEach(f => f({ source: parent, data: r }));
    if (msg.method === "ui/initialize") setTimeout(() => reply({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: "2026-01-26", hostInfo: { name: "node-smoke" }, hostContext: { theme: "light", timeZone: "Asia/Shanghai" } } }), 0);
    else if (msg.method === "tools/call") {
      const s = JSON.parse(JSON.stringify(snap));
      s.request = Object.assign({}, s.request, { seq: msg.params.arguments.seq, manualSessionId: msg.params.arguments.sessionId || null });
      setTimeout(() => reply({ jsonrpc: "2.0", id: msg.id, result: { structuredContent: s } }), 0);
    }
  };
  const ctx = vm.createContext({
    window, document, console: { log() {}, error: e => errors.push(String(e)), warn() {} },
    setTimeout, clearTimeout, setInterval: f => intervals.push(f), clearInterval() {}, Promise, Date: Clock, Math, JSON, Intl, Object, Array, String, Number, Map, Proxy, Error,
    isFinite, parseFloat, navigator: {}, ResizeObserver: undefined, requestAnimationFrame: window.requestAnimationFrame, cancelAnimationFrame() {}
  });
  // every time the refresh mark is set busy: background reads should never do it
  const busy = [];
  const btn = document.getElementById("btn-refresh");
  if (btn) { const set = btn.setAttribute.bind(btn); btn.setAttribute = (k, v) => { if (k === "data-busy" && v === "true") busy.push(Date.now()); set(k, v); }; }
  try { vm.runInContext(script, ctx); } catch (e) { errors.push("load: " + e.stack); }
  out[role] = { errors, busy };
  // the manual binding makes the panel ask for this fixture's session
  out[role]._pending = { document, errors, timers, intervals, clock };
}

// let the host replies arrive, then visit every tab
setTimeout(() => {
  for (const role of Object.keys(out)) {
    const { document, errors, timers, intervals, clock } = out[role]._pending;
    delete out[role]._pending;
    timers.splice(0).forEach(f => { try { f(); } catch (e) { errors.push("raf: " + e.stack); } });
    const panel = document.getElementById("panel");
    for (const tab of TABS) {
      try { document.getElementById("tab-" + tab).dispatch("click"); }
      catch (e) { errors.push(tab + ": " + e.stack); }
      timers.splice(0).forEach(f => { try { f(); } catch (e) { errors.push("raf: " + e.stack); } });
      const t = document.getElementById("tab-" + tab);
      out[role][tab] = {
        ribbon: t.getAttribute("aria-label") + " | " + t.textContent.replace(/\s+/g, " ").trim(),
        text: panel.textContent.replace(/\s+/g, " ").trim(),
        sliders: panel.querySelectorAll('[role="slider"]').map(s => s.getAttribute("aria-label") + " = " + s.getAttribute("aria-valuetext")),
        tips: panel.querySelectorAll("[data-tip]").map(s => s.getAttribute("data-tip")),
        // the history layers: open or closed, and what their entries say
        hist: panel.querySelectorAll(".hist-toggle").map(b => b.getAttribute("aria-expanded") + " " + b.textContent.replace(/\s+/g, " ").trim()),
        // the columns drawn in the window, and what the axis says at its ends
        cols: panel.querySelectorAll(".tc-col, .tc-zero").length,
        ends: panel.querySelectorAll(".tc-end").map(e => e.textContent)
      };
      // the window over a long record: Home goes to the first request, the window with it
      const slider = panel.querySelector('[role="slider"]');
      // the speed's chosen reply: its reading takes the line under the figure and the columns,
      // not a place beside them, and gives the line back on Escape
      if (tab === "speed" && slider) {
        const sub = panel.querySelector(".lead-sub"), txt = panel.querySelector(".lead-text");
        const rest = txt ? txt.textContent : null;
        slider.dispatch("keydown", { key: "End" });
        out[role].speedRead = { besideChart: !!(sub && sub.closest(".lead")), rest,
          chosen: txt ? txt.textContent : null, reading: !!(txt && txt.classList.contains("is-reading")),
          now: slider.getAttribute("aria-valuenow"), max: slider.getAttribute("aria-valuemax") };
        slider.dispatch("keydown", { key: "Escape" });
        out[role].speedRead.back = txt ? txt.textContent : null;
        out[role].speedRead.probeEmpty = panel.querySelectorAll(".rd-probe").every(p => p.textContent === "");
      }
      if (tab === "session" && slider) {
        const probe = key => {
          slider.dispatch("keydown", { key });
          const r = panel.querySelector(".tc-reading");
          return { now: slider.getAttribute("aria-valuenow"), reading: r && !r.hidden ? r.textContent : null,
            cols: panel.querySelectorAll(".tc-col, .tc-zero").map(c => c.getAttribute("x")).length };
        };
        out[role].window = { home: probe("Home"), end: probe("End") };
        slider.dispatch("keydown", { key: "Escape" });
        // a window back: the slider stands on the window's last column, and the first key moves from there
        const back = panel.querySelector('.tc-page[data-dir="-1"]');
        if (back) {
          back.dispatch("click");
          const atRest = slider.getAttribute("aria-valuenow");
          slider.dispatch("keydown", { key: "ArrowLeft" });
          out[role].window.paged = { atRest, afterKey: slider.getAttribute("aria-valuenow"),
            ends: panel.querySelectorAll(".tc-end").map(e => e.textContent) };
          slider.dispatch("keydown", { key: "Escape" });
        }
      }
    }
    // the thread picker, opened: its rows and what they add up to
    const pick = document.getElementById("thread-pick");
    if (pick) {
      pick.dispatch("click");
      timers.splice(0).forEach(f => { try { f(); } catch (e) { errors.push("raf: " + e.stack); } });
      out[role].threads = document.getElementById("threads").textContent.replace(/\s+/g, " ").trim();
      pick.dispatch("click");
    }
    // the ticker, three minutes on: the focusable rows' names follow the times they show
    document.getElementById("tab-cache").dispatch("click");
    timers.splice(0).forEach(f => { try { f(); } catch (e) { errors.push("raf: " + e.stack); } });
    const names = () => panel.querySelectorAll("[data-relabel]").map(e => e.getAttribute("aria-label"));
    const before = names();
    clock.offset += 3 * 60000;
    intervals.forEach(f => { try { f(); } catch (e) { errors.push("tick: " + e.stack); } });
    out[role].tick = { before, after: names() };
    out[role].foot = document.getElementById("foot").textContent.replace(/\s+/g, " ").trim();
    out[role].banner = document.getElementById("banner-slot").textContent.replace(/\s+/g, " ").trim();
    const git = document.getElementById("foot-git");
    out[role].git = git.hidden ? null : git.getAttribute("aria-label");
  }
  // the panel keeps polling; the run ends once everything is written
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
}, 2500);
