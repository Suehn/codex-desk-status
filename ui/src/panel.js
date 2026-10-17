/* 会话状态 (internal name: Clepsydra / desk-status) — MCP App panel.
 *
 * Bridge: MCP Apps JSON-RPC over postMessage (ui/initialize → initialized →
 * tools/call read_status every 2 s). window.openai is only a feature-detected
 * fallback when the standard handshake is unavailable.
 *
 * Every log-derived string is inserted with textContent / setAttribute. There
 * is no innerHTML anywhere in this file.
 */
(function () {
  "use strict";

  var APP_INFO = { name: "desk-status-panel", version: "0.2.4" };
  var PROTOCOL_VERSION = "2026-01-26";
  var SNAPSHOT_KEY = "desk-status/snapshot";
  var SCHEMA = "desk-status/2";
  var POLL_MS = 2000;
  var BUSY_AFTER_MS = 700;  // a background read shows the busy mark only once it takes this long
  var MIN_BUSY_MS = 450;    // an asked-for refresh shows it at least this long
  var INIT_TIMEOUT_MS = 6000;
  var CALL_TIMEOUT_MS = 10000;
  var FIRST_RESULT_WAIT_MS = 1500;
  var MAX_BACKOFF_MS = 30000;
  var OFFLINE_AFTER_FAILURES = 3;
  var LIST_REFRESH_EVERY = 5;
  var GROUP = 5;
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABS = ["context", "quota", "cache", "speed", "session"];
  var AUTO = "__auto";
  // Only these host-context keys may name the *current* thread. Keys such as
  // parentThreadId / relatedThreadId / sessionId are deliberately ignored.
  var HOST_KEYS = ["threadId", "thread_id", "codex/threadId", "codex/thread_id", "openai/threadId", "openai/thread_id"];

  // ------------------------------------------------------------------
  // DOM helpers (text only — never HTML strings)
  // ------------------------------------------------------------------
  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      for (var k in props) {
        if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
        var v = props[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "text") el.textContent = String(v);
        else if (k === "class") el.className = v;
        else if (k === "on") { for (var ev in v) el.addEventListener(ev, v[ev]); }
        else if (k === "data") { for (var dk in v) if (v[dk] !== undefined && v[dk] !== null) el.dataset[dk] = String(v[dk]); }
        else if (k === "style") { for (var sk in v) el.style.setProperty(sk, v[sk]); }
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(el, k); }); return; }
    el.appendChild(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  function svg(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    if (attrs) for (var k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) el.setAttribute(k, String(attrs[k]));
    return el;
  }
  function icon(name, cls) {
    var s = svg("svg", { "aria-hidden": "true", focusable: "false", class: cls || null });
    s.appendChild(svg("use", { href: "#" + name }));
    return s;
  }
  function $(id) { return document.getElementById(id); }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

  // ------------------------------------------------------------------
  // Formatting
  // ------------------------------------------------------------------
  var nf = new Intl.NumberFormat("zh-CN");
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmtInt(v) { return isNum(v) ? nf.format(Math.round(v)) : "—"; }
  function fmtK(v) {
    if (!isNum(v)) return "—";
    if (Math.abs(v) < 1000) return String(Math.round(v));
    if (Math.abs(v) < 1e6) return (v / 1000).toFixed(v < 1e4 ? 1 : v < 1e5 ? 1 : 0).replace(/\.0$/, "") + "k";
    return (v / 1e6).toFixed(2).replace(/0$/, "") + "M";
  }
  function pctNum(ratio) {
    if (!isNum(ratio)) return "—";
    var p = ratio * 100;
    // a tenth shown where rounding would mislead: under 10, and between 99 and 100
    if ((p > 0 && p < 10) || (p > 99 && p < 100)) return p.toFixed(1);
    return String(Math.round(p));
  }
  function pctOf(v) { return isNum(v) ? (v > 0 && v < 10 ? v.toFixed(1) : String(Math.round(v))) : "—"; }
  function fmtRate(v) { return isNum(v) ? (v >= 100 ? v.toFixed(0) : v.toFixed(1)) : "—"; }
  function fmtSecs(ms) {
    if (!isNum(ms)) return "—";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + " 秒";
    var m = Math.floor(s / 60);
    return m + " 分 " + Math.round(s - m * 60) + " 秒";
  }
  function fmtRel(ts, nowMs) {
    if (!isNum(ts)) return "—";
    var d = Math.max(0, nowMs - ts) / 1000;
    if (d < 5) return "刚刚";
    if (d < 60) return Math.floor(d) + " 秒前";
    if (d < 3600) return Math.floor(d / 60) + " 分前";
    if (d < 86400) return Math.floor(d / 3600) + " 小时前";
    return Math.floor(d / 86400) + " 天前";
  }
  function fmtUntil(ts, nowMs) {
    if (!isNum(ts)) return "—";
    var d = (ts - nowMs) / 1000;
    if (d <= 0) return "已到重置时间";
    if (d < 3600) return Math.max(1, Math.ceil(d / 60)) + " 分";
    var hrs = Math.floor(d / 3600), mins = Math.floor((d - hrs * 3600) / 60);
    if (d < 86400) return hrs + " 小时 " + mins + " 分";
    var days = Math.floor(d / 86400), rh = Math.floor((d - days * 86400) / 3600);
    return days + " 天 " + rh + " 小时";
  }
  var dtfCache = {};
  function dtf(kind) {
    var tz = state.hostContext && typeof state.hostContext.timeZone === "string" ? state.hostContext.timeZone : undefined;
    var key = kind + "|" + (tz || "");
    if (dtfCache[key]) return dtfCache[key];
    var opts = kind === "time"
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false };
    try { if (tz) opts.timeZone = tz; dtfCache[key] = new Intl.DateTimeFormat("zh-CN", opts); }
    catch (e) { delete opts.timeZone; dtfCache[key] = new Intl.DateTimeFormat("zh-CN", opts); }
    return dtfCache[key];
  }
  function parts(kind, ts) {
    var o = {};
    dtf(kind).formatToParts(new Date(ts)).forEach(function (p) { o[p.type] = p.value; });
    return o;
  }
  function fmtTime(ts) { if (!isNum(ts)) return "—"; var p = parts("time", ts); return p.hour + ":" + p.minute; }
  function fmtStamp(ts) { if (!isNum(ts)) return "—"; var p = parts("stamp", ts); return p.month + "/" + p.day + " " + p.hour + ":" + p.minute; }
  function sameDay(a, b) { var x = parts("stamp", a), y = parts("stamp", b); return x.month === y.month && x.day === y.day; }
  function fmtWhen(ts) { return !isNum(ts) ? "—" : sameDay(ts, now()) ? fmtTime(ts) : fmtStamp(ts); }
  function windowShort(min) {
    if (!isNum(min) || min <= 0) return "额度";
    if (min % 1440 === 0) return min / 1440 + " 天";
    if (min % 60 === 0) return min / 60 + " 小时";
    return min + " 分钟";
  }
  function windowHours(min) {
    if (!isNum(min) || min <= 0) return "未知";
    if (min % 60 === 0) return min / 60 + " 小时";
    return min + " 分钟";
  }
  // ------------------------------------------------------------------
  // Money. Every figure is priced by the server from its built-in API price table, each
  // request at its own rates (server/desk_status/pricing.py); the panel adds them up and says
  // it is an estimate. Nothing here knows a price.
  // ------------------------------------------------------------------
  function isFast(tier) { return tier === "priority" || tier === "fast"; }
  function tierName(tier) { return isFast(tier) ? "Fast" : "标准"; }
  function fmtUsd(v) {
    if (!isNum(v)) return "—";
    if (v === 0) return "$0";
    if (v < 0.005) return "<$0.01";
    return "$" + v.toFixed(2);
  }
  function fmtRateUsd(v) { return "$" + (v < 0.1 ? String(+v.toFixed(3)) : v.toFixed(2)); }
  function times(tokens, rate) { return fmtK(tokens) + " × " + fmtRateUsd(rate); }

  // The prompt cache. OpenAI documents for GPT-5.6 and later that a cached prefix is kept at
  // least 30 minutes after its last write or reuse. That is the model's rule, a reference: the
  // log does not say this Codex request was kept so. Past the 30 minutes the state is not
  // known (not "expired"); for earlier models no fixed time is documented.
  var CACHE_MIN_TTL_MS = 30 * 60000;
  function cacheTtlMs(model) {
    var m = /^gpt-(\d+)(?:\.(\d+))?/i.exec(typeof model === "string" ? model.trim() : "");
    if (!m) return null;
    var major = Number(m[1]), minor = Number(m[2] || 0);
    return major > 5 || (major === 5 && minor >= 6) ? CACHE_MIN_TTL_MS : null;
  }

  // The cache as the next request may find it: the rule's phase now (within the minimum, past
  // it, no rule, or a switch of model, which a cache written for another model cannot serve),
  // and the server's two conditional costs for it.
  function cacheOutlook(s) {
    var c = s.cache || {}, l = c.latest;
    if (!l || !isNum(l.inputTokens)) return null;
    var e = c.estimate || null;
    var ranOn = l.model || s.model;
    var o = {
      ranOn: ranOn, next: (e && e.model) || s.model || ranOn, tier: s.serviceTier,
      switched: !!(e && e.switched),
      sent: isNum(l.requestAt) ? l.requestAt : l.at,
      ttl: cacheTtlMs(ranOn),
      est: e && e.priced ? e : null
    };
    if (isNum(o.sent) && o.ttl) {
      o.until = o.sent + o.ttl;
      o.left = o.until - now();
      o.used = Math.min(1, Math.max(0, (now() - o.sent) / o.ttl));
    }
    o.phase = o.switched ? "switched" : !o.ttl || !isNum(o.sent) ? "norule" : o.left > 0 ? "within" : "beyond";
    return o;
  }

  var TURN = {
    active: { text: "正在工作", tone: "active" },
    completed: { text: "空闲", tone: "idle" },
    error: { text: "出错结束", tone: "crit" },
    interrupted: { text: "已中断", tone: "warn" },
    superseded: { text: "未正常结束", tone: "warn" },
    none: { text: "等待开始", tone: "idle" }
  };
  var INVALID = {
    no_timing: "仅工具调用",
    too_short: "用时过短",
    too_long: "用时异常",
    no_output: "无输出",
    clock_skew: "时间戳异常",
    no_response_id: "无法去重",
    no_timestamp: "缺少时间"
  };
  var KIND = { review: "自动审查", subagent: "子代理", agent: "代理创建" };

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  var state = {
    phase: "connecting", // connecting | ready | failed | standalone | torndown
    transport: null,
    hostInfo: null,
    hostContext: {},
    hostHint: null,          // allowlisted id from host context (unvalidated)
    hostCtxPresent: false,   // host context carries an allowlisted key at all
    hostAmbiguous: false,    // allowlisted keys disagree → never bind
    launchHostId: null,      // server-validated id from the launching open_status result
    hostSessionId: null,     // server-validated current-chat id of the latest applied snapshot
    hostEpoch: 0,
    manualSessionId: null,
    threadId: null,          // a subagent of the session viewed in its place; null is the main agent
    threadMenuOpen: false,
    threadSort: "recent",  // the thread list's order: "recent" (newest first) or "cost"
    switching: false,
    allKinds: false,
    themePref: "auto",
    tab: "context",
    histOpen: {},            // the cards whose history layer is open, by card
    snapshot: null,
    sessions: null,
    skew: 0,
    lastSuccessAt: 0,
    failures: 0,
    lastError: null,
    inflight: false,
    queued: false,
    timer: null,
    seq: 0,
    appliedSeq: -1,
    epoch: 0,
    initialHandled: false,
    paused: document.hidden === true,
    menuOpen: false,
    initError: null,
    pollCount: 0
  };

  function now() { return Date.now() + state.skew; }

  // ------------------------------------------------------------------
  // Preferences. The manual binding is instance state (widgetState only);
  // purely visual prefs may also use sessionStorage.
  // ------------------------------------------------------------------
  function openai() { return typeof window.openai === "object" && window.openai ? window.openai : null; }
  function store() { try { return window.sessionStorage; } catch (e) { return null; } }
  function restorePrefs() {
    var s = store(), saved = null;
    try { saved = s && JSON.parse(s.getItem("desk-status/prefs") || "null"); } catch (e) { saved = null; }
    var o = openai();
    var ws = o && o.widgetState;
    var w = ws && typeof ws === "object" ? (ws.privateContent && ws.privateContent.deskStatus) || ws.deskStatus : null;
    [saved, w].forEach(function (p) {
      if (!p || typeof p !== "object") return;
      if (p.theme === "light" || p.theme === "dark" || p.theme === "auto") state.themePref = p.theme;
      if (TABS.indexOf(p.tab) >= 0) state.tab = p.tab;
      if (typeof p.allKinds === "boolean") state.allKinds = p.allKinds;
      if (p.hist && typeof p.hist === "object") TABS.forEach(function (t) { if (typeof p.hist[t] === "boolean") state.histOpen[t] = p.hist[t]; });
    });
    if (w && typeof w.manual === "string" && UUID_RE.test(w.manual)) state.manualSessionId = w.manual;
  }
  function savePrefs() {
    var p = { theme: state.themePref, tab: state.tab, allKinds: state.allKinds, hist: state.histOpen };
    var s = store();
    try { if (s) s.setItem("desk-status/prefs", JSON.stringify(p)); } catch (e) { /* optional */ }
    var o = openai();
    if (!o || typeof o.setWidgetState !== "function") return;
    try { o.setWidgetState({ privateContent: { deskStatus: Object.assign({ manual: state.manualSessionId }, p) } }); }
    catch (e) { /* optional */ }
  }

  // ------------------------------------------------------------------
  // Theme
  // ------------------------------------------------------------------
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function resolvedTheme() {
    if (state.themePref === "light" || state.themePref === "dark") return state.themePref;
    var t = state.hostContext && state.hostContext.theme;
    if (t === "light" || t === "dark") return t;
    return darkQuery && darkQuery.matches ? "dark" : "light";
  }
  function applyTheme() { document.documentElement.setAttribute("data-theme", resolvedTheme()); }
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", applyTheme);

  var SAFE_FONT_RE = /^[\w\s\-",.'一-鿿]{1,300}$/;
  function applyHostStyles(ctx) {
    var vars = ctx && ctx.styles && ctx.styles.variables;
    if (!vars || typeof vars !== "object") return;
    ["--font-sans", "--font-mono"].forEach(function (k) {
      var v = vars[k];
      if (typeof v === "string" && SAFE_FONT_RE.test(v)) {
        document.documentElement.style.setProperty(k, v + (k === "--font-sans" ? ", \"Microsoft YaHei UI\", system-ui, sans-serif" : ", ui-monospace, monospace"));
      }
    });
  }

  // ------------------------------------------------------------------
  // Host identity
  // ------------------------------------------------------------------
  function findHostHint(ctx) {
    var ids = [], present = false;
    if (ctx && typeof ctx === "object") {
      HOST_KEYS.forEach(function (k) {
        if (!Object.prototype.hasOwnProperty.call(ctx, k)) return;
        var v = ctx[k];
        if (v === null || v === undefined || v === "") return;
        present = true;
        var id = typeof v === "string" ? v.trim().toLowerCase() : "";
        if (UUID_RE.test(id) && ids.indexOf(id) < 0) ids.push(id);
      });
    }
    return { present: present, ambiguous: ids.length > 1, id: ids.length === 1 ? ids[0] : null };
  }

  function hostCandidate() {
    if (state.hostAmbiguous) return null;
    if (state.hostCtxPresent) return state.hostHint;
    return state.launchHostId;
  }

  function mergeHostContext(patch) {
    if (!patch || typeof patch !== "object") return;
    for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) state.hostContext[k] = patch[k];
    dtfCache = {};
    applyTheme();
    applyHostStyles(state.hostContext);
    var r = findHostHint(state.hostContext);
    if (r.present !== state.hostCtxPresent || r.id !== state.hostHint || r.ambiguous !== state.hostAmbiguous) {
      var hadPrevious = state.hostCtxPresent || state.launchHostId || state.hostSessionId;
      state.hostCtxPresent = r.present;
      state.hostHint = r.id;
      state.hostAmbiguous = r.ambiguous;
      if (r.present || hadPrevious) hostIdentityChanged();
    }
    render();
  }

  // The current chat changed (A → B, A → none, or became ambiguous). Context
  // is newer than anything we validated before: drop it, invalidate replies
  // in flight, and keep any explicit manual choice untouched.
  function hostIdentityChanged() {
    state.hostEpoch++;
    state.launchHostId = null;
    state.hostSessionId = null;
    state.epoch++;
    if (!state.manualSessionId) state.switching = true;
    pollSoon();
  }

  // ------------------------------------------------------------------
  // Transports
  // ------------------------------------------------------------------
  function rpcError(err) {
    var e = new Error(err && typeof err.message === "string" ? err.message : "宿主返回错误");
    e.code = err && err.code;
    return e;
  }

  function createMcpAppsTransport() {
    var pending = new Map();
    var nextId = 1;
    var parentWin = window.parent;
    function onMessage(ev) {
      if (ev.source !== parentWin) return;
      var m = ev.data;
      if (!m || typeof m !== "object" || m.jsonrpc !== "2.0") return;
      if (typeof m.method === "string") {
        if (m.id !== undefined && m.id !== null) handleHostRequest(m);
        else handleNotification(m.method, m.params);
        return;
      }
      if (m.id !== undefined && pending.has(m.id)) {
        var p = pending.get(m.id);
        pending.delete(m.id);
        clearTimeout(p.timer);
        if (m.error) p.reject(rpcError(m.error));
        else p.resolve(m.result);
      }
    }
    window.addEventListener("message", onMessage);
    function post(msg) { parentWin.postMessage(msg, "*"); }
    function request(method, params, timeout) {
      return new Promise(function (resolve, reject) {
        var id = nextId++;
        var timer = setTimeout(function () {
          pending.delete(id);
          var e = new Error("宿主 " + Math.round(timeout / 1000) + " 秒内没有响应");
          e.code = "timeout";
          reject(e);
        }, timeout);
        pending.set(id, { resolve: resolve, reject: reject, timer: timer });
        try { post({ jsonrpc: "2.0", id: id, method: method, params: params }); }
        catch (err) { clearTimeout(timer); pending.delete(id); reject(err); }
      });
    }
    function handleHostRequest(m) {
      if (m.method === "ping") { post({ jsonrpc: "2.0", id: m.id, result: {} }); return; }
      if (m.method === "ui/resource-teardown") {
        teardown();
        post({ jsonrpc: "2.0", id: m.id, result: {} });
        return;
      }
      post({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "Method not found" } });
    }
    return {
      kind: "mcp-apps",
      request: request,
      notify: function (method, params) { try { post({ jsonrpc: "2.0", method: method, params: params }); } catch (e) { /* ignore */ } },
      callTool: function (name, args, timeout) { return request("tools/call", { name: name, arguments: args }, timeout); },
      destroy: function () {
        window.removeEventListener("message", onMessage);
        pending.forEach(function (p) { clearTimeout(p.timer); var e = new Error("连接已关闭"); e.code = "closed"; p.reject(e); });
        pending.clear();
      }
    };
  }

  function createOpenAiTransport(o) {
    return {
      kind: "openai-compat",
      notify: function () {},
      callTool: function (name, args, timeout) {
        return new Promise(function (resolve, reject) {
          var timer = setTimeout(function () { var e = new Error("宿主没有及时响应"); e.code = "timeout"; reject(e); }, timeout);
          Promise.resolve().then(function () { return o.callTool(name, args); }).then(
            function (r) { clearTimeout(timer); resolve(r); },
            function (err) { clearTimeout(timer); reject(err instanceof Error ? err : rpcError(err)); }
          );
        });
      },
      destroy: function () {}
    };
  }

  function handleNotification(method, params) {
    if (method === "ui/notifications/tool-result") onToolResult(params);
    else if (method === "ui/notifications/host-context-changed") mergeHostContext(params);
  }

  function extractSnapshot(result) {
    if (!result || typeof result !== "object") return null;
    var meta = result._meta;
    if (meta && typeof meta === "object" && meta[SNAPSHOT_KEY] && meta[SNAPSHOT_KEY].schema === SCHEMA) return meta[SNAPSHOT_KEY];
    var sc = result.structuredContent;
    if (sc && typeof sc === "object" && sc.schema === SCHEMA) return sc;
    return null;
  }

  // The launching open_status result. It may arrive late; it is only allowed
  // to paint when nothing newer exists and the user has not chosen anything.
  function onToolResult(params) {
    var snap = extractSnapshot(params);
    if (!snap || state.initialHandled) return;
    state.initialHandled = true;
    var host = snap.binding && snap.binding.host;
    if (state.hostEpoch === 0 && !state.hostCtxPresent && host && host.status === "matched" && UUID_RE.test(host.sessionId || "")) {
      state.launchHostId = host.sessionId;
    }
    var manualMatches = ((snap.request && snap.request.manualSessionId) || null) === (state.manualSessionId || null);
    if (state.appliedSeq < 0 && state.epoch === 0 && !state.inflight && manualMatches) applySnapshot(snap, -1);
    else if (state.launchHostId && !state.hostSessionId) pollSoon();
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------
  function waitFor(pred, ms) {
    return new Promise(function (resolve) {
      var t0 = Date.now();
      (function tick() {
        if (pred() || Date.now() - t0 >= ms) resolve();
        else setTimeout(tick, 50);
      })();
    });
  }

  function connect() {
    if (state.transport) { state.transport.destroy(); state.transport = null; }
    clearTimeout(state.timer);
    state.timer = null;
    state.inflight = false;
    state.queued = false;
    clearTimeout(state.busyTimer);
    setBusy(false);
    state.phase = "connecting";
    state.initError = null;
    render();
    if (window.parent === window) {
      state.phase = "standalone";
      render();
      return Promise.resolve();
    }
    var t = createMcpAppsTransport();
    state.transport = t;
    return t.request(
      "ui/initialize",
      { appInfo: APP_INFO, appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] }, protocolVersion: PROTOCOL_VERSION },
      INIT_TIMEOUT_MS
    ).then(function (res) {
      if (state.transport !== t) return;
      t.notify("ui/notifications/initialized", {});
      state.hostInfo = res && res.hostInfo && typeof res.hostInfo === "object" ? res.hostInfo : null;
      state.phase = "ready";
      mergeHostContext((res && res.hostContext) || {});
      startSizeReporting();
    }, function (err) {
      if (state.transport !== t) return;
      var o = openai();
      if (o && typeof o.callTool === "function") {
        t.destroy();
        state.transport = createOpenAiTransport(o);
        state.phase = "ready";
        if (o.theme === "light" || o.theme === "dark") mergeHostContext({ theme: o.theme });
        onToolResult({ _meta: o.toolResponseMetadata, structuredContent: o.toolOutput });
        return;
      }
      state.phase = "failed";
      state.initError = err;
      render();
    }).then(function () {
      if (state.phase !== "ready") return;
      return waitFor(function () { return !!state.snapshot || state.epoch > 0; }, FIRST_RESULT_WAIT_MS).then(function () {
        if (!state.inflight && !state.timer) schedule(state.snapshot ? POLL_MS : 0);
      });
    });
  }

  function teardown() {
    state.phase = "torndown";
    clearTimeout(state.timer);
    state.timer = null;
    if (ticker) { clearInterval(ticker); ticker = null; }
    if (sizeObserver) { sizeObserver.disconnect(); sizeObserver = null; }
    if (state.transport) { var t = state.transport; state.transport = null; setTimeout(function () { t.destroy(); }, 0); }
    render();
  }

  // ------------------------------------------------------------------
  // Polling: one request in flight, sequenced, epoch-guarded
  // ------------------------------------------------------------------
  function schedule(delay) {
    clearTimeout(state.timer);
    state.timer = null;
    if (state.phase !== "ready" || state.paused) return;
    state.timer = setTimeout(function () { state.timer = null; poll(); }, Math.max(0, delay));
  }
  function backoffDelay() {
    if (!state.failures) return POLL_MS;
    return Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, state.failures - 1));
  }
  function pollSoon() {
    if (state.phase !== "ready") return;
    if (state.inflight) { state.queued = true; return; }
    schedule(0);
  }

  function wantSessionList() {
    var snap = state.snapshot;
    if (!snap || !state.sessions || state.listWanted) return true;
    if (snap.binding && (snap.binding.state === "none" || snap.binding.state === "manual_missing")) return true;
    return state.pollCount % LIST_REFRESH_EVERY === 0;
  }

  function poll() {
    if (state.phase !== "ready" || state.paused) return;
    if (state.inflight) { state.queued = true; return; }
    clearTimeout(state.timer);
    state.timer = null;
    var mySeq = ++state.seq;
    var myEpoch = state.epoch;
    var manual = state.manualSessionId;
    var thread = state.threadId;
    var args = { seq: mySeq, includeSessions: wantSessionList(), allKinds: state.allKinds };
    if (manual) args.sessionId = manual;
    if (thread) args.threadId = thread;
    var hint = hostCandidate();
    if (hint) args.hostSessionId = hint;
    state.listWanted = false;
    state.inflight = true;
    state.pollCount++;
    // the refresh mark turns for a refresh asked for, or a read that keeps us waiting; the
    // background read every two seconds stays still
    var asked = state.refreshAsked;
    state.refreshAsked = false;
    clearTimeout(state.busyTimer);
    if (asked) setBusy(true);
    else state.busyTimer = setTimeout(function () { setBusy(true); }, BUSY_AFTER_MS);
    var busySince = Date.now();
    var transport = state.transport;
    transport.callTool("read_status", args, CALL_TIMEOUT_MS).then(function (result) {
      if (state.transport !== transport || myEpoch !== state.epoch || mySeq <= state.appliedSeq) return;
      if (result && result.isError) {
        var txt = result.content && result.content[0] && typeof result.content[0].text === "string" ? result.content[0].text : "工具返回错误";
        throw Object.assign(new Error(txt), { code: "tool_error" });
      }
      var snap = extractSnapshot(result);
      if (!snap) throw Object.assign(new Error("返回的数据无法识别"), { code: "bad_snapshot" });
      if (snap.request && snap.request.seq !== undefined && snap.request.seq !== null && snap.request.seq !== mySeq) return;
      if (((snap.request && snap.request.manualSessionId) || null) !== (manual || null)) return;
      // a reply for the thread viewed before a switch is not this view's
      if (thread !== state.threadId) return;
      applySnapshot(snap, mySeq);
    }).catch(function (err) {
      if (state.transport !== transport || myEpoch !== state.epoch) return;
      state.failures++;
      state.lastError = err || new Error("未知错误");
      if (state.failures === OFFLINE_AFTER_FAILURES) announce("与本机服务的连接中断，正在重试");
      render();
    }).then(function () {
      if (state.transport !== transport) return;
      state.inflight = false;
      clearTimeout(state.busyTimer);
      // an asked-for refresh shows that it ran, briefly, even when the answer is instant
      if (asked) state.busyTimer = setTimeout(function () { setBusy(false); }, Math.max(0, MIN_BUSY_MS - (Date.now() - busySince)));
      else setBusy(false);
      if (state.queued) { state.queued = false; schedule(0); }
      else schedule(backoffDelay());
    });
  }

  function applySnapshot(snap, seq) {
    if (!snap || snap.schema !== SCHEMA || !snap.binding || typeof snap.binding !== "object") return;
    state.appliedSeq = Math.max(state.appliedSeq, seq);
    if (snap.server && isNum(snap.server.time)) state.skew = snap.server.time - Date.now();
    var host = snap.binding.host;
    state.hostSessionId = host && host.status === "matched" && typeof host.sessionId === "string" ? host.sessionId : null;
    if (Array.isArray(snap.sessions)) state.sessions = snap.sessions;
    var wasFailing = state.failures >= OFFLINE_AFTER_FAILURES;
    state.snapshot = snap;
    state.switching = false;
    // the viewed subagent is gone from the session: back to the main agent
    if (state.threadId && !snap.thread) state.threadId = null;
    state.threadPending = false;
    state.failures = 0;
    state.lastError = null;
    state.lastSuccessAt = Date.now();
    if (wasFailing) announce("连接已恢复");
    render();
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  function rebind(manual, message) {
    state.manualSessionId = manual;
    state.threadId = null;
    state.epoch++;
    state.switching = true;
    savePrefs();
    announce(message);
    render();
    pollSoon();
  }
  // An explicit pick is sticky — even when it is the current chat today.
  function selectSession(id) {
    if (typeof id !== "string" || !UUID_RE.test(id)) return;
    if (id === state.manualSessionId && !state.switching) return;
    rebind(id, "已固定到所选会话");
  }
  function backToHost() {
    if (state.manualSessionId === null && !state.switching) return;
    rebind(null, "已回到当前会话");
  }
  function manualRefresh() {
    if (state.phase === "failed" || state.phase === "torndown") { state.failures = 0; connect(); return; }
    if (state.phase !== "ready") return;
    state.failures = 0;
    state.listWanted = true;
    state.refreshAsked = true;
    if (state.paused) state.paused = false;
    pollSoon();
  }
  function setAllKinds(v) {
    state.allKinds = !!v;
    state.sessions = null;
    savePrefs();
    render();
    pollSoon();
  }
  function setThemePref(v) {
    state.themePref = v;
    savePrefs();
    applyTheme();
  }
  function setTab(t, focus) {
    if (TABS.indexOf(t) < 0) return;
    var changed = state.tab !== t;
    state.tab = t;
    if (changed) savePrefs();
    renderTabsSelection();
    renderPanel();
    var el = $("tab-" + t);
    if (focus) el.focus();
    scrollTabIntoView(el);
  }

  // ------------------------------------------------------------------
  // Announcer / toast / busy
  // ------------------------------------------------------------------
  function announce(msg) {
    var a = $("announcer");
    a.textContent = "";
    setTimeout(function () { a.textContent = msg; }, 30);
  }
  var toastTimer = null;
  function toast(msg) {
    var slot = $("toast-slot");
    clear(slot);
    slot.appendChild(h("div", { class: "toast", role: "status", text: msg }));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { clear(slot); }, 2000);
  }
  function setBusy(b) { $("btn-refresh").setAttribute("data-busy", b ? "true" : "false"); }

  function connection() {
    if (state.phase === "standalone") return "standalone";
    if (state.phase === "failed") return "failed";
    if (state.phase === "torndown") return "closed";
    if (state.phase === "connecting") return "connecting";
    if (state.paused) return "paused";
    if (!state.snapshot) return state.failures ? "firstfail" : "loading";
    if (state.failures >= OFFLINE_AFTER_FAILURES) return "offline";
    if (state.failures > 0) return "stale";
    return "live";
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  var renderKeys = {};
  function keyed(slot, key, build) {
    if (renderKeys[slot] === key) return false;
    renderKeys[slot] = key;
    build();
    return true;
  }
  function relSpan(ts) { return h("span", { data: { rel: isNum(ts) ? ts : null }, text: fmtRel(ts, now()) }); }
  function untilSpan(ts) { return h("span", { data: { until: isNum(ts) ? ts : null }, text: fmtUntil(ts, now()) }); }
  // a rule's remaining time, in whole minutes rounded down so "≥" holds: "12 分", "<1 分"
  var LIVE = {
    lefts: function (ts, t) { var m = Math.floor((ts - t) / 60000); return m >= 1 ? m + " 分" : "<1 分"; },
    refleft: function (ts, t) { var m = Math.floor((ts - t) / 60000); return m >= 1 ? "参考 ≥" + m + " 分" : "参考 <1 分"; }
  };
  function liveSpan(kind, ts) { return h("span", { data: { live: kind, at: ts }, text: LIVE[kind](ts, now()) }); }
  function errText(e) { return e ? String(typeof e.message === "string" ? e.message : e).slice(0, 160) : ""; }

  function session() { var s = state.snapshot; return !state.switching && s && s.session ? s.session : null; }
  // What the cards show: the subagent chosen in the thread picker, else the session's main agent
  function view() {
    var s = session(), snap = state.snapshot;
    if (s && state.threadId && snap.thread && snap.thread.id === state.threadId) return snap.thread;
    return s;
  }

  function render() {
    renderSync();
    renderThreads();
    renderSessionRow();
    renderBanner();
    renderRibbon();
    renderPanel();
    renderFoot();
    if (state.menuOpen) renderMenu();
    $("btn-refresh").disabled = state.phase === "connecting" || state.phase === "standalone";
  }

  // ---- sync, in the footer: when the log last moved, or why it did not -----
  function renderSync() {
    var s = session();
    var conn = connection();
    var sync = $("sync");
    sync.setAttribute("data-state", conn);
    clear(sync);
    if (conn === "offline") sync.textContent = "连接中断";
    else if (conn === "stale") sync.textContent = "重试中";
    else if (conn === "paused") sync.textContent = "已暂停";
    else if (s && isNum(s.lastEventAt)) {
      sync.textContent = "更新 " + fmtWhen(s.lastEventAt);
    }
    sync.title = s ? "会话日志最近写入于 " + fmtStamp(s.lastEventAt) + "；面板每 2 秒读取一次" : "";
  }

  // ---- session selector ------------------------------------------------
  function sessionOptionText(s) {
    var bits = [s.project || "未知目录"];
    if (KIND[s.kind]) bits.push(KIND[s.kind]);
    bits.push(fmtRel(s.lastActivityAt, now()));
    bits.push("…" + (s.shortId || ""));
    return bits.join(" · ") + (s.isHost ? "（当前）" : "");
  }
  function renderSessionRow() {
    var sel = $("session-select");
    var snap = state.snapshot;
    var ready = state.phase === "ready" || state.phase === "torndown";
    var list = state.sessions || [];
    var manual = state.manualSessionId;
    var autoLabel = state.hostSessionId ? "当前会话（自动）"
      : hostCandidate() ? "当前会话（本机未找到）"
      : state.hostAmbiguous ? "当前会话（无法确定）" : "当前会话（未识别）";
    var rows = list.map(function (s) { return [s.id, sessionOptionText(s)]; });
    if (manual && !list.some(function (s) { return s.id === manual; })) {
      var ms = snap && snap.session && snap.session.id === manual ? snap.session : null;
      rows.unshift([manual, ms ? (ms.project || "未知目录") + " · …" + ms.shortId : "…" + manual.slice(-8)]);
    }
    // Rebuilding the options of an open native picker would close it, so the
    // list is refreshed only while the control is not focused.
    var key = [ready, autoLabel, JSON.stringify(rows.map(function (r) { return r[0] + "|" + r[1].replace(/ · [^·]*前 · /, " · "); }))].join("|");
    if (document.activeElement !== sel) {
      keyed("select", key, function () {
        clear(sel);
        if (!ready) { sel.appendChild(h("option", { value: "", text: state.phase === "connecting" ? "读取中…" : "不可用" })); return; }
        sel.appendChild(h("option", { value: AUTO, text: autoLabel }));
        if (rows.length) {
          var grp = h("optgroup", { label: "最近会话" });
          rows.forEach(function (r) { grp.appendChild(h("option", { value: r[0], text: r[1] })); });
          sel.appendChild(grp);
        }
      });
      sel.value = ready ? (manual || AUTO) : "";
    }
    sel.disabled = !ready || state.phase === "torndown";
    var wrap = $("select-wrap");
    wrap.setAttribute("data-mode", manual ? "manual" : "auto");
    $("select-lead").firstChild.setAttribute("href", manual ? "#i-pin" : "#i-chat");
    // A pinned session is the one exception to "the chat you are in": say so, once, above the card
    var note = $("binding-note");
    var pinned = ready && !!manual;
    var ms = snap && snap.session && snap.session.id === manual ? snap.session : null;
    keyed("note", [pinned, manual, ms && ms.project].join("|"), function () {
      clear(note);
      note.hidden = !pinned;
      if (!pinned) return;
      note.appendChild(icon("i-pin"));
      note.appendChild(h("span", { class: "pinned-text" }, "正在查看 ", h("b", { class: "mono", text: manual.slice(-8) }), ms && ms.project ? " · " + ms.project : ""));
      note.appendChild(h("button", { type: "button", class: "link-btn", on: { click: backToHost } }, "回到当前"));
    });
  }

  // ---- banner ----------------------------------------------------------
  function renderBanner() {
    var slot = $("banner-slot");
    var conn = connection();
    var snap = state.snapshot;
    var w = (snap && !state.switching && snap.warnings) || [];
    var key = [conn, errText(state.lastError), w.join(",")].join("|");
    keyed("banner", key, function () {
      clear(slot);
      function banner(tone, ico, text, action) {
        slot.appendChild(h("div", { class: "banner", "data-tone": tone, role: tone === "crit" ? "alert" : null },
          icon(ico), h("span", { class: "banner-text", text: text }), action || null));
      }
      if (conn === "offline") {
        banner("crit", "i-warn", "连接中断，显示的是上次的数据。",
          h("button", { type: "button", class: "btn", on: { click: manualRefresh }, text: "重试" }));
        return;
      }
      if (conn === "stale") { banner("warn", "i-warn", "刷新失败，正在重试…"); return; }
      if (!snap) return;
      if (w.indexOf("no_session_roots") >= 0) banner("crit", "i-warn", "没有找到 Codex 会话目录，见右下角菜单的「诊断信息」。");
      else if (w.indexOf("session_id_mismatch") >= 0) banner("warn", "i-warn", "日志内的会话 ID 与文件名不一致，可能是复制或改名的文件。");
      else if (w.indexOf("read_error") >= 0) banner("warn", "i-warn", "读取日志出错，显示的是上次的数据。");
      // the thread ran on into further log files, and not all of them could be read as its own
      else if (w.indexOf("segments_ambiguous") >= 0) banner("warn", "i-warn", "这个会话有两份后续日志接在同一处，无法确定衔接，显示的可能不是最新数据。");
      else if (w.indexOf("segments_unjoined") >= 0) banner("warn", "i-warn", "这个会话还有后续日志未能核对衔接，显示的可能不是最新数据。");
      else if (w.indexOf("segments_dropped") >= 0) banner("warn", "i-warn", "这个会话的一份后续日志已不能核对，已重新读取，数据可能不完整。");
      else if (w.indexOf("segments_late") >= 0) banner("warn", "i-warn", "较早的日志在交接后仍有写入，这部分未计入，数据可能不完整。");
      else if (w.indexOf("catching_up") >= 0) banner("info", "i-sigma", "日志较大，正在分批读取…");
    });
  }

  // ---- ribbon ----------------------------------------------------------
  function quotaWindow(rl) {
    if (!rl || !Array.isArray(rl.windows)) return null;
    var best = null;
    rl.windows.forEach(function (w) {
      if (!isNum(w.usedPercent)) return;
      if (!best || w.usedPercent > best.usedPercent) best = w;
    });
    return best || rl.windows[0] || null;
  }
  function speedGroup(t) {
    var r = (t && Array.isArray(t.series)) ? t.series : [];
    return r.filter(function (x) { return x.inAggregate && isNum(x.tokensPerSecond); }).slice(-GROUP);
  }

  /* pure:begin — quantity geometry, free of the DOM (tests/test_ui_geometry.py runs it in node).
   * A quantity is drawn at its true proportion: no minimum length, round ends taken out of the
   * length they would add, nothing drawn for a zero, a full shape only at the whole. */
  var Geom = {
    RING_R: 5.25,
    RING_W: 2,
    // a ring filling clockwise from 12 o'clock: round ends once the arc is longer than both of
    // them together, their length taken out of the dash and the start turned to 12 o'clock;
    // below that, flat ends at the true length
    ringArc: function (p) {
      var C = 2 * Math.PI * Geom.RING_R, w = Geom.RING_W;
      if (!(p > 0)) return null;
      if (p >= 1) return { full: true, C: C };
      if (C * p >= w) return { dash: C * p - w, cap: "round", rotate: -90 + (w / 2) / C * 360, C: C, visible: C * p };
      return { dash: C * p, cap: "butt", rotate: -90, C: C, visible: C * p };
    },
    // the hourglass's sand by area: above, the share of the minimum retention still to run;
    // below, the share gone. The upper bulb widens from 1 at the neck to 4.6 over 4.2; the lower
    // narrows from 4.6 at the floor to 1.8 over 3.6.
    sandTop: function (left) {
      var depth = 4.2, k = 1.8 / depth, total = depth + k * depth * depth;
      if (!(left > 0)) return 0;
      if (left >= 1) return depth;
      var A = left * total;
      return (-1 + Math.sqrt(1 + 4 * k * A)) / (2 * k);
    },
    sandBottom: function (gone) {
      var h = 3.6, a = 1.4 / h, total = 4.6 * h - a * h * h;
      if (!(gone > 0)) return 0;
      if (gone >= 1) return h;
      var A = gone * total;
      return (4.6 - Math.sqrt(4.6 * 4.6 - 4 * a * A)) / (2 * a);
    },
    // a bar's parts, each from its true start to its true end (fractions of the whole), and the
    // gaps cut between two parts: centred on the boundary, or out of the wider neighbour where
    // a part is a sliver (under `sliver` of the whole), so no part lends its width to another
    barParts: function (fracs, sliver) {
      var parts = [], gaps = [], cum = 0, s = isFinite(sliver) ? sliver : 0.015;
      fracs.forEach(function (f, i) {
        if (!(f > 0)) return;
        var w = Math.min(f, Math.max(0, 1 - cum));
        if (!(w > 0)) return;
        parts.push({ i: i, start: cum, end: cum + w });
        cum += w;
      });
      for (var j = 1; j < parts.length; j++) {
        var a = parts[j - 1], b = parts[j], at = b.start;
        var align = (a.end - a.start) < s ? "after" : (b.end - b.start) < s ? "before" : "centre";
        gaps.push({ at: at, align: align });
      }
      return { parts: parts, gaps: gaps, end: cum };
    },
    // column heights out of a plot height, by the largest value; a zero is no column
    columns: function (values, plot, max) {
      var m = isFinite(max) && max > 0 ? max : Math.max.apply(null, values.concat([0]));
      return values.map(function (v) { return v > 0 && m > 0 ? Math.min(1, v / m) * plot : 0; });
    },
    // labels under a bar, each led to the middle of its stretch: a label's square (7px, its centre
    // 3.5px in) stands on its line, held inside the width; one that would come within 12px of the
    // label before it takes the next row down. A label right of the middle opens its readout
    // leftward ("end"). Items are {at: 0..1, w: px}; returns each {left, row, x, edge}, and rows.
    leaderLayout: function (W, items) {
      var lastEnd = -Infinity, rows = 1;
      var out = items.map(function (it) {
        var x = Math.max(0.5, Math.min(W - 0.5, it.at * W));
        var left = Math.max(0, Math.min(W - it.w, x - 3.5));
        var row = left < lastEnd + 12 ? rows++ : 0;
        lastEnd = Math.max(lastEnd, left + it.w);
        return { left: left, row: row, x: x, edge: left + it.w / 2 > W / 2 ? "end" : "start" };
      });
      return { items: out, rows: rows };
    },
    // a set of readings on two columns, or, when any one would not hold its name and figure on a
    // line of its column, on one: all its rows then full width, so a pair compared (若命中 /
    // 若未命中) keeps one line each and one edge for its figures. Cells are {k, v} widths in px.
    phrasesStack: function (W, gap, inner, cells) {
      var col = (W - gap) / 2;
      return cells.some(function (c) { return c.k + inner + c.v > col + 0.5; });
    },
    // a chosen column's reading on the line under a chart W wide, w its width on one line, x the
    // column's middle: centred under the column while it fits, held to the chart's edges; one
    // wider than the chart starts at its left and wraps within it, never past its right
    readingPlace: function (W, w, x) {
      if (w > W) return { left: 0, width: W, wrap: true };
      return { left: Math.max(0, Math.min(W - w, x - w / 2)), width: w, wrap: false };
    }
  };
  /* pure:end */

  // The marks: each a small copy of its card's graphic, 16px tall, in the tab's colour.
  var clipSeq = 0;
  function frac(r) { return isNum(r) ? Math.max(0, Math.min(1, r)) : 0; }
  function glyphBox(w) { return svg("svg", { viewBox: "0 0 " + w + " 16", width: w, height: 16, focusable: "false" }); }
  // the context: a 20 × 4 bar filling left to right, its round ends cut by a clip the track's shape
  function capsuleGlyph(ratio) {
    var s = glyphBox(20);
    s.appendChild(svg("rect", { x: 0, y: 6, width: 20, height: 4, rx: 2, class: "g-ftrack" }));
    var r = frac(ratio);
    if (r > 0) {
      var id = "ds-cap-" + (++clipSeq);
      var cp = svg("clipPath", { id: id });
      cp.appendChild(svg("rect", { x: 0, y: 6, width: 20, height: 4, rx: 2 }));
      s.appendChild(cp);
      s.appendChild(svg("rect", { x: 0, y: 6, width: (20 * r).toFixed(2), height: 4, class: "g-ffill", "clip-path": "url(#" + id + ")" }));
    }
    return s;
  }
  // a quota window: a ring filling clockwise as it is used
  function ringGlyph(usedRatio) {
    var s = glyphBox(14), R = Geom.RING_R, W = Geom.RING_W;
    s.appendChild(svg("circle", { cx: 7, cy: 8, r: R, fill: "none", class: "g-track", "stroke-width": W }));
    var a = Geom.ringArc(isNum(usedRatio) ? usedRatio : 0);
    if (a && a.full) s.appendChild(svg("circle", { cx: 7, cy: 8, r: R, fill: "none", class: "g-fill", "stroke-width": W }));
    else if (a) s.appendChild(svg("circle", {
      cx: 7, cy: 8, r: R, fill: "none", class: "g-fill", "stroke-width": W, "stroke-linecap": a.cap,
      "stroke-dasharray": a.dash.toFixed(3) + " " + a.C.toFixed(3), transform: "rotate(" + a.rotate.toFixed(3) + " 7 8)"
    }));
    return s;
  }
  // the cache: an hourglass, 10 × 13. Within the model's minimum retention the sand above is
  // the share still to run and the sand below the share gone; past it, or with no rule, the
  // frame alone (not known); after a switch of model, all run down.
  function hourglassGlyph(o) {
    var s = glyphBox(10);
    var top = 1.5, bot = 14.5, cx = 5, neck = 7.3, depth = 4.2;
    s.appendChild(svg("path", { class: "g-frame", "stroke-width": 1.15, "stroke-linecap": "round", "stroke-linejoin": "round",
      d: "M1 " + top + "H9M1 " + bot + "H9M2 " + top + "v1.5c0 2.2 2.6 3 2.6 5s-2.6 2.8-2.6 5v1.5M8 " + top + "v1.5c0 2.2-2.6 3-2.6 5s2.6 2.8 2.6 5v1.5" }));
    var gone = !o ? 0 : o.phase === "switched" ? 1 : o.phase === "within" ? frac(o.used) : 0;
    var left = o && o.phase === "within" ? 1 - gone : 0;
    var h = Geom.sandTop(left);
    if (h > 0) {
      var yt = neck - h, hw = 0.5 + (h / depth) * 1.8;
      s.appendChild(svg("path", { class: "g-ffill", d: "M" + (cx - hw).toFixed(2) + " " + yt.toFixed(2) + "H" + (cx + hw).toFixed(2) + "L" + (cx + 0.5) + " " + neck + "H" + (cx - 0.5) + "Z" }));
    }
    var z = Geom.sandBottom(gone);
    if (z > 0) {
      var floor = bot - 1.1, hw2 = 2.3 - (z / 3.6) * 1.4;
      s.appendChild(svg("path", { class: "g-ffill", d: "M" + (cx - 2.3) + " " + floor.toFixed(2) + "H" + (cx + 2.3) + "L" + (cx + hw2).toFixed(2) + " " + (floor - z).toFixed(2) + "H" + (cx - hw2).toFixed(2) + "Z" }));
    }
    return s;
  }
  // the speed: the last five responses as columns by the largest of them, the latest in full;
  // a slot with no response yet is a hairline stub, not a short column
  function columnsGlyph(values) {
    var n = 5, bw = 2.2, gap = 1.6, w = n * bw + (n - 1) * gap;
    var s = glyphBox(18);
    var shown = (values || []).slice(-n);
    var hs = Geom.columns(shown, 12);
    var floor = 14, x0 = (18 - w) / 2;
    for (var i = 0; i < n; i++) {
      var k = i - (n - shown.length), x = (x0 + i * (bw + gap)).toFixed(2);
      if (k < 0) { s.appendChild(svg("rect", { x: x, y: floor - 0.75, width: bw, height: 0.75, class: "g-stub" })); continue; }
      if (!(hs[k] > 0)) continue;
      s.appendChild(svg("rect", { x: x, y: (floor - hs[k]).toFixed(2), width: bw, height: hs[k].toFixed(2), class: i === n - 1 ? "g-ffill" : "g-fsoft" }));
    }
    return s;
  }
  function glyphInto(name, el) {
    var slot = document.querySelector('[data-glyph="' + name + '"]');
    clear(slot); slot.appendChild(el);
  }

  // Thresholds, judged on the number shown so that a figure and its colour never disagree:
  // the context and a quota by what is used.
  function usedTone(pct) { return !isNum(pct) ? "muted" : pct >= 80 ? "crit" : pct >= 60 ? "warn" : null; }
  function quotaTone(pct) { return !isNum(pct) ? "muted" : pct >= 90 ? "crit" : pct >= 70 ? "warn" : null; }

  function setTab_(id, value, tone, aria, opts) {
    var tab = $("tab-" + id);
    tab.querySelector("[data-v]").textContent = value;
    if (opts && opts.label !== undefined) tab.querySelector("[data-l]").textContent = opts.label;
    var suf = tab.querySelector("[data-s]");
    if (suf && opts && "suffix" in opts) {
      suf.textContent = opts.suffix || "";
      suf.hidden = !opts.suffix;
      if (opts.suffixTone) suf.setAttribute("data-tone", opts.suffixTone); else suf.removeAttribute("data-tone");
    }
    var flag = tab.querySelector(".tab-flag");
    if (flag) {
      flag.textContent = (opts && opts.flag) || "";
      flag.hidden = !(opts && opts.flag);
      if (opts && opts.flagTone) flag.setAttribute("data-tone", opts.flagTone); else flag.removeAttribute("data-tone");
    }
    if (tone) tab.setAttribute("data-tone", tone); else tab.removeAttribute("data-tone");
    tab.setAttribute("aria-label", aria);
  }

  function renderRibbon() {
    var wrap = $("ribbon-wrap");
    var snap = state.snapshot;
    var s = session();
    var show = !!(snap && (s || state.switching) && (state.phase === "ready" || state.phase === "torndown"));
    wrap.hidden = !show;
    if (!show) return;
    // the figures are the viewed thread's; the quota is the account's either way
    s = view();
    var c = s && s.context, cache = s && s.cache, t = s && s.throughput;
    var rl = s ? snap.rateLimits : null;
    var qw = quotaWindow(rl);
    var group = speedGroup(t);
    var o = s ? cacheOutlook(s) : null;
    var leftMin = o && o.phase === "within" ? Math.ceil(o.left / 60000) : null;
    var sc = s ? sessionCost(s) : null;
    var key = JSON.stringify([!!s, c && [c.state, c.ratio], qw, cache && cache.latest && cache.latest.ratio, o && [o.phase, o.until, leftMin],
      t && [t.current, t.model, t.aggregate && t.aggregate.tokensPerSecond], group.map(function (g) { return g.tokensPerSecond; }),
      sc && [sc.priced, sc.total, sc.tokens, sc.usageMissing]]);
    keyed("ribbon", key, function () {
      // context
      var cr = c && c.state === "ok" ? c.ratio : null;
      var cp = isNum(cr) ? pctNum(cr) : null;
      setTab_("context", cp !== null ? cp + "%" : "—", usedTone(cp !== null ? Number(cp) : null),
        "上下文，" + (cp !== null ? "已用 " + cp + "%" : "暂无数据"));
      glyphInto("context", capsuleGlyph(cr));
      // quota: the tightest window, as used
      var used = qw && isNum(qw.usedPercent) ? Math.min(100, Math.max(0, qw.usedPercent)) : null;
      var up = isNum(used) ? pctOf(used) : null;
      var wname = qw ? windowShort(qw.windowMinutes) : "";
      setTab_("quota", up !== null ? up + "%" : "—", qw && qw.expired ? "muted" : quotaTone(up !== null ? Number(up) : null),
        wname + "额度，" + (up !== null ? "已用 " + up + "%，剩余 " + pctOf(100 - used) + "%" : "暂无数据") + (qw && qw.expired ? "，已过重置时间" : ""),
        { label: qw ? wname.replace(" ", "") : "额度" });
      glyphInto("quota", ringGlyph(isNum(used) ? used / 100 : null));
      // cache: the last request's measured hit rate; the hourglass and the words, the model's rule
      var lr = cache && cache.latest && isNum(cache.latest.ratio) ? cache.latest.ratio : null;
      var hp = isNum(lr) ? pctNum(lr) : null;
      var phase = o ? o.phase : null;
      var ctone = hp === null ? "muted" : phase === "switched" ? "warn" : phase === "within" ? null : "quiet";
      // the measured rate keeps its word; a switch of model is a flag beside the name
      setTab_("cache", hp !== null ? hp + "%" : "—", ctone,
        "缓存，" + (hp !== null ? "上次命中 " + hp + "%" : "暂无数据") +
        (phase === "switched" ? "，已切换模型，下次请求预计读不到原模型的缓存" : phase === "within" ? "，保留参考：按模型规则推算的最短保留时长还剩 " + fmtSpan(o.left)
          : phase === "beyond" ? "，已超过最短保留时长，状态未确认" : ""),
        { suffix: "命中", flag: phase === "switched" ? "换模型" : "", flagTone: "warn" });
      glyphInto("cache", hourglassGlyph(o));
      // the rule's reference beside the measured rate, with its separator, both gone together
      var ct = $("tab-cache").querySelector(".tab-time");
      clear(ct);
      if (phase === "within" || phase === "beyond") ct.appendChild(h("span", { class: "tab-sep", "aria-hidden": "true", text: "·" }));
      if (phase === "within") ct.appendChild(liveSpan("refleft", o.until));
      else if (phase === "beyond") ct.appendChild(document.createTextNode("未确认"));
      ct.hidden = phase !== "within" && phase !== "beyond";
      // speed
      var tps = t && t.aggregate ? t.aggregate.tokensPerSecond : null;
      // a speed from the model or effort before a switch: the figure stays, quieter, and says so
      var before = isNum(tps) && t.current === false;
      setTab_("speed", isNum(tps) ? fmtRate(tps) : "—", isNum(tps) ? (before ? "quiet" : null) : "muted",
        "输出速度，" + (isNum(tps) ? (before ? "切换前的 " + [t.model, t.effort].filter(Boolean).join(" · ") + "，" : "") +
          "近 " + t.aggregate.samples + " 次平均 " + fmtRate(tps) + " tok/s" : "暂无数据"),
        { flag: before ? "切换前" : "" });
      glyphInto("speed", columnsGlyph(group.map(function (g) { return g.tokensPerSecond; })));
      // the session's bill, where its models are priced; else its tokens. A bill missing some
      // of its usage says so beside the figure, not only in the card
      var priced = sc && sc.priced;
      setTab_("session", priced ? fmtUsd(sc.total) : sc && sc.tokens ? fmtK(sc.tokens) : "—", s ? null : "muted",
        "本会话，" + (priced ? "API 价估算 " + fmtUsd(sc.total) + (sc.usageMissing ? "，只含已记录的用量，不完整" : "")
          : sc && sc.tokens ? fmtK(sc.tokens) + " tokens" : s ? "还没有完成的请求" : "读取中"),
        { suffix: priced ? (sc.usageMissing ? "不完整" : "") : sc && sc.tokens ? "tokens" : "" });
    });
    renderTabsSelection();
    updateRibbonFade();
  }

  function renderTabsSelection() {
    TABS.forEach(function (id) {
      var tab = $("tab-" + id);
      var on = id === state.tab;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
    });
    $("panel").setAttribute("aria-labelledby", "tab-" + state.tab);
    $("app").setAttribute("data-tab", state.tab);
  }
  function reduced() { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  function scrollTabIntoView(el) {
    var rib = $("ribbon");
    if (!el || rib.scrollWidth <= rib.clientWidth) return;
    var l = el.offsetLeft - rib.offsetLeft, r = l + el.offsetWidth;
    var target = null;
    if (l < rib.scrollLeft + 12) target = l - 12;
    else if (r > rib.scrollLeft + rib.clientWidth - 12) target = r - rib.clientWidth + 12;
    if (target !== null) rib.scrollTo({ left: Math.max(0, target), behavior: reduced() ? "auto" : "smooth" });
  }
  function updateRibbonFade() {
    var rib = $("ribbon"), wrap = $("ribbon-wrap");
    var over = rib.scrollWidth - rib.clientWidth > 1;
    wrap.setAttribute("data-fade-l", over && rib.scrollLeft > 2 ? "true" : "false");
    wrap.setAttribute("data-fade-r", over && rib.scrollLeft < rib.scrollWidth - rib.clientWidth - 2 ? "true" : "false");
  }

  // ---- threads: the main agent and the subagents it spawned, one viewed at a time ----
  // A subagent is known by its nickname and task (agent_path); its avatar is a small pattern
  // drawn from its id, so it keeps the same mark wherever it shows, as Codex gives each one.
  // identity hues, kept off amber and red (states) and off the input blue and output teal
  var AVATAR_HUES = ["#5866d1", "#9b4f96", "#5f7486", "#77803a", "#4f7f9e", "#7d6aa8"];
  function hash32(str) {
    var x = 2166136261;
    for (var i = 0; i < str.length; i++) { x ^= str.charCodeAt(i); x = Math.imul(x, 16777619) >>> 0; }
    return x >>> 0;
  }
  function avatar(thread, size) {
    var n = size || 16;
    var s = svg("svg", { viewBox: "0 0 16 16", width: n, height: n, class: "avatar", "aria-hidden": "true", focusable: "false" });
    if (!thread) {
      // the main agent: a quiet ring around a point, the thread the others branch from
      s.setAttribute("data-main", "true");
      s.appendChild(svg("circle", { cx: 8, cy: 8, r: 6.25, class: "av-ring", fill: "none", "stroke-width": 1.3 }));
      s.appendChild(svg("circle", { cx: 8, cy: 8, r: 2.4, class: "av-core" }));
      return s;
    }
    var x = hash32(thread.id || "");
    s.style.color = AVATAR_HUES[x % AVATAR_HUES.length];
    s.appendChild(svg("rect", { x: 0, y: 0, width: 16, height: 16, rx: 4.5, class: "av-ground" }));
    // a 3 × 3 pattern mirrored about the middle column: six bits pick the dots
    var bits = x >>> 3, on = 0;
    for (var row = 0; row < 3; row++) {
      for (var col = 0; col < 2; col++) {
        if (!((bits >>> (row * 2 + col)) & 1)) continue;
        on++;
        [col, 2 - col].forEach(function (c, k) {
          if (k === 1 && c === col) return;
          s.appendChild(svg("circle", { cx: 4 + c * 4, cy: 4 + row * 4, r: 1.45, class: "av-dot" }));
        });
      }
    }
    if (!on) s.appendChild(svg("circle", { cx: 8, cy: 8, r: 1.6, class: "av-dot" }));
    return s;
  }
  function taskName(c) {
    var p = c && c.path ? String(c.path).split("/").filter(Boolean).pop() : "";
    return p ? p.replace(/[_-]+/g, " ") : "";
  }
  function threadName(c) { return !c ? "主代理" : c.name || (c.kind === "review" ? "自动审查" : "子代理 …" + (c.shortId || "")); }
  // running means a turn under way and the log still moving: a turn the log never closed is not
  var RUNNING_QUIET_MS = 10 * 60000;
  function isRunning(t) {
    if (!t) return false;
    var active = t.status === "active" || (t.turn && t.turn.status === "active");
    return active && isNum(t.lastEventAt) && now() - t.lastEventAt < RUNNING_QUIET_MS;
  }
  function turnText(t) {
    var st = (t && t.turn && t.turn.status) || "none";
    if (st === "active" && !isRunning(t)) st = "completed";
    return (TURN[st] || TURN.none).text;
  }
  // the session's threads: subagents to choose from, reviews summed, and what all of it cost
  function threadsOf(s) {
    var kids = (s && s.children) || [];
    var agents = kids.filter(function (c) { return c.kind !== "review"; });
    var reviews = kids.filter(function (c) { return c.kind === "review"; });
    // the sum of what could be priced, and how many threads it leaves out in whole or in part
    // (a model not in the table, or usage the log did not record), so a sum that is not all of
    // it never reads as the whole
    var all = { total: 0, priced: false, short: 0 };
    function add(sum, cost) {
      if (!cost.responses) return;
      if (cost.priced || cost.partial) { sum.total += cost.total; sum.priced = true; }
      if (!cost.priced || cost.usageMissing) sum.short += 1;
    }
    var main = sessionCost(s);
    add(all, main);
    var rows = agents.map(function (c) {
      var cost = sessionCost({ usage: c.usage });
      add(all, cost);
      return { c: c, cost: cost };
    });
    var review = { n: reviews.length, responses: 0, total: 0, priced: false, short: 0 };
    reviews.forEach(function (c) {
      var cost = sessionCost({ usage: c.usage });
      review.responses += c.responses || 0;
      add(review, cost);
      add(all, cost);
    });
    return { main: main, rows: rows, review: review, total: all.total, priced: all.priced, short: all.short };
  }

  function reviewCost(rv) { return { total: rv.total, priced: rv.priced && !rv.short, partial: rv.priced && rv.short > 0, usageMissing: 0 }; }

  function renderThreads() {
    var bar = $("threads");
    var s = session();
    var th = s ? threadsOf(s) : null;
    var show = !!(th && th.rows.length) && (state.phase === "ready" || state.phase === "torndown");
    bar.hidden = !show;
    if (!show) { if (state.threadMenuOpen) closeThreadMenu(false); return; }
    var cur = state.threadId ? th.rows.filter(function (r) { return r.c.id === state.threadId; })[0] : null;
    var key = JSON.stringify([state.threadId, th.rows.map(function (r) { return [r.c.id, r.c.name, r.c.status, costText(r.cost)]; }), costText(th.main), th.review, th.total, th.short, state.threadMenuOpen, state.threadSort]);
    keyed("threads", key, function () {
      // what had focus comes back after the rebuild: an agent's row, a sort button, or the picker
      var ae = document.activeElement, inBar = !!(ae && bar.contains(ae));
      var refocus = inBar ? ae.getAttribute("data-thread") : null;
      var refk = inBar && !refocus ? (ae.id === "thread-pick" ? "pick" : ae.getAttribute("data-fk")) : null;
      var oldList = bar.querySelector(".tm-list"), scrollTop = oldList ? oldList.scrollTop : 0;
      clear(bar);
      var pick = h("button", { type: "button", class: "thread-pick", id: "thread-pick", "aria-haspopup": "listbox", "aria-expanded": state.threadMenuOpen ? "true" : "false",
        "aria-controls": "thread-menu", "data-sub": cur ? "true" : null, title: "切换查看的代理",
        on: { click: function () { if (state.threadMenuOpen) closeThreadMenu(true); else openThreadMenu(); } } },
        avatar(cur ? cur.c : null), h("span", { class: "tp-name", text: threadName(cur ? cur.c : null) }),
        cur && taskName(cur.c) ? h("span", { class: "tp-task", text: taskName(cur.c) }) : null,
        icon("i-chevron", "tp-chev"));
      var summary = h("span", { class: "thread-sum" },
        th.rows.length + " 个子代理",
        th.rows.some(function (r) { return isRunning(r.c); }) ? h("span", { class: "run-dot", title: "运行中" }) : null,
        th.priced ? h("span", null, th.short ? " · 已计价 " : " · 全部 ", h("b", { text: fmtUsd(th.total) })) : null);
      bar.appendChild(pick);
      bar.appendChild(summary);
      if (refk === "pick") pick.focus();
      if (!state.threadMenuOpen) return;
      var menu = h("div", { class: "thread-menu", id: "thread-menu", role: "listbox", "aria-label": "代理", tabindex: "-1",
        on: { keydown: threadMenuKeys, focusout: threadMenuLeft } });
      function item(c, cost) {
        var on = (c ? c.id : null) === (state.threadId || null);
        var sub = c ? [taskName(c), c.model].filter(Boolean).join(" · ") : [s.model, s.serviceTier ? tierName(s.serviceTier) : null].filter(Boolean).join(" · ");
        return h("button", { type: "button", role: "option", class: "thread-item", "aria-selected": on ? "true" : "false", "data-thread": c ? c.id : "main",
          on: { click: function () { selectThread(c ? c.id : null); } } },
          avatar(c, 20),
          h("span", { class: "ti-text" }, h("span", { class: "ti-name", text: threadName(c) }), sub ? h("span", { class: "ti-sub", text: sub }) : null),
          isRunning(c || s) ? h("span", { class: "run-dot", title: "运行中" }) : null,
          h("span", { class: "ti-cost", text: costText(cost) }),
          icon("i-check", "ti-check"));
      }
      // the main agent stays on top; the subagents below it, newest first or dearest first,
      // in a list that scrolls once there are more than a few
      menu.appendChild(item(null, th.main));
      var sortBtn = function (key, label) {
        return h("button", { type: "button", class: "tm-sort", "data-fk": "sort:" + key, "aria-pressed": state.threadSort === key ? "true" : "false",
          on: { click: function (ev) { ev.stopPropagation(); state.threadSort = key; state.threadMenuFocus = false; renderThreads(); } } }, label);
      };
      menu.appendChild(h("div", { class: "tm-head", role: "presentation" },
        h("span", { text: "子代理 · " + th.rows.length }),
        h("span", { class: "tm-sorts", role: "group", "aria-label": "排序" }, sortBtn("recent", "最新"), sortBtn("cost", "费用"))));
      var ordered = th.rows.slice().sort(state.threadSort === "cost"
        ? function (a, b) { return (b.cost.priced || b.cost.partial ? b.cost.total : -1) - (a.cost.priced || a.cost.partial ? a.cost.total : -1); }
        : function (a, b) { return (b.c.startedAt || 0) - (a.c.startedAt || 0); });
      var list = h("div", { class: "tm-list", role: "presentation" });
      ordered.forEach(function (r) { list.appendChild(item(r.c, r.cost)); });
      menu.appendChild(list);
      var foot = h("div", { class: "tm-foot", role: "presentation" },
        h("span", null, th.review.n ? "自动审查 " + th.review.responses + " 次" + (th.review.priced ? " · " + costText(reviewCost(th.review)) : "") : "全部线程"),
        th.priced ? h("span", null, th.short ? "已计价 " : "合计 ", h("b", { text: fmtUsd(th.total) })) : null);
      menu.appendChild(foot);
      if (th.priced && th.short) menu.appendChild(h("div", { class: "tm-note", role: "presentation",
        text: th.short + " 个线程未计价或用量不全，未全部计入" }));
      bar.appendChild(menu);
      list.scrollTop = scrollTop;
      var syncFade = function () { list.setAttribute("data-more", list.scrollTop + list.clientHeight < list.scrollHeight - 2 ? "true" : "false"); };
      list.addEventListener("scroll", syncFade, { passive: true });
      syncFade();
      var target = menu.querySelector('[data-thread="' + (refocus || (state.threadId || "main")) + '"]') || menu.querySelector(".thread-item");
      var sortEl = refk && refk !== "pick" ? menu.querySelector('[data-fk="' + refk + '"]') : null;
      if (sortEl) sortEl.focus();
      else if (target && (refocus || state.threadMenuFocus)) { target.focus(); state.threadMenuFocus = false; }
    });
  }
  function openThreadMenu() { state.threadMenuOpen = true; state.threadMenuFocus = true; renderThreads(); }
  function closeThreadMenu(restore) {
    if (!state.threadMenuOpen) return;
    state.threadMenuOpen = false;
    renderThreads();
    if (restore && $("thread-pick")) $("thread-pick").focus();
  }
  // Focus that leaves the picker and its menu (Tab past the last row) closes the menu. Judged a
  // moment later, once focus has landed: moving between rows, choosing a sort (which rebuilds
  // the menu and puts focus back in it) or the frame losing focus to the host leave it open.
  function threadMenuLeft() {
    setTimeout(function () {
      if (!state.threadMenuOpen) return;
      if (document.hasFocus && !document.hasFocus()) return;
      var a = document.activeElement, bar = $("threads");
      if (a && a !== document.body && bar.contains(a)) return;
      closeThreadMenu(false);
    }, 0);
  }
  function threadMenuKeys(ev) {
    var items = Array.prototype.slice.call(ev.currentTarget.querySelectorAll(".thread-item"));
    var i = items.indexOf(document.activeElement), n = null;
    if (ev.key === "ArrowDown") n = (i + 1) % items.length;
    else if (ev.key === "ArrowUp") n = (i - 1 + items.length) % items.length;
    else if (ev.key === "Home") n = 0;
    else if (ev.key === "End") n = items.length - 1;
    else if (ev.key === "Escape") { ev.preventDefault(); closeThreadMenu(true); return; }
    if (n === null) return;
    ev.preventDefault();
    items[n].focus();
  }
  function selectThread(id) {
    state.threadMenuOpen = false;
    if ((id || null) === (state.threadId || null)) { renderThreads(); $("thread-pick").focus(); return; }
    state.threadId = id || null;
    state.threadPending = !!id;
    state.epoch++;
    announce(id ? "正在查看子代理" : "已回到主代理");
    render();
    if ($("thread-pick")) $("thread-pick").focus();
    pollSoon();
  }

  // ---- panel -----------------------------------------------------------
  function stateBlock(title, text, actions, anim) {
    return h("div", { class: "state", data: { anim: anim ? "true" : null } },
      svgArt(),
      h("h2", { text: title }),
      text ? h("p", { text: text }) : null,
      actions && actions.length ? h("div", { class: "actions" }, actions) : null);
  }
  function svgArt() {
    var s = svg("svg", { class: "state-art", "aria-hidden": "true", focusable: "false" });
    s.appendChild(svg("use", { href: "#art-clepsydra" }));
    return s;
  }
  function skeleton() {
    return h("div", { class: "skeleton", "aria-hidden": "true" },
      h("div", { class: "skel-line skel-w60" }), h("div", { class: "skel-line skel-hero" }), h("div", { class: "skel-line skel-w80" }), h("div", { class: "skel-line" }));
  }

  function renderPanel() {
    var slot = $("panel");
    var conn = connection();
    var snap = state.snapshot;
    var stateKey = null, build = null;
    if (conn === "standalone") {
      stateKey = "standalone";
      build = function () {
        slot.appendChild(stateBlock("请在 Codex 中打开",
          "读取本机的 Codex 会话日志，显示上下文、额度、缓存与输出速度。安装插件后在 Codex 侧栏中打开。"));
      };
    } else if (conn === "failed") {
      stateKey = "failed|" + errText(state.initError);
      build = function () {
        slot.appendChild(stateBlock("无法连接 Codex", "没有收到宿主的回应。" + (state.initError ? "（" + errText(state.initError) + "）" : ""),
          [h("button", { type: "button", class: "btn btn-primary", on: { click: function () { connect(); } } }, icon("i-refresh"), "重新连接")]));
      };
    } else if (conn === "closed" && !snap) {
      stateKey = "closed";
      build = function () { slot.appendChild(stateBlock("面板已关闭", "宿主已结束此面板。")); };
    } else if (conn === "firstfail") {
      stateKey = "firstfail|" + errText(state.lastError);
      build = function () {
        slot.appendChild(stateBlock("暂时读不到数据", errText(state.lastError) || "正在重试。",
          [h("button", { type: "button", class: "btn", on: { click: manualRefresh } }, icon("i-refresh"), "重试")]));
      };
    } else if (conn === "connecting" || conn === "loading" || state.switching || state.threadPending) {
      stateKey = "loading";
      build = function () { slot.appendChild(skeleton()); };
    }
    if (!stateKey && snap) {
      var b = snap.binding || {};
      if (b.state === "manual_missing") {
        stateKey = "missing|" + b.sessionId;
        build = function () {
          slot.appendChild(stateBlock("所选会话已不在本机日志中", "可能已被清理或移动。",
            [h("button", { type: "button", class: "btn btn-primary", on: { click: backToHost } }, icon("i-back"), "回到当前会话")]));
        };
      } else if (!snap.session) {
        var host = b.host || {};
        var sug = b.suggestedSessionId;
        var sugRow = (state.sessions || []).filter(function (s) { return s.id === sug; })[0];
        stateKey = "none|" + host.status + "|" + sug + "|" + (sugRow ? sugRow.project : "");
        build = function () {
          var why = host.status === "ambiguous" ? "宿主给出的会话信息互相矛盾。"
            : host.status === "unmatched" ? "当前对话的日志不在本机扫描的目录中。"
            : "宿主没有提供当前对话。";
          var actions = [];
          if (sug) actions.push(h("button", { type: "button", class: "btn btn-primary", on: { click: function () { selectSession(sug); } } },
            icon("i-pin"), "查看最近活动的会话" + (sugRow && sugRow.project ? "：" + sugRow.project : "")));
          actions.push(h("button", { type: "button", class: "btn", on: { click: function () { openMenu(); } } }, "选择会话"));
          slot.appendChild(stateBlock("未识别当前会话", why, actions));
        };
      }
    }
    if (stateKey) {
      keyed("panel", "state|" + stateKey, function () { rebuildKeepingFocus(slot, build); attachCharts(null); });
      return;
    }
    if (!snap || !snap.session) return;
    var s = view();
    var d = DETAIL[state.tab](s, snap);
    var key = JSON.stringify([state.tab, d.key, resolvedTheme()]);
    keyed("panel", key, function () {
      rebuildKeepingFocus(slot, function () { slot.appendChild(detailView(d)); });
      attachCharts(slot);
    });
  }

  // A card is rebuilt only when what it says changes (time-driven words tick in place). When it
  // is, the control the reader was on comes back: found again by its stable data-fk, or the
  // panel itself, never the page body; a chart's chosen point comes back with it.
  function rebuildKeepingFocus(slot, build) {
    var a = document.activeElement;
    var inside = a && a !== slot && slot.contains(a);
    var fk = inside ? a.getAttribute("data-fk") : null;
    clear(slot);
    build();
    if (!inside) return;
    var sel = fk ? '[data-fk="' + (window.CSS && CSS.escape ? CSS.escape(fk) : fk) + '"]' : null;
    var el = sel ? slot.querySelector(sel) : null;
    (el || slot).focus({ preventScroll: true });
  }

  // One card per tab, in the grammar of the desktop mod's F cards, which the user chose: a title,
  // and on the right a dot in the figure's colour before a short neutral sentence (where the figure
  // comes from, how fresh); one large figure with a short note beside it, and beside it too, where
  // a card has one, a small graphic of its own (the speed's columns); its one bar, a capsule, its
  // two ends in words beneath; the parts of a whole as a legend; the rest as short phrases, two to
  // a row; small bars of the same kind (another window, the cache's rule); the history, a layer
  // opened on asking; one footnote; exceptions last. No lines or panels divide it: size and
  // lightness do. The words a figure needs are behind it, on focus or hover, never dropped.
  function detailView(d) {
    var head = h("div", { class: "d-head" }, h("h2", { class: "d-title", text: d.title }),
      d.src ? tipped(h("span", { class: "d-src", "data-dot": d.dot || null }, d.src), d.srcTip, "src") : null);
    var figure = d.value === undefined ? null : h("p", { class: "figure" },
      h("span", { class: "big", "data-tone": d.tone || null, text: d.value }),
      d.unit ? h("span", { class: "unit", "data-u": d.unit, text: d.unit }) : null,
      d.note ? h("span", { class: "figure-note" }, d.note) : null);
    var lead = figure || d.side ? h("div", { class: "lead" }, h("div", { class: "lead-main" }, figure, d.side ? null : d.sub || null), d.side || null) : null;
    var notes = (d.notes || []).map(function (n) {
      return h("p", { class: "note", "data-tone": n.tone || null }, n.icon ? icon(n.icon) : null, h("span", null, n.text));
    });
    // beside a small chart, the line under them both is the figure's and, while a column is
    // chosen, its reading's: the card's width, never under the columns' feet
    return h("div", { class: "card" }, head, lead, d.side ? d.sub || null : null,
      d.viz ? h("div", { class: "viz" }, d.viz, d.leaders ? leaders(d.leaders.frac, d.leaders.cells) : null,
        d.caption ? caption(d.caption[0], d.caption[1]) : null) : null,
      d.legend ? legend(d.legend, "legend") : null,
      d.parts ? phrases(d.parts, "parts") : null,
      d.rows ? limRows(d.rows, d.rowsKind) : null,
      d.stats ? phrases(d.stats.cells, "stats", d.stats.head) : null,
      d.history && d.history.chart ? historyLayer(d.history) : null,
      d.fn ? tipped(h("p", { class: "fn" }, d.fn.text), d.fn.tip, "fn") : null,
      notes);
  }

  // The history: a layer at the card's foot, closed until asked for, its entry saying what it
  // holds and how many. Open or closed is kept per card, across polls and visits.
  function historyLayer(hs) {
    var open = !!state.histOpen[hs.key];
    var id = "hist-" + hs.key;
    var body = h("div", { class: "hist-body", id: id, hidden: !open },
      hs.note ? h("p", { class: "hist-note" }, hs.note) : null, hs.chart);
    var btn = h("button", { type: "button", class: "hist-toggle", "aria-expanded": open ? "true" : "false", "aria-controls": id,
      "data-fk": "hist:" + hs.key, on: { click: function () {
        var now_ = btn.getAttribute("aria-expanded") !== "true";
        state.histOpen[hs.key] = now_;
        btn.setAttribute("aria-expanded", now_ ? "true" : "false");
        body.hidden = !now_;
        savePrefs();
        if (now_) Array.prototype.forEach.call(body.querySelectorAll(".tc-plot"), function (p) { if (p._draw) p._draw(); });
      } } },
      h("span", { class: "hist-title", text: hs.title }), h("span", { class: "hist-n", text: hs.count }), icon("i-chevron", "hist-chev"));
    return h("section", { class: "hist" }, btn, body);
  }

  // An element whose words are on focus or hover: a readout of how a figure was reached, or
  // when. Its accessible name is what it shows and then the readout, kept current (labelFrom).
  function tipped(el, tip, fk) {
    if (!tip) return el;
    el.setAttribute("data-tip", tip);
    el.setAttribute("tabindex", "0");
    el.setAttribute("data-relabel", "");
    if (fk) el.setAttribute("data-fk", "tip:" + fk);
    labelFrom(el, function () { return el.textContent.replace(/\s+/g, " ").trim() + "。" + tip; });
    return el;
  }

  // A reading that can be focused for its readout: its accessible name read from what it shows
  function readable(el, c, kind, read) {
    if (c.tip) {
      el.setAttribute("data-tip", c.tip);
      el.setAttribute("tabindex", "0");
      el.setAttribute("data-fk", kind + ":" + c.k);
    }
    el.setAttribute("data-relabel", "");
    labelFrom(el, function () { return read() + (c.tip ? "。" + c.tip : ""); });
    return el;
  }

  // a figure's unit, small and faint: a Han unit (秒, 次) a quarter em off, %, k and tokens close
  function unitSpan(u) { return h("span", { class: /[\u4e00-\u9fff]/.test(u) ? "ph-u ph-u-han" : "ph-u", text: u }); }

  // Readings as short phrases, two to a row on the card's two columns: the name, then the figure
  // and its unit ("剩余 146 k"). Cells are {k, v, u, tone, tip}; a group may have a head
  // [what, aside], said above it.
  function phrases(cells, kind, head) {
    cells = (cells || []).filter(Boolean);
    if (!cells.length) return null;
    var box = h("div", { class: "phrases", "data-kind": kind, role: "list" });
    var measured = [];
    cells.forEach(function (c, i) {
      var name = h("span", { class: "ph-k", text: c.k });
      var num = h("span", { class: "ph-v" }, c.v);
      var unit = c.u ? unitSpan(c.u) : null;
      var fig = h("span", { class: "ph-fig" }, num, unit);
      var cell = h("div", { class: "ph", role: "listitem", "data-col": String(i % 2 + 1), "data-tone": c.tone || null }, name, fig);
      measured.push([name, fig]);
      readable(cell, c, kind, function () { return name.textContent + " " + num.textContent + (unit ? " " + unit.textContent : ""); });
      box.appendChild(cell);
    });
    // on a narrow card the set goes to one column as a whole, never one figure dropped alone
    box._draw = function () {
      var W = box.clientWidth;
      if (!W) return;
      var widths = measured.map(function (m) { return { k: m[0].getBoundingClientRect().width, v: m[1].getBoundingClientRect().width }; });
      // the gaps of panel.css: 28 between the columns, 8 between a name and its figure
      var stack = Geom.phrasesStack(W, 28, 8, widths);
      if (stack) box.setAttribute("data-stack", "true"); else box.removeAttribute("data-stack");
    };
    if (!head) return box;
    return h("div", { class: "group" },
      h("div", { class: "group-head" }, h("span", { text: head[0] }), head[1] ? h("span", { class: "gh-aside", text: head[1] }) : null), box);
  }

  // The bar's two stretches named where they lie, as the user asked of the context card: from the
  // middle of each, a short 1px line down to its label, a small square of its colour before the
  // name. The used stretch's label stands from its line; the free one's too, unless it would run
  // into the first, when it takes the line below and its line runs on to it. Only stretches the
  // log can vouch for are named (used and free); the widths are the true ones.
  function leaders(frac, cells) {
    var u = isNum(frac) ? Math.max(0, Math.min(1, frac)) : 0;
    var at = [u / 2, u + (1 - u) / 2];
    var box = h("div", { class: "leaders", role: "list" });
    var parts = cells.map(function (c, i) {
      var line = h("span", { class: "ld-line", "aria-hidden": "true" });
      var name = h("span", { class: "lg-k", text: c.k });
      var num = h("span", { class: "lg-v" }, c.v, c.u ? unitSpan(c.u) : null);
      var item = h("div", { class: "ld", role: "listitem" }, h("span", { class: "swatch", "data-tone": c.sw || null, "aria-hidden": "true" }), name, num);
      readable(item, c, "leader", function () { return name.textContent + " " + num.textContent; });
      box.appendChild(line);
      box.appendChild(item);
      return { line: line, item: item, at: at[i] };
    });
    var ROW = 18, DROP = 5;
    box._draw = function () {
      var W = box.clientWidth;
      if (!W) return;
      var g = Geom.leaderLayout(W, parts.map(function (p) { return { at: p.at, w: p.item.offsetWidth }; }));
      parts.forEach(function (p, i) {
        var o = g.items[i];
        p.item.style.left = o.left.toFixed(1) + "px";
        p.item.style.top = (DROP + o.row * ROW) + "px";
        p.item.setAttribute("data-edge", o.edge);
        // the 1px line centred on the square's middle, down into the label's top
        p.line.style.left = (o.x - 0.5).toFixed(1) + "px";
        p.line.style.height = (DROP + 3 + o.row * ROW) + "px";
      });
      box.style.height = (DROP + g.rows * ROW) + "px";
    };
    return box;
  }

  // The parts of a whole as a legend in two columns, down the first and then the second, in the
  // bar's order: the swatch of its segment, the name, the figure set flush right.
  function legend(cells, kind) {
    cells = (cells || []).filter(Boolean);
    if (!cells.length) return null;
    var rows = Math.ceil(cells.length / 2);
    var box = h("div", { class: "legend", role: "list", style: { "--rows": String(rows) } });
    cells.forEach(function (c, i) {
      var name = h("span", { class: "lg-k", text: c.k });
      var num = h("span", { class: "lg-v" }, c.v, c.u ? unitSpan(c.u) : null);
      var cell = h("div", { class: "lg", role: "listitem", "data-col": i < rows ? "1" : "2" },
        h("span", { class: "swatch", "data-tone": c.sw || null, "aria-hidden": "true" }), name, num);
      readable(cell, c, kind, function () { return name.textContent + " " + num.textContent; });
      box.appendChild(cell);
    });
    return box;
  }

  // Small bars of a card's own kind (another quota window, the cache's rule) on the same scale as
  // its main bar: the name and the figure on a line, the 8px bar the card's full width beneath,
  // and its two ends in words under that. Rows are {k, frac, tone, tick, fill, v, vtone, sub, tip}.
  function limRows(rows, kind) {
    rows = (rows || []).filter(Boolean);
    if (!rows.length) return null;
    var box = h("div", { class: "lims", role: "list", "data-kind": kind || null });
    rows.forEach(function (r) {
      var meter = bar([[r.frac, r.tone]], "", r.tick ? [r.tick] : null);
      meter.setAttribute("aria-hidden", "true");
      meter.removeAttribute("role");
      var m = meter.querySelector(".meter") || meter;
      m.removeAttribute("role"); m.removeAttribute("aria-label");
      if (r.fill) { var seg = m.querySelector(".seg"); if (seg) { seg.setAttribute("data-fill-until", String(r.fill.until)); seg.setAttribute("data-fill-span", String(r.fill.span)); } }
      var v = h("span", { class: "lim-v", "data-tone": r.vtone || null }, r.v);
      var sub = r.sub ? h("span", { class: "lim-sub" }, h("span", null, r.sub[0]), r.sub[1] ? h("span", null, r.sub[1]) : null) : null;
      var row = h("div", { class: "lim", role: "listitem" },
        h("span", { class: "lim-top" }, h("span", { class: "lim-k", text: r.k }), v), meter, sub);
      readable(row, { k: "lim:" + r.k, tip: r.tip }, "lim", function () {
        var ends = sub ? Array.prototype.map.call(sub.childNodes, function (x) { return x.textContent; }).join("，") : "";
        return r.k + "，" + v.textContent + (ends ? "，" + ends : "");
      });
      if (r.tip) row.setAttribute("data-fk", "lim:" + r.k);
      box.appendChild(row);
    });
    return box;
  }
  // A focusable row's accessible name, read from what the row shows; the ticker reads it again
  // after it moves a time in the row, so the name never keeps a minute the row has left behind.
  // Rows are not live regions: a changed name is there when focus or a reader comes to it.
  function labelFrom(el, read) { el._label = read; relabel(el); }
  function relabel(el) {
    if (!el._label) return;
    var t = el._label();
    if (el.getAttribute("aria-label") !== t) el.setAttribute("aria-label", t);
  }

  // ---- columns ---------------------------------------------------------------------------
  // The one chart, the F cards' columns: 6 wide, 4 apart, round heads on a 1px base, the latest
  // in full and the rest quiet; a reference a dotted line. Two sizes: small beside a figure (the
  // speed's twelve replies, 24 tall, from the right, the reading taken by the line under the
  // figure), and in a history layer (28 tall, from the left, as many as fit up to 24, the axis
  // under them). Older records stay a step away: the window follows the chosen column, and the
  // arrows at the axis's ends move it a window at a time. A zero is a hairline on the base, never
  // a short column; what was not recorded is not drawn. Keyboard and assistive technology read it
  // as a slider over the whole record: the arrow keys, Home, End and Page Up/Down choose a column,
  // and its time and value are its value text. Choosing reads; nothing is changed. Where the
  // window was left, it stays, across polls and visits; the slider's place and the first key
  // start from its last column.
  var COL_W = 6, COL_GAP = 4, COL_STEP = COL_W + COL_GAP;
  var MINI_N = 12, MINI_W = MINI_N * COL_STEP - COL_GAP, MINI_H = 25, HIST_N = 24, HIST_H = 29;
  var chartCursor = {};
  var chartWindow = {};   // per chart, the time its window ends at; absent, it keeps to the latest
  var chartObserver = typeof ResizeObserver === "function" ? new ResizeObserver(function (entries) {
    entries.forEach(function (e) { if (e.target._draw) e.target._draw(); });
  }) : null;
  // the plots in the card on screen, observed for their width; a card replaced is let go
  var observedPlots = [];
  function attachCharts(root) {
    observedPlots.forEach(function (p) { if (chartObserver) chartObserver.unobserve(p); });
    observedPlots = root ? Array.prototype.slice.call(root.querySelectorAll(".tc-plot, .leaders, .phrases, .lead-sub")) : [];
    observedPlots.forEach(function (p) {
      if (chartObserver) chartObserver.observe(p);
      else requestAnimationFrame(function () { if (p._draw) p._draw(); });
    });
  }
  // A reading's line (the speed's under its figure, a history's under its columns) is as tall as
  // the longest reading it may show, wrapped at its width, measured on a hidden copy of it: the
  // card does not grow or shrink as columns are chosen, at any width. Texts are what it may hold.
  function readingProbe() { return h("span", { class: "rd-probe", "aria-hidden": "true" }); }
  function reserveLine(line, probe, texts) {
    if (!line.clientWidth) return;
    var tall = 0;
    texts.forEach(function (t) {
      probe.textContent = t;
      var ht = probe.offsetHeight;
      if (isNum(ht) && ht > tall) tall = ht;
    });
    probe.textContent = "";
    line.style.minHeight = tall ? tall + "px" : "";
  }
  // a column with a round head and a flat foot on the base
  function colPath(x, floor, w, hgt) {
    var r = Math.min(w / 2, hgt), top = floor - hgt;
    return "M" + x.toFixed(2) + " " + floor + "V" + (top + r).toFixed(2) + "A" + r.toFixed(2) + " " + r.toFixed(2) + " 0 0 1 " +
      (x + w).toFixed(2) + " " + (top + r).toFixed(2) + "V" + floor + "Z";
  }
  function columnsChart(points, o) {
    var pts = (points || []).filter(function (p) { return isNum(p[0]) && isNum(p[1]); });
    if (pts.length < 1) return null;
    var n = pts.length, mini = !!o.mini, height = mini ? MINI_H : HIST_H;
    var pointText = o.pointText || function (i) { return fmtWhen(pts[i][0]) + " · " + o.fmt(pts[i][1]); };
    var box = h("div", { class: "tchart" + (mini ? " tc-mini" : ""), "data-tone": o.tone || null, "data-fk": "chart:" + o.key,
      tabindex: "0", role: "slider", "aria-roledescription": "历史读数", "aria-orientation": "horizontal",
      "aria-label": "浏览" + o.label + "的历史读数", "aria-valuemin": "1", "aria-valuemax": String(n) });
    var plot = h("div", { class: "tc-plot", style: mini ? { height: height + "px", width: MINI_W + "px" } : { height: height + "px" } });
    box.appendChild(plot);
    // in a history layer, the line under the columns: its two ends, or while a column is chosen,
    // that column's reading; arrows at the ends where the record runs on past the window
    var page = function (dir) {
      return h("button", { type: "button", class: "tc-page", "data-dir": dir, tabindex: "-1", "aria-hidden": "true",
        title: dir < 0 ? "更早" : "更近", hidden: true, on: { click: function () { turn(dir); } } }, icon("i-chevron"));
    };
    var prev = mini ? null : page(-1), next = mini ? null : page(1);
    var endL = h("span", { class: "tc-end" }), endR = h("span", { class: "tc-end tc-end-r" });
    var reading = h("span", { class: "tc-reading", hidden: true });
    var probe = mini ? null : readingProbe();
    var axis = mini ? null : h("div", { class: "tc-axis", "aria-hidden": "true" }, prev, endL, reading, endR, next, probe);
    if (axis) box.appendChild(axis);
    // the readings the line may show: in a history, the window's; beside a figure, all of them
    function readings(all) {
      var a = all ? 0 : off, b = all ? n : off + fit, t = [];
      for (var i = a; i < b; i++) t.push(pointText(i));
      return t;
    }
    var geo = null, cursor = null, fit = Math.min(n, mini ? MINI_N : HIST_N), off = n - fit;
    function setValue(i) {
      box.setAttribute("aria-valuenow", String(i + 1));
      box.setAttribute("aria-valuetext", "第 " + (i + 1) + " / " + n + " 个，" + pointText(i).replace(/ · /g, "，"));
    }
    // where the window ends: where it was left, else at the latest
    function windowEnd() {
      var at = chartWindow[o.key], end = n - 1;
      if (isNum(at)) { end = 0; pts.forEach(function (p, i) { if (p[0] <= at) end = i; }); }
      return end;
    }
    // with no column chosen, the slider stands on the window's last column, and keys start there
    function anchor() { return cursor !== null ? cursor : windowEnd(); }
    function place(W) {
      fit = Math.max(1, Math.min(mini ? MINI_N : HIST_N, n, Math.floor((W + COL_GAP) / COL_STEP)));
      off = Math.max(0, Math.min(n - fit, windowEnd() - fit + 1));
      if (cursor !== null) inView(cursor);
    }
    function inView(i) { if (i < off) off = i; else if (i >= off + fit) off = i - fit + 1; }
    function keepWindow() { if (off + fit >= n) delete chartWindow[o.key]; else chartWindow[o.key] = pts[off + fit - 1][0]; }
    function ends() {
      if (mini) return;
      var a = off, b = off + fit - 1;
      var e = [fmtWhen(pts[a][0]), fmtWhen(pts[b][0])];
      // a short run of columns: its two times as one span from the left
      var short = geo && geo.drawn < 150;
      if (short) e = [a === b ? e[0] : [e[0], " – ", e[1]], null];
      clear(endL); append(endL, e[0]);
      clear(endR); append(endR, e[1]);
      prev.hidden = next.hidden = fit >= n;
      prev.disabled = off <= 0;
      next.disabled = off + fit >= n;
      // the right-hand time sits under the last column, not at the card's edge
      var gapR = geo && !short ? Math.max(0, geo.W - geo.drawn) : 0;
      (next.hidden ? endR : next).style.marginRight = gapR + "px";
      (next.hidden ? next : endR).style.marginRight = "0px";
    }
    function draw(force) {
      var W = mini ? MINI_W : Math.round(plot.clientWidth);
      if (!W || (!force && geo && geo.W === W)) return;
      place(W);
      var drawn = fit * COL_STEP - COL_GAP;
      var x0 = mini ? W - drawn : 0;   // the small columns stand from the right, as on the F card
      var floor = height - 1, tall = floor - 1;
      var max = isNum(o.max) ? o.max : Math.max.apply(null, pts.map(function (p) { return p[1]; })) || 1;
      var shown = pts.slice(off, off + fit);
      var hs = Geom.columns(shown.map(function (p) { return p[1]; }), tall, max);
      var s = svg("svg", { width: W, height: height, viewBox: "0 0 " + W + " " + height, "aria-hidden": "true", focusable: "false" });
      s.appendChild(svg("rect", { x: mini ? 0 : x0, y: floor, width: mini ? W : drawn, height: 1, class: "tc-base" }));
      shown.forEach(function (p, k) {
        var i = off + k, x = x0 + k * COL_STEP;
        var col = hs[k] > 0
          ? svg("path", { d: colPath(x, floor, COL_W, hs[k]), class: i === n - 1 ? "tc-col tc-col-last" : "tc-col" })
          // a true zero: a hairline on the base, so it is not taken for a missing one
          : svg("rect", { x: x, y: floor - 1.5, width: COL_W, height: 1.5, rx: 0.75, class: "tc-zero" });
        col._i = i;
        s.appendChild(col);
      });
      // a reference over the columns it covers, at its value (the speed's average)
      if (o.ref && isNum(o.ref.value) && o.ref.from < off + fit) {
        var ry = Math.round(floor - (Math.min(o.ref.value, max) / max) * tall) + 0.5;
        s.appendChild(svg("path", { d: "M" + (x0 + Math.max(0, o.ref.from - off) * COL_STEP) + " " + ry + "H" + (x0 + drawn), class: "tc-avg" }));
      }
      geo = { W: W, x0: x0, drawn: drawn, svg: s };
      clear(plot);
      plot.appendChild(s);
      ends();
      if (axis) reserveLine(axis, probe, readings(false));
      if (cursor !== null) show(cursor, false);
    }
    // a small chart's reading takes the line under the figure for as long as a column is chosen
    var kept = null;
    function readOut(text) {
      var el = o.readout;
      if (!el) return;
      if (text === null) {
        if (kept) { clear(el); kept.forEach(function (c) { el.appendChild(c); }); kept = null; el.classList.remove("is-reading"); }
        return;
      }
      if (!kept) kept = Array.prototype.slice.call(el.childNodes);
      clear(el);
      el.appendChild(document.createTextNode(text));
      el.classList.add("is-reading");
    }
    function show(i, remember) {
      cursor = i;
      setValue(i);
      if (remember) chartCursor[o.key] = pts[i][0];
      if (!geo) return;
      var was = off;
      inView(i);
      if (off !== was) { keepWindow(); draw(true); return; }
      // the chosen column alone in full
      geo.svg.classList.add("tc-choosing");
      Array.prototype.forEach.call(geo.svg.querySelectorAll(".tc-col, .tc-zero"), function (r) { r.classList.toggle("tc-col-on", r._i === i); });
      if (mini) { readOut(pointText(i)); return; }
      reading.textContent = pointText(i);
      reading.removeAttribute("data-wrap");
      reading.hidden = false; endL.hidden = true; endR.hidden = true; prev.hidden = true; next.hidden = true;
      // the reading starts under its column while it fits, keeps to the chart's edges, and one
      // longer than the chart is wide wraps within it (Geom.readingPlace)
      var at = Geom.readingPlace(geo.W, reading.offsetWidth, geo.x0 + (i - off) * COL_STEP + COL_W / 2);
      if (at.wrap) reading.setAttribute("data-wrap", "true");
      reading.style.marginLeft = at.left.toFixed(0) + "px";
    }
    function hide() {
      cursor = null;
      setValue(anchor());
      if (mini) readOut(null);
      else { reading.hidden = true; endL.hidden = false; endR.hidden = false; }
      if (!geo) return;
      geo.svg.classList.remove("tc-choosing");
      Array.prototype.forEach.call(geo.svg.querySelectorAll(".tc-col-on"), function (r) { r.classList.remove("tc-col-on"); });
      ends();
    }
    // a window back or on; a column chosen before is let go, as the window no longer holds it
    function turn(dir) {
      cursor = null;
      delete chartCursor[o.key];
      off = Math.max(0, Math.min(n - fit, off + dir * fit));
      keepWindow();
      hide();
      draw(true);
    }
    function nearest(clientX) {
      var r = plot.getBoundingClientRect(), k = Math.floor((clientX - r.left - geo.x0 + COL_GAP / 2) / COL_STEP);
      return off + Math.max(0, Math.min(fit - 1, k));
    }
    plot.addEventListener("pointermove", function (ev) { if (geo) show(nearest(ev.clientX), document.activeElement === box); });
    plot.addEventListener("pointerleave", function () { if (document.activeElement !== box) hide(); });
    // leaving the chart forgets its column; a chart being replaced (no longer in the page) keeps it
    box.addEventListener("blur", function () { if (!box.isConnected) return; delete chartCursor[o.key]; hide(); });
    box.addEventListener("keydown", function (ev) {
      var i = anchor();
      if (ev.key === "ArrowLeft" || ev.key === "ArrowDown") i = Math.max(0, i - 1);
      else if (ev.key === "ArrowRight" || ev.key === "ArrowUp") i = Math.min(n - 1, i + 1);
      else if (ev.key === "PageDown") i = Math.max(0, i - fit);
      else if (ev.key === "PageUp") i = Math.min(n - 1, i + fit);
      else if (ev.key === "Home") i = 0;
      else if (ev.key === "End") i = n - 1;
      else if (ev.key === "Escape") { delete chartCursor[o.key]; hide(); return; }
      else return;
      ev.preventDefault();
      show(i, true);
    });
    // a column chosen before this chart replaced the last one: the same moment, or the nearest before it
    if (isNum(chartCursor[o.key])) {
      var want = chartCursor[o.key], at = 0;
      pts.forEach(function (p, i) { if (p[0] <= want) at = i; });
      cursor = at;
    }
    setValue(anchor());
    // the line a small chart reads out on keeps the height of its longest reading, as its width changes
    if (o.readSlot) o.readSlot.line._draw = function () { reserveLine(o.readSlot.line, o.readSlot.probe, readings(true)); };
    // drawn once laid out and again whenever the panel's width changes (attachCharts); the small
    // chart has its width and is drawn at once
    plot._draw = function () { draw(false); };
    if (mini) draw(true);
    return box;
  }

  // A bar out of a whole: parts [[fraction, tone]], each drawn from its true start to its true
  // end, a 2px gap cut at each boundary between two parts (Geom.barParts)
  function bar(parts, label, extra) {
    var fr = parts.map(function (p) { return isNum(p[0]) ? Math.max(0, p[0]) : 0; });
    var g = Geom.barParts(fr);
    var el = h("div", { class: "meter", role: "img", "aria-label": label, "data-parts": String(g.parts.length) });
    g.parts.forEach(function (p) {
      el.appendChild(h("span", { class: "seg", "data-tone": parts[p.i][1] || null,
        style: { left: (p.start * 100).toFixed(4) + "%", width: ((p.end - p.start) * 100).toFixed(4) + "%" } }));
    });
    g.gaps.forEach(function (q) {
      var off = q.align === "centre" ? "-1px" : q.align === "after" ? "0px" : "-2px";
      el.appendChild(h("span", { class: "seg-gap", "aria-hidden": "true", style: { left: "calc(" + (q.at * 100).toFixed(4) + "% + " + off + ")" } }));
    });
    if (!extra) return el;
    var wrap = h("div", { class: "meter-wrap" }, el);
    extra.forEach(function (x) { wrap.appendChild(x); });
    return wrap;
  }
  // under a graphic: what it shows on the left, what is left or when on the right
  function caption(left, right) {
    return h("div", { class: "caption" }, h("span", null, left), right ? h("span", null, right) : null);
  }
  function whenWithRel(ts) { return isNum(ts) ? [fmtWhen(ts), " · ", relSpan(ts)] : "—"; }
  function readAt(ts) { return isNum(ts) ? relSpan(ts) : "—"; }

  // The session's bill as the server priced it, request by request: each part's tokens and
  // cost summed over the buckets it keeps per model and tier. Priced when every bucket is.
  // Two gaps in the log are carried, never read as zero: requests whose cache counts were not
  // recorded (priced as uncached, an assumption) and requests missing their input or output
  // count (only what was recorded is in the sums, so they fall short).
  var PARTS = [["read", "缓存读取", null], ["write", "缓存写入", "soft"], ["uncached", "未缓存输入", "pale"], ["output", "输出", "output"]];
  function sessionCost(s) {
    var parts = { read: [0, 0], write: [0, 0], uncached: [0, 0], output: [0, 0] };
    var byLabel = {}, models = [], total = 0, responses = 0, priced = true, anyPriced = false, unpricedResponses = 0;
    var cacheMissing = 0, usageMissing = 0, longResponses = 0, tokens = 0;
    (s && s.usage || []).forEach(function (u) {
      if (!u.responses) return;
      responses += u.responses;
      var input = Math.max(0, u.input || 0), cached = Math.min(Math.max(0, u.cached || 0), input);
      var write = Math.min(Math.max(0, u.write || 0), input - cached);
      var t = { read: cached, write: write, uncached: input - cached - write, output: Math.max(0, u.output || 0) };
      var c = u.priced && u.cost ? u.cost : null;
      var cost = 0;
      PARTS.forEach(function (p) {
        parts[p[0]][0] += t[p[0]];
        tokens += t[p[0]];
        if (c) { parts[p[0]][1] += c[p[0]] || 0; cost += c[p[0]] || 0; }
      });
      if (c) { total += cost; anyPriced = true; } else { priced = false; unpricedResponses += u.responses; }
      cacheMissing += u.cacheMissing || 0;
      usageMissing += u.usageMissing || 0;
      longResponses += u.longResponses || 0;
      var label = (u.model || "未知模型") + (isFast(u.serviceTier) ? " · Fast" : "");
      if (!byLabel[label]) { byLabel[label] = { label: label, responses: 0, cost: 0, priced: true }; models.push(byLabel[label]); }
      byLabel[label].responses += u.responses;
      byLabel[label].cost += cost;
      if (!c) byLabel[label].priced = false;
    });
    return { parts: parts, models: models, total: total, responses: responses, tokens: tokens,
      priced: priced && models.length > 0, partial: !priced && anyPriced, unpricedResponses: unpricedResponses,
      cacheMissing: cacheMissing, usageMissing: usageMissing, longResponses: longResponses };
  }
  // a thread's bill as a row shows it: the figure, and whether all of the thread is in it
  function costText(cost) {
    if (!cost || !(cost.priced || cost.partial)) return "—";
    return fmtUsd(cost.total) + (cost.priced && !cost.usageMissing ? "" : " 部分");
  }

  // Two scenarios' figures as one reading: the same figure where the rates agree, else the
  // range from the one to the other (tokens not read from the cache billed as input, or at the
  // cache-write rate if they are written; the log does not say beforehand which)
  function usdRange(lo, hi) {
    if (!isNum(lo)) return "—";
    var a = fmtUsd(lo), b = isNum(hi) ? fmtUsd(hi) : a;
    if (a === b) return a;
    if (a.charAt(0) === "<") return "≤" + b;
    return [a, "–", h("wbr"), b.replace(/^\$/, "")];
  }
  function inputScenarioTip(lead, readTokens, newTokens, rt) {
    var plain = readTokens ? times(readTokens, rt.read) + " + " + times(newTokens, rt.input) : times(newTokens, rt.input);
    var written = readTokens ? times(readTokens, rt.read) + " + " + times(newTokens, rt.write) : times(newTokens, rt.write);
    return lead + "\n未读到的部分按未缓存输入价：" + plain + " / 百万 tokens" +
      (rt.write !== rt.input ? "\n若这部分同时写入缓存，按写入价：" + written + " / 百万 tokens" : "");
  }
  // a token count as a reading: the figure, and its k or M set small as its unit
  function tokCell(v, k, extra) {
    var t = fmtK(v), m = /^(.*?)([kM])$/.exec(t);
    return Object.assign({ v: m ? m[1] : t, u: m ? m[2] : isNum(v) ? "tokens" : null, k: k }, extra || {});
  }
  // a reply's duration as a reading: seconds under a minute, else minutes and seconds
  function secsCell(ms, k, tip) {
    if (!isNum(ms)) return { v: "—", k: k, tone: "muted" };
    return ms < 60000 ? { v: (ms / 1000).toFixed(1), u: "秒", k: k, tip: tip } : { v: fmtSecs(ms), k: k, tip: tip };
  }
  function timeTick(prog) {
    return h("span", { class: "time-tick", "aria-hidden": "true", data: { tickStart: prog.start, tickSpan: prog.span },
      style: { left: (prog.f * 100).toFixed(3) + "%" } });
  }

  var DETAIL = {
    context: function (s) {
      var c = s.context || {};
      var d = { title: "上下文", dot: "accent",
        key: [c.state, c.usedTokens, c.windowTokens, c.compactedAt, c.at, c.history && c.history.length] };
      d.src = isNum(c.at) ? ["最近一次响应 · ", relSpan(c.at)] : "最近一次响应";
      if (isNum(c.at)) d.srcTip = "响应于 " + fmtStamp(c.at);
      if (c.state === "ok") {
        var r = c.ratio, shown = pctNum(r), tone = usedTone(Number(shown)), left = Math.max(0, c.windowTokens - c.usedTokens);
        d.value = shown; d.unit = "%"; d.note = "已用 · 窗口 " + fmtK(c.windowTokens);
        if (tone) d.dot = tone;
        d.viz = bar([[r, tone]], "上下文已用 " + shown + "%");
        d.leaders = { frac: r, cells: [tokCell(c.usedTokens, "已用", { sw: tone, tip: fmtInt(c.usedTokens) + " tokens" }),
          tokCell(left, "剩余", { sw: "free", tip: fmtInt(left) + " tokens" })] };
        var hist = (c.history || []).filter(function (p) { return p.window > 0; });
        // what the last response added: the step from the one before, unless a compaction lies between
        var a = hist[hist.length - 2], b = hist[hist.length - 1];
        var grew = a && b && (!isNum(c.compactedAt) || a.t > c.compactedAt) && b.used >= a.used ? b.used - a.used : null;
        d.parts = [grew !== null ? tokCell(grew, "上次增加", { v: "+" + tokCell(grew, "").v, tip: "前一次响应 " + fmtInt(a.used) + " → 这次 " + fmtInt(b.used) + " tokens" }) : null,
          isNum(c.compactedAt) ? { k: "上次压缩", v: fmtWhen(c.compactedAt), tip: "压缩于 " + fmtStamp(c.compactedAt) } : null];
        d.history = { key: "context", title: "占用历史", count: hist.length + " 次响应",
          chart: columnsChart(hist.map(function (p) { return [p.t, (p.used / p.window) * 100]; }),
            { key: "context", max: 100, label: "上下文占用", fmt: function (v) { return "已用 " + pctOf(v) + "%"; } }) };
        d.fn = { text: "按日志记录的窗口 " + fmtK(c.windowTokens) + " 计 · 最近一次响应的占用，不是累计用量" };
      } else if (c.state === "compacted_pending") {
        d.value = "—"; d.tone = "muted"; d.note = "刚压缩";
        d.parts = [tokCell(c.windowTokens, "窗口"), { v: readAt(c.compactedAt), k: "压缩于", tip: fmtStamp(c.compactedAt) }];
        d.notes = [{ text: "下一次响应后更新。" }];
      } else if (c.state === "no_window") {
        d.value = fmtK(c.usedTokens); d.unit = "tokens"; d.note = "已用";
        d.notes = [{ text: "日志未记录窗口大小，无法算出占比。" }];
      } else {
        d.value = "—"; d.tone = "muted";
        d.notes = [{ text: "还没有完成的响应。" }];
      }
      return d;
    },

    quota: function (s, snap) {
      var rl = snap.rateLimits;
      var d = { title: "额度", src: "账号共享", dot: "accent", key: [rl && [rl.at, rl.windows, rl.history && rl.history.length, rl.historyJoined, rl.fromSelectedSession]] };
      var w = quotaWindow(rl);
      if (!w) {
        d.value = "—"; d.tone = "muted";
        d.notes = [{ text: "还没有额度记录。" }];
        return d;
      }
      var used = isNum(w.usedPercent) ? Math.min(100, Math.max(0, w.usedPercent)) : null;
      var other = !rl.fromSelectedSession && rl.sourceSessionShortId;
      d.title = windowShort(w.windowMinutes) + "额度";
      // where the reading comes from and how old it is; when exactly, on focus
      d.src = [other ? "账号共享 · 来自会话 …" + rl.sourceSessionShortId : "账号共享", " · ", relSpan(rl.at)];
      d.srcTip = "读取于 " + fmtStamp(rl.at) + (other ? "\n来自同账号的会话 …" + rl.sourceSessionShortId : "\n来自本会话的日志");
      d.value = isNum(used) ? pctOf(used) : "—"; d.unit = isNum(used) ? "%" : ""; d.note = isNum(used) ? "已用" : "暂无读数";
      if (w.expired || !isNum(used)) d.tone = "muted";
      var qtone = w.expired ? null : quotaTone(isNum(used) ? Number(pctOf(used)) : null);
      if (qtone === "warn" || qtone === "crit") d.dot = qtone;
      // the window's elapsed time, a reference upright on the bar, only where its length and
      // reset are both known and now lies inside it
      var prog = windowProgress(w);
      d.viz = bar([[isNum(used) ? used / 100 : 0, w.expired ? "muted" : qtone]],
        isNum(used) ? "已用 " + pctOf(used) + "%" : "暂无数据", prog ? [timeTick(prog)] : null);
      d.caption = [isNum(w.resetsAt) && !w.expired ? [fmtWhen(w.resetsAt) + " 重置 · ", untilSpan(w.resetsAt), "后"] : w.expired ? "已过重置时间" : "重置时间未知",
        isNum(used) ? ["剩余 ", h("b", { text: pctOf(100 - used) + "%" })] : null];
      // the account's other windows: the same bar, smaller, on the same scale
      d.rows = rl.windows.filter(function (x) { return x !== w; }).map(function (x) {
        var u = isNum(x.usedPercent) ? Math.min(100, Math.max(0, x.usedPercent)) : null;
        var xp = windowProgress(x);
        return { k: windowShort(x.windowMinutes), frac: isNum(u) ? u / 100 : 0,
          tone: x.expired ? "muted" : quotaTone(isNum(u) ? Number(pctOf(u)) : null), tick: xp ? timeTick(xp) : null,
          v: isNum(u) ? [pctOf(u), h("span", { class: "ph-u", text: "%" })] : "—", vtone: x.expired || !isNum(u) ? "muted" : null,
          sub: [isNum(x.resetsAt) && !x.expired ? [fmtWhen(x.resetsAt) + " 重置 · ", untilSpan(x.resetsAt), "后"] : x.expired ? "已过重置时间" : "重置时间未知",
            isNum(u) ? "剩余 " + pctOf(100 - u) + "%" : null],
          tip: (isNum(u) ? "已用 " + pctOf(u) + "%" : "暂无读数") + (isNum(x.resetsAt) ? "\n" + fmtStamp(x.resetsAt) + " 重置" : "") };
      });
      var key = String(w.windowMinutes);
      var qh = (rl.history || []).filter(function (p) { return p.w && isNum(p.w[key]); });
      d.history = { key: "quota", title: "已用历史", count: qh.length + " 次读数",
        chart: columnsChart(qh.map(function (p) { return [p.t, Math.min(100, Math.max(0, p.w[key]))]; }),
          { key: "quota", max: 100, label: "额度已用",
            fmt: function (v) { return "已用 " + pctOf(v) + "%"; },
            pointText: function (i) { var p = qh[i]; return fmtWhen(p.t) + " · 已用 " + pctOf(p.w[key]) + "%" + (p.from ? " · 来自会话 …" + p.from : ""); } }) };
      // the record is this session's own unless the account's newer reading could join it
      if (other && !rl.historyJoined) d.history.note = "本会话记录 · 当前读数来自会话 …" + rl.sourceSessionShortId;
      // the bar's upright, keyed once under the card in its own shape
      if (prog) d.fn = { text: [h("span", { class: "tick-sw", "aria-hidden": "true" }), "时间进度 · 已过 ",
        h("span", { data: { progStart: prog.start, progSpan: prog.span }, text: pctOf(prog.f * 100) + "%" })],
        tip: "这个 " + windowShort(w.windowMinutes) + "窗口已经过去的时间，按重置时刻推算。\n只作时间参照，不是用量目标，也不是服务承诺。" };
      if (w.expired) d.notes = [{ text: "已过重置时间，等下一次响应更新。", tone: "warn" }];
      return d;
    },

    cache: function (s, snap) {
      var c = s.cache || {};
      var l = c.latest, r = c.recent;
      var o = cacheOutlook(s);
      var d = { title: "提示缓存", src: "最近一次请求", dot: o && o.phase === "switched" ? "warn" : "accent",
        key: [l, r, c.history && c.history.length, c.estimate, o && o.phase, s.model, s.serviceTier] };
      if (!l) {
        d.value = "—"; d.tone = "muted"; d.notes = [{ text: "还没有完成的响应。" }];
        return d;
      }
      if (l.state === "missing_cached" || !isNum(l.ratio)) {
        d.value = "—"; d.tone = "muted"; d.note = "命中率未记录";
        d.parts = [tokCell(l.inputTokens, "输入"), { v: "未记录", k: "缓存读取", tone: "muted" }];
        d.notes = [{ text: "这次响应没有记录缓存读取数，命中率未知。" }];
        return d;
      }
      var write = Math.min(Math.max(0, l.cacheWriteInputTokens || 0), l.inputTokens - l.cachedInputTokens);
      var miss = Math.max(0, l.inputTokens - l.cachedInputTokens - write);
      var total = Math.max(1, l.inputTokens);
      d.value = pctNum(l.ratio); d.unit = "%"; d.note = "命中 · 输入 " + fmtK(l.inputTokens);
      // read, written, sent uncached: the parts of the prompt, named as the session card names them
      d.viz = bar([[l.cachedInputTokens / total, null], [write / total, "soft"], [miss / total, "pale"]], "命中 " + pctNum(l.ratio) + "%");
      if (r && isNum(r.ratio)) d.caption = [r.responses === r.considered ? "近 " + r.responses + " 次 " + pctNum(r.ratio) + "%"
        : "近 " + r.considered + " 次中 " + r.responses + " 次有记录 · " + pctNum(r.ratio) + "%", null];
      d.legend = [{ sw: null, v: fmtK(l.cachedInputTokens), k: "缓存读取", tip: fmtInt(l.cachedInputTokens) + " tokens" },
        write > 0 ? { sw: "soft", v: fmtK(write), k: "缓存写入", tip: fmtInt(write) + " tokens" } : null,
        { sw: "pale", v: fmtK(miss), k: "未缓存输入", tip: fmtInt(miss) + " tokens" }];
      // what the cache saved on this request at the built-in API prices
      var sv = c.estimate && c.estimate.saved;
      if (sv && isNum(sv.paid) && isNum(sv.full) && sv.full > sv.paid) d.parts = [{ v: fmtUsd(sv.full - sv.paid), k: "本次节省",
        tip: "按 API 价：这次输入约 " + fmtUsd(sv.paid) + "；若全部按未缓存输入计约 " + fmtUsd(sv.full) + "（省 " + Math.round((1 - sv.paid / sv.full) * 100) + "%）" }];
      // the model's rule, as a reference beside the measured rate: the time it leaves, drawn
      // as a bar that runs down, its ends in words
      if (o && (o.phase === "within" || o.phase === "beyond")) {
        var within = o.phase === "within";
        d.rows = [{ k: "保留参考", frac: within ? Math.max(0, o.left) / o.ttl : 0, fill: within ? { until: o.until, span: o.ttl } : null,
          v: within ? liveSpan("lefts", o.until) : "状态未确认", vtone: within ? null : "muted",
          sub: within ? ["规则推算", "最短至 " + fmtWhen(o.until)] : ["已超过最短保留时长", "最短至 " + fmtWhen(o.until)],
          tip: "按 OpenAI 对 GPT-5.6 及以后模型的说明推算：缓存在最近一次写入或复用后至少保留 30 分钟。\n日志不记录这次请求的缓存实际保留了多久。" +
            (isNum(o.sent) ? "\n请求于 " + fmtStamp(o.sent) : "") }];
        d.rowsKind = "minor";
      }
      // the next request's input at the built-in API prices, if it reads the cache or if it does
      // not; its tokens taken as the last request's input plus output, said in the group's head
      var e = o && o.est;
      if (e) {
        var rt = e.rate, cells = [];
        var basis = "下次输入按上次输入 " + fmtInt(e.readTokens) + " + 输出 " + fmtInt(e.newTokens) + " = " + fmtInt(e.prompt) +
          " tokens 计；之后新加的消息、工具结果和压缩都不在其中。\n";
        if (e.switched) {
          cells.push({ v: usdRange(e.cold, e.coldWritten), k: "新模型若未命中", tone: "warn",
            tip: inputScenarioTip(basis + "换到 " + e.model + " 后，若下次请求读不到缓存：", 0, e.prompt, rt) });
          if (isNum(e.stayWarm)) cells.push({ v: usdRange(e.stayWarm, e.stayWarmWritten), k: "原模型若命中",
            tip: basis + "缓存是为 " + o.ranOn + " 写入的；留在它上面且缓存仍在时，按 " + o.ranOn + " 的价格估算" });
        } else {
          cells.push({ v: usdRange(e.warm, e.warmWritten), k: "下次若命中",
            tip: inputScenarioTip(basis + "假设上次输入的 " + fmtK(e.readTokens) + " tokens 全部从缓存读取：", e.readTokens, e.newTokens, rt) });
          cells.push({ v: usdRange(e.cold, e.coldWritten), k: "下次若未命中",
            tip: inputScenarioTip(basis + "假设下次请求全部读不到缓存：", 0, e.prompt, rt) });
        }
        // after a switch the two cells are priced on two models, each named in its cell: the
        // head says which two, from the old to the new
        d.stats = { head: ["下次输入 · 按上次输入 + 输出量", "API 价 · " + (e.switched ? o.ranOn + " → " : "") + e.model + " · " +
          tierName(e.tier) + (e.long ? " · 长上下文" : "")], cells: cells };
      }
      var ch = c.history || [];
      d.history = { key: "cache", title: "命中率历史", count: ch.length + " 次请求",
        chart: columnsChart(ch.map(function (p) { return [p[0], p[1] * 100]; }),
          { key: "cache", max: 100, label: "缓存命中率", fmt: function (v) { return "命中 " + pctOf(v) + "%"; } }) };
      if (o && o.phase === "norule") d.fn = { text: "这个模型没有公开的最短保留时长", tip: isNum(o.sent) ? "请求于 " + fmtStamp(o.sent) : null };
      if (o && o.phase === "switched") {
        d.notes = [{ tone: "warn", icon: "i-swap", text: "已从 " + o.ranOn + " 切换到 " + o.next + "。缓存按模型分开，下次请求预计读不到原模型的缓存。" }];
      }
      return d;
    },

    speed: function (s) {
      var t = s.throughput || {};
      var agg = t.aggregate, latest = t.latest;
      // the replies drawn: the latest timed ones on this model and effort, the averaged among
      // them, oldest first (the server draws every averaged reply)
      var series = (t.series || []).filter(function (x) { return isNum(x.tokensPerSecond); });
      var d = { title: "输出速度", dot: "output", key: [agg, latest && [latest.at, latest.valid], t.current, t.model, t.effort, series.map(function (x) { return x.at; })] };
      if (!agg || !isNum(agg.tokensPerSecond)) {
        d.value = "—"; d.tone = "muted"; d.unit = "tok/s";
        d.notes = [{ text: !t.model || !t.effort ? "等待记录模型与推理强度。" : "还没有可计时的响应。" }];
        return d;
      }
      d.src = (t.current === false ? "切换前 · " : "") + [t.model, t.effort].filter(Boolean).join(" · ");
      if (t.current === false) d.dot = null;
      d.value = fmtRate(agg.tokensPerSecond); d.unit = "tok/s";
      // under the figure, what it is; while a reply's column is chosen, that reply
      var subText = h("span", { class: "lead-text" }, "近 " + agg.samples + " 次平均"), subProbe = readingProbe();
      d.sub = h("p", { class: "lead-sub" }, subText, subProbe);
      var from = 0;
      while (from < series.length && !series[from].inAggregate) from++;
      // beside it the replies as small columns, the dotted line the average over the replies it is of
      d.side = !series.length ? null : columnsChart(series.map(function (g) { return [g.at, g.tokensPerSecond]; }), {
        key: "speed", mini: true, tone: "output", readout: subText, readSlot: { line: d.sub, probe: subProbe },
        label: "输出速度（最近 " + series.length + " 次回复）", fmt: function (v) { return fmtRate(v) + " tok/s"; },
        ref: { value: agg.tokensPerSecond, from: from },
        pointText: function (i) { var g = series[i]; return fmtWhen(g.at) + " · " + fmtRate(g.tokensPerSecond) + " tok/s · " + fmtInt(g.outputTokens) + " tokens / " + fmtSecs(g.durationMs) + (g.inAggregate ? " · 计入平均" : ""); }
      });
      // the newest timed reply, as phrases
      var g = series[series.length - 1];
      var lastUntimed = latest && !latest.valid;
      if (g) d.parts = [
        { v: fmtRate(g.tokensPerSecond), u: "tok/s", k: lastUntimed ? "上次计时" : "最近一次", tip: fmtWhen(g.at) + " 的回复" },
        secsCell(g.durationMs, "用时"),
        tokCell(g.outputTokens, "输出", { tip: fmtInt(g.outputTokens) + " tokens" }),
        { v: String(series.length), u: "次", k: "图中回复", tip: "同一模型与推理强度最近 " + series.length + " 次有计时的回复；点线是其中近 " + agg.samples + " 次的平均" }];
      d.fn = { text: "日志估算：输出 tokens ÷ 记录时长，含推理", tip: "平均按总量算：近 " + agg.samples + " 次共 " + fmtK(agg.outputTokens) + " tokens ÷ " + fmtSecs(agg.durationMs) };
      if (lastUntimed) d.notes = [{ text: "最近一次回复不计时：" + (INVALID[latest.reason] || "没有可用的计时") + "。" }];
      return d;
    },

    session: function (s, snap) {
      var b = snap.binding || {};
      var main = session();
      // a subagent viewed in the main agent's place is named as the thread picker names it
      var child = state.threadId && main ? (main.children || []).filter(function (c) { return c.id === s.id; })[0] : null;
      var sc = sessionCost(s);
      var turn = turnText(s);
      var d = { title: child ? threadName(child) : "本会话", dot: "output",
        src: [child ? taskName(child) : null, turn, sc.responses ? sc.responses + " 次请求" : null].filter(Boolean).join(" · "),
        key: [s.id, s.model, s.effort, s.serviceTier, s.startedAt, s.usage, s.timeline && s.timeline.length, turn, b.state, s.idMismatch,
          state.threadId, main && main.children && main.children.length, snap.pricing] };
      d.notes = [];
      var shown = PARTS.filter(function (p) { return sc.parts[p[0]][0] > 0; });
      if (sc.priced) {
        d.value = fmtUsd(sc.total); d.note = sc.usageMissing ? "API 价估算 · 不完整" : "API 价估算";
        d.viz = bar(PARTS.map(function (p) { return [sc.total > 0 ? sc.parts[p[0]][1] / sc.total : 0, p[2]]; }), "本会话费用 " + fmtUsd(sc.total));
        d.legend = shown.map(function (p) { return { sw: p[2], v: fmtUsd(sc.parts[p[0]][1]), k: p[1], tip: fmtInt(sc.parts[p[0]][0]) + " tokens" }; });
        // each request at the cost the total was summed from, a column a request; a column's
        // reading carries the running total, so the last one reads the total above. The log
        // keeps the latest requests: what came before them is the running total's base, said.
        var tl = s.timeline || [], sum = 0, steps = [];
        tl.forEach(function (r) { if (isNum(r[1])) { sum += r[1]; steps.push([r[0], r[1], sum, r[2]]); } });
        var base = Math.max(0, sc.total - sum), cut = sc.responses > steps.length;
        d.history = { key: "session", title: "每次请求费用", count: (cut ? "最近 " : "") + steps.length + " 次请求",
          note: cut && base > 0 ? "更早的 " + (sc.responses - steps.length) + " 次请求合计 " + fmtUsd(base) + "，计入累计" : null,
          chart: columnsChart(steps.map(function (p) { return [p[0], p[1]]; }), { key: "session", tone: "ink", label: "每次请求费用",
            fmt: fmtUsd, pointText: function (i) {
              var f = steps[i][3] || 0;
              return fmtWhen(steps[i][0]) + " · 本次 " + fmtUsd(steps[i][1]) + " · 累计 " + fmtUsd(base + steps[i][2]) + (f & 1 ? " · 长上下文请求" : "") +
                (f & 2 ? " · 缓存未记录，按未缓存估算" : "") + (f & 4 ? " · 用量不全，只计已记录部分" : "");
            } }) };
      } else if (!sc.responses) {
        d.value = "—"; d.tone = "muted";
        d.notes.push({ text: "还没有完成的请求。" });
      } else {
        d.value = sc.tokens ? fmtK(sc.tokens) : "—"; d.unit = sc.tokens ? "tokens" : ""; if (!sc.tokens) d.tone = "muted";
        d.legend = shown.map(function (p) { return { sw: p[2], v: fmtK(sc.parts[p[0]][0]), k: p[1], tip: fmtInt(sc.parts[p[0]][0]) + " tokens" }; });
        d.viz = sc.tokens ? bar(PARTS.map(function (p) { return [sc.parts[p[0]][0] / sc.tokens, p[2]]; }), "本会话 " + fmtK(sc.tokens) + " tokens") : null;
        d.notes.push({ text: sc.partial ? "部分模型不在内置价表中，不显示合计费用。" : "模型不在内置价表中，只显示 tokens。" });
      }
      // gaps in what the log recorded: said where the figure is, not rounded into it
      if (sc.usageMissing) d.notes.push({ text: sc.usageMissing + " 次请求缺少输入或输出用量，" + (sc.priced ? "金额" : "合计") + "只含已记录的部分。" });
      if (sc.cacheMissing) d.notes.push({ text: sc.cacheMissing + " 次请求未记录缓存用量，" + (sc.priced ? "按全部未缓存估算。" : "计入未缓存输入。") });
      var cells = [];
      if (isNum(s.startedAt)) cells.push({ v: sinceSpan(s.startedAt), k: "已运行", tip: "开始于 " + fmtStamp(s.startedAt) });
      if (sc.partial) cells.push({ v: fmtUsd(sc.total), k: "已计价部分", tip: "未收录单价的 " + sc.unpricedResponses + " 次请求不在其中" });
      if (sc.longResponses && (sc.priced || sc.partial)) cells.push({ v: String(sc.longResponses), u: "次", k: "长上下文",
        tip: "这些请求的输入超过所用模型的长上下文门槛，整次按长上下文价计。" });
      // the main agent's automatic reviews: threads of their own, summed rather than listed
      if (!child && main) {
        var rv = threadsOf(main).review;
        if (rv.n) cells.push({ v: rv.priced ? costText(reviewCost(rv)) : "未计价", tone: rv.priced ? null : "muted", k: "自动审查",
          tip: rv.responses + " 次请求 · " + rv.n + " 个线程" });
      }
      if (cells.length) d.parts = cells;
      // the model it ran on and the prices used, one line; the breakdown and the terms on focus
      var asOf = snap.pricing && snap.pricing.asOf;
      var priceTerms = "按内置的 OpenAI API 价目表逐次估算，不是账单；订阅不按此计费。\n每次请求按它自己的模型、档位与输入量取价。";
      var modelText;
      var tip = null;
      if (sc.models.length > 1) {
        var byCost = sc.models.slice().sort(function (a, b2) { return b2.cost - a.cost; });
        modelText = byCost[0].label + " 等 " + sc.models.length + " 个模型";
        tip = byCost.map(function (m) { return m.label + "  " + (m.priced ? fmtUsd(m.cost) : "未收录单价") + " · " + m.responses + " 次"; }).join("\n");
      } else modelText = [s.model, s.effort, s.serviceTier ? tierName(s.serviceTier) : null].filter(Boolean).join(" · ") || "模型未记录";
      if (sc.priced || sc.partial) tip = (tip ? tip + "\n" : "") + priceTerms;
      d.fn = { text: modelText + (sc.priced || sc.partial ? " · 内置 API 价" + (asOf ? " " + asOf : "") : ""), tip: tip };
      if (s.idMismatch) d.notes.push({ text: "日志内的会话 ID 与文件名不一致，不作为当前会话。", tone: "warn" });
      return d;
    }
  };

  // the share of a quota window already past, where its length and reset are known and now lies inside it
  function windowProgress(w) {
    if (!w || w.expired || !isNum(w.windowMinutes) || !isNum(w.resetsAt)) return null;
    var span = w.windowMinutes * 60000, start = w.resetsAt - span, f = (now() - start) / span;
    if (!(f >= 0 && f <= 1)) return null;
    return { start: start, span: span, f: f };
  }

  // [symbol, name, count] for each kind of change present
  function gitCounts(g) {
    var out = [];
    if (g.conflicted) out.push(["=", "冲突", g.conflicted, "conf"]);
    if (g.staged) out.push(["+", "已暂存", g.staged, "add"]);
    if (g.unstaged) out.push(["!", "已修改", g.unstaged, "mod"]);
    if (g.untracked) out.push(["?", "未跟踪", g.untracked, "new"]);
    return out;
  }
  // a long branch name kept to its last part, which is the one that tells branches apart
  function shortBranch(name) {
    if (!name || name.length <= 24) return name;
    var tail = name.slice(name.lastIndexOf("/") + 1);
    return "…/" + (tail.length > 22 ? "…" + tail.slice(-21) : tail);
  }
  function gitSummary(g) {
    var bits = ["Git", g.detached ? "分离 HEAD" : g.branch || "未知分支"];
    if (g.ahead) bits.push("领先 " + g.ahead);
    if (g.behind) bits.push("落后 " + g.behind);
    var cs = gitCounts(g);
    bits.push(cs.length ? cs.map(function (c) { return c[1] + " " + c[2]; }).join("，") : "无改动");
    return bits.join("，");
  }
  function gitText(g) {
    if (!g || !g.state) return "—";
    var M = { not_repo: "不是 git 仓库", no_cwd: "目录不可访问", disabled: "已关闭", pending: "读取中", timeout: "读取超时", unavailable: "未安装 git", dubious_ownership: "仓库未受信任" };
    return M[g.state] || "不可用";
  }
  // a span for the rows: "3 小时 5 分", "2 天", "不到 1 分"; parts that are zero are left out
  function fmtSpan(ms) {
    if (!isNum(ms)) return "—";
    var m = Math.floor(Math.max(0, ms) / 60000);
    if (m < 1) return "不到 1 分";
    var dd = Math.floor(m / 1440), hh = Math.floor((m % 1440) / 60), mm = m % 60;
    if (dd > 0) return hh > 0 ? dd + " 天 " + hh + " 小时" : dd + " 天";
    if (hh > 0) return mm > 0 ? hh + " 小时 " + mm + " 分" : hh + " 小时";
    return mm + " 分";
  }
  function sinceSpan(ts) { return h("span", { data: { since: ts }, text: fmtSpan(now() - ts) }); }

  function copyText(text, btn) {
    function done(ok) {
      toast(ok ? "已复制会话 ID" : "宿主不允许复制，请手动选择文本");
      if (ok && btn) { btn.setAttribute("data-done", "true"); setTimeout(function () { btn.removeAttribute("data-done"); }, 1400); }
    }
    function fallback() {
      try {
        var ta = h("textarea", { readonly: true, style: { position: "fixed", opacity: "0", left: "-999px" } });
        ta.value = text; document.body.appendChild(ta); ta.select();
        var ok = document.execCommand("copy"); ta.remove(); done(ok);
      } catch (e) { done(false); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
    else fallback();
  }

  // ---- footer ----------------------------------------------------------
  function renderFoot() {
    var snap = state.snapshot;
    var s0 = session();
    var b = snap && snap.binding ? snap.binding.state : null;
    var left = state.switching ? "切换中…" : b === "host" ? "当前会话" : b === "manual" ? "手动选择" : b ? "未绑定会话" : "";
    var fb = $("foot-binding");
    fb.textContent = left;
    // following the chat you are in needs no word; a pinned or unbound session does
    fb.classList.toggle("sr-only", b === "host" && !state.switching);
    var idText = $("menu-id-text"), sid = s0 && s0.id;
    if (idText.textContent !== (sid || "")) idText.textContent = sid || "";
    $("menu-id").hidden = !sid;
    // git, read-only: the branch, ↑ ahead ↓ behind, then +staged !modified ?untracked =conflicted
    // in their colours. It is a button: the menu holds the full account, which a narrow footer
    // shortens to the branch alone.
    var s = session(), g = s && s.git;
    var gitKey = g && g.state === "ok" ? JSON.stringify([g.branch, g.detached, g.upstream, g.ahead, g.behind, gitCounts(g)]) : "";
    keyed("foot-git", gitKey, function () {
      var el = $("foot-git");
      clear(el);
      el.hidden = !gitKey;
      if (!gitKey) { el.removeAttribute("aria-label"); return; }
      var sync = [g.ahead ? "↑" + g.ahead : "", g.behind ? "↓" + g.behind : ""].filter(Boolean).join(" ");
      var counts = gitCounts(g);
      append(el, [icon("i-branch"), h("span", { class: "br", text: g.detached ? "分离 HEAD" : shortBranch(g.branch) || "—" }),
        sync ? h("span", { class: "ab", text: sync }) : null,
        counts.length ? h("span", { class: "ct" }, counts.map(function (c) { return h("span", { class: "gc", "data-k": c[3], text: c[0] + c[2] }); })) : null]);
      el.setAttribute("aria-label", gitSummary(g) + "。打开详情");
      el.title = gitSummary(g);
    });
  }

  // ---- menu ------------------------------------------------------------
  function renderMenu() {
    var m = $("menu-dyn");
    var snap = state.snapshot;
    var sv = session(), g = sv && sv.git;
    var key = [state.themePref, state.allKinds, JSON.stringify(snap && snap.roots), state.transport && state.transport.kind, state.diagOpen, JSON.stringify(g || null)].join("|");
    keyed("menu", key, function () {
      var focusedName = document.activeElement && m.contains(document.activeElement) ? (document.activeElement.name || document.activeElement.id) : null;
      clear(m);
      // git in full: what the footer shows, and what a narrow footer leaves out
      if (g && g.state) {
        // the branch in mono with how far it is ahead or behind beside it, what it tracks, then
        // its counts as the F card's capsules: each in its own colour on a ground of it, a zero
        // an outline only, so the three always stand where they were; conflicts first, if any
        var rows = h("div", { class: "git-rows" });
        if (g.state === "ok") {
          rows.appendChild(h("div", { class: "git-line" }, h("span", { class: "git-br", text: g.detached ? "分离 HEAD" : g.branch || "未知分支" }),
            g.upstream ? h("span", { class: "git-ab", text: "领先 " + (g.ahead || 0) + " · 落后 " + (g.behind || 0) }) : null));
          rows.appendChild(h("div", { class: "git-up", text: g.upstream ? "跟踪 " + g.upstream : "没有上游" }));
          var counts = (g.conflicted ? [["=", "冲突", g.conflicted, "conf"]] : []).concat([["+", "已暂存", g.staged || 0, "add"],
            ["!", "已修改", g.unstaged || 0, "mod"], ["?", "未跟踪", g.untracked || 0, "new"]]);
          var pills = h("div", { class: "pills", role: "list" });
          if (!counts.some(function (c) { return c[2]; })) pills.appendChild(h("span", { class: "pill-none", role: "listitem", text: "工作区干净" }));
          else counts.forEach(function (c) {
            pills.appendChild(h("span", { class: "pill", role: "listitem", "data-k": c[3], "data-zero": c[2] ? null : "true", "aria-label": c[1] + " " + c[2] },
              h("b", { text: c[0] + c[2] }), h("span", { text: c[1] })));
          });
          rows.appendChild(pills);
        } else rows.appendChild(h("div", { text: gitText(g) }));
        m.appendChild(h("div", { class: "menu-group", id: "menu-git", tabindex: "-1", role: "group", "aria-label": "Git" },
          h("div", { class: "menu-label", text: "Git" }), rows));
      }
      var seg = h("div", { class: "segctl", role: "radiogroup", "aria-label": "外观" });
      [["auto", "自动"], ["light", "浅色"], ["dark", "深色"]].forEach(function (p) {
        seg.appendChild(h("label", null,
          h("input", { type: "radio", name: "theme", value: p[0], checked: state.themePref === p[0] ? "checked" : null, on: { change: function () { setThemePref(p[0]); } } }),
          h("span", { text: p[1] })));
      });
      m.appendChild(h("div", { class: "menu-group" }, h("div", { class: "menu-label", text: "外观" }), seg));
      m.appendChild(h("div", { class: "menu-group" },
        h("label", { class: "check" }, h("input", { type: "checkbox", id: "menu-allkinds", checked: state.allKinds ? "checked" : null, on: { change: function (e) { setAllKinds(e.target.checked); } } }), "列表包含子代理与自动审查")));
      var diag = h("div", { class: "diag" });
      ((snap && snap.roots) || []).forEach(function (r) {
        diag.appendChild(h("div", null, h("div", { text: r.label + " · " + (r.exists ? r.sessions + " 个会话" : "目录不存在") }), h("div", { class: "path", text: r.path })));
      });
      var hostTxt = state.hostSessionId ? "已识别" : state.hostAmbiguous ? "信息矛盾" : hostCandidate() ? "本机未找到" : "未提供";
      diag.appendChild(h("div", { text: "版本 " + ((snap && snap.server && snap.server.version) || APP_INFO.version) + " · 通道 " + ((state.transport && state.transport.kind) || "—") + " · 当前会话 " + hostTxt }));
      m.appendChild(h("div", { class: "menu-group" },
        h("details", { open: state.diagOpen ? true : null, on: { toggle: function (e) { state.diagOpen = e.target.open; } } }, h("summary", { id: "diag-summary", text: "诊断信息" }), diag)));
      if (focusedName) { var re = m.querySelector('[name="' + focusedName + '"]:checked') || $(focusedName); if (re) re.focus(); }
    });
  }
  // The menu opens from its button, or from the footer's git at the git section; on closing,
  // focus goes back to whichever opened it, and so it does after a session is chosen.
  function openMenu(section) {
    state.menuOpen = true;
    state.menuOpener = section === "git" ? "foot-git" : "btn-menu";
    $("menu").hidden = false;
    $("btn-menu").setAttribute("aria-expanded", "true");
    renderKeys.menu = null;
    renderMenu();
    var target = section === "git" ? $("menu-git") : $("session-select");
    if (target && !target.disabled) target.focus();
  }
  function closeMenu(restore) {
    if (!state.menuOpen) return;
    state.menuOpen = false;
    $("menu").hidden = true;
    $("btn-menu").setAttribute("aria-expanded", "false");
    if (restore) { var back = $(state.menuOpener || "btn-menu"); (back && !back.hidden ? back : $("btn-menu")).focus(); }
  }

  // ------------------------------------------------------------------
  // Size reporting, ticker, visibility, wiring
  // ------------------------------------------------------------------
  var sizeObserver = null;
  var lastSize = "";
  function startSizeReporting() {
    if (!window.ResizeObserver || sizeObserver) return;
    var raf = 0;
    sizeObserver = new ResizeObserver(function () {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(function () {
        var el = document.documentElement;
        var size = { width: Math.ceil(el.scrollWidth), height: Math.ceil($("app").scrollHeight) };
        var k = size.width + "x" + size.height;
        if (k === lastSize || !state.transport) return;
        lastSize = k;
        state.transport.notify("ui/notifications/size-changed", size);
      });
    });
    sizeObserver.observe($("app"));
  }

  var ticker = null;
  function tick() {
    var t = now();
    var nodes = document.querySelectorAll("[data-rel]");
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = fmtRel(Number(nodes[i].dataset.rel), t);
    nodes = document.querySelectorAll("[data-until]");
    for (var j = 0; j < nodes.length; j++) nodes[j].textContent = fmtUntil(Number(nodes[j].dataset.until), t);
    nodes = document.querySelectorAll("[data-since]");
    for (var k = 0; k < nodes.length; k++) nodes[k].textContent = fmtSpan(t - Number(nodes[k].dataset.since));
    // a quota window's elapsed time moves on its own: the reference line and its words with it
    nodes = document.querySelectorAll("[data-tick-start]");
    for (var q = 0; q < nodes.length; q++) {
      var f = (t - Number(nodes[q].dataset.tickStart)) / Number(nodes[q].dataset.tickSpan);
      nodes[q].style.left = (Math.max(0, Math.min(1, f)) * 100).toFixed(3) + "%";
    }
    nodes = document.querySelectorAll("[data-prog-start]");
    for (var r = 0; r < nodes.length; r++) {
      var g = (t - Number(nodes[r].dataset.progStart)) / Number(nodes[r].dataset.progSpan);
      nodes[r].textContent = pctOf(Math.max(0, Math.min(1, g)) * 100) + "%";
    }
    nodes = document.querySelectorAll("[data-live]");
    for (var v = 0; v < nodes.length; v++) nodes[v].textContent = LIVE[nodes[v].dataset.live](Number(nodes[v].dataset.at), t);
    // a bar that runs down with the time it shows (the cache rule's reference)
    nodes = document.querySelectorAll("[data-fill-until]");
    for (var b = 0; b < nodes.length; b++) {
      var left = (Number(nodes[b].getAttribute("data-fill-until")) - t) / Number(nodes[b].getAttribute("data-fill-span"));
      nodes[b].style.width = (Math.max(0, Math.min(1, left)) * 100).toFixed(3) + "%";
    }
    nodes = document.querySelectorAll("[data-relabel]");
    for (var a = 0; a < nodes.length; a++) relabel(nodes[a]);
  }
  function startTicker() { if (!ticker) ticker = setInterval(tick, 1000); }

  document.addEventListener("visibilitychange", function () {
    if (state.phase === "torndown") return;
    if (document.hidden) {
      state.paused = true;
      clearTimeout(state.timer);
      state.timer = null;
      if (ticker) { clearInterval(ticker); ticker = null; }
      render();
    } else {
      state.paused = false;
      startTicker();
      tick();
      render();
      pollSoon();
    }
  });

  function wire() {
    $("btn-refresh").addEventListener("click", manualRefresh);
    $("btn-menu").addEventListener("click", function () { if (state.menuOpen) closeMenu(true); else openMenu(); });
    $("foot-git").addEventListener("click", function () { if (state.menuOpen && state.menuOpener === "foot-git") closeMenu(true); else openMenu("git"); });
    $("menu").addEventListener("keydown", function (ev) { if (ev.key === "Escape") { ev.preventDefault(); closeMenu(true); } });
    document.addEventListener("pointerdown", function (ev) {
      if (state.threadMenuOpen && !$("threads").contains(ev.target)) closeThreadMenu(false);
      if (!state.menuOpen) return;
      if ($("menu").contains(ev.target) || $("btn-menu").contains(ev.target) || $("foot-git").contains(ev.target)) return;
      closeMenu(false);
    });
    document.addEventListener("focusin", function (ev) {
      if (state.menuOpen && !$("menu").contains(ev.target) && ev.target !== $("btn-menu") && ev.target !== $("foot-git")) closeMenu(false);
    });

    var sel = $("session-select");
    sel.addEventListener("change", function () {
      var v = sel.value;
      if (v === AUTO) backToHost(); else selectSession(v);
      closeMenu(true);
    });
    sel.addEventListener("focus", function () { state.listWanted = true; });
    $("menu-copy").addEventListener("click", function (ev) { var sv = session(); if (sv) copyText(sv.id, ev.currentTarget); });
    sel.addEventListener("blur", function () { renderSessionRow(); });

    var rib = $("ribbon");
    rib.addEventListener("click", function (ev) {
      var tab = ev.target.closest && ev.target.closest('[role="tab"]');
      if (tab) setTab(tab.dataset.tab, false);
    });
    rib.addEventListener("keydown", function (ev) {
      var i = TABS.indexOf(state.tab), n = null;
      if (ev.key === "ArrowRight" || ev.key === "ArrowDown") n = (i + 1) % TABS.length;
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") n = (i - 1 + TABS.length) % TABS.length;
      else if (ev.key === "Home") n = 0;
      else if (ev.key === "End") n = TABS.length - 1;
      if (n === null) return;
      ev.preventDefault();
      setTab(TABS[n], true);
    });
    rib.addEventListener("scroll", updateRibbonFade, { passive: true });
    window.addEventListener("resize", function () { updateRibbonFade(); scrollTabIntoView($("tab-" + state.tab)); });
    window.addEventListener("pagehide", function () { clearTimeout(state.timer); });
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  restorePrefs();
  applyTheme();
  wire();
  startTicker();
  render();
  connect();
})();
