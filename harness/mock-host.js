/* MOCK HOST — a minimal MCP Apps host stand-in for browser acceptance tests.
 * NOT Codex. It forwards the panel's tools/call requests to the real
 * desk-status MCP server (via harness/serve.py) and lets tests inject host
 * context changes, delays and failures through window.mockHost.
 *
 * Query parameters:
 *   w=380            iframe width (px)          h=720  minimum height
 *   theme=light|dark host theme                 thread=<uuid> host-context threadId
 *   initial=0|1      send the launching tool-result (default 1)
 *   initialDelay=ms  delay that tool-result     standalone=1 load panel top-level
 *   role=<name>      host-context threadId of a fixture role from /fixtures.json
 *                    (e.g. role=long with harness --edge)
 *   failAfter=ms&failCount=n   fail n read_status calls once ms have passed, to show
 *                    the stale/offline states and the recovery after them
 */
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var width = Number(q.get("w") || 380);
  var minH = Number(q.get("h") || 640);
  var theme = q.get("theme") === "dark" ? "dark" : "light";
  if (q.get("standalone") === "1") { location.replace("/panel.html"); return; }

  var frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("title", "会话状态面板（mock host）");
  frame.style.width = width + "px";
  frame.style.height = minH + "px";
  frame.style.background = theme === "dark" ? "#16181c" : "#fbfbfc";
  document.getElementById("stage").appendChild(frame);

  var hostContext = { theme: theme, displayMode: "inline", locale: "zh-CN", timeZone: "Asia/Shanghai", platform: "desktop" };
  if (q.get("thread")) hostContext.threadId = q.get("thread");
  var roleReady = Promise.resolve();
  if (q.get("role")) {
    roleReady = fetch("/fixtures.json").then(function (r) { return r.json(); }).then(function (f) {
      if (f[q.get("role")]) hostContext.threadId = f[q.get("role")];
    });
  }

  var M = window.mockHost = {
    calls: [],          // read_status arguments, in order
    notifications: [],  // methods the panel sent
    sizes: [],
    callDelayMs: 0,
    delayQueue: [],     // per-call delays consumed first
    failNext: 0,
    hangNext: 0,
    callMeta: null,     // _meta attached to each tools/call (as a host might)
    initialized: false,
    hostContext: hostContext,
    setContext: function (patch) {
      Object.keys(patch).forEach(function (k) { if (patch[k] === undefined) delete hostContext[k]; else hostContext[k] = patch[k]; });
      post({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: patch });
    },
    sendToolResult: function (result) { post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: result }); },
    openStatus: function (meta) { return rpc("tools/call", { name: "open_status", arguments: {}, _meta: meta || undefined }); },
    teardown: function () { post({ jsonrpc: "2.0", id: "teardown-1", method: "ui/resource-teardown", params: {} }); },
    rpc: rpc
  };

  function post(msg) { frame.contentWindow.postMessage(msg, "*"); }
  function rpc(method, params) {
    return fetch("/rpc", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ method: method, params: params }) })
      .then(function (r) { return r.json(); });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  window.addEventListener("message", function (ev) {
    if (ev.source !== frame.contentWindow) return;
    var m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.method && (m.id === undefined || m.id === null)) {
      M.notifications.push(m.method);
      if (m.method === "ui/notifications/size-changed" && m.params) {
        M.sizes.push(m.params);
        frame.style.height = Math.max(minH, Number(m.params.height) || 0) + "px";
      }
      if (m.method === "ui/notifications/initialized") onInitialized();
      return;
    }
    if (m.method === "ui/initialize") {
      post({ jsonrpc: "2.0", id: m.id, result: {
        protocolVersion: "2026-01-26",
        hostInfo: { name: "mock-host", version: "0" },
        hostCapabilities: { serverTools: {} },
        hostContext: JSON.parse(JSON.stringify(hostContext))
      } });
      return;
    }
    if (m.method === "tools/call") {
      var p = m.params || {};
      if (p.name === "read_status") M.calls.push(JSON.parse(JSON.stringify(p.arguments || {})));
      var delay = M.delayQueue.length ? M.delayQueue.shift() : M.callDelayMs;
      if (M.hangNext > 0) { M.hangNext--; return; }
      sleep(delay).then(function () {
        if (M.failNext > 0) { M.failNext--; post({ jsonrpc: "2.0", id: m.id, error: { code: -32000, message: "mock host: injected failure" } }); return; }
        var params = { name: p.name, arguments: p.arguments };
        if (M.callMeta) params._meta = M.callMeta;
        return rpc("tools/call", params).then(function (reply) {
          if (reply.error) post({ jsonrpc: "2.0", id: m.id, error: reply.error });
          else post({ jsonrpc: "2.0", id: m.id, result: reply.result });
        });
      });
      return;
    }
    post({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "mock host: method not found" } });
  });

  if (q.get("failAfter")) {
    setTimeout(function () { M.failNext = Number(q.get("failCount") || 4); }, Number(q.get("failAfter")));
  }

  function onInitialized() {
    M.initialized = true;
    if (q.get("initial") === "0") return;
    var meta = hostContext.threadId ? { threadId: hostContext.threadId } : undefined;
    var delay = Number(q.get("initialDelay") || 0);
    M.openStatus(meta).then(function (reply) {
      M.initialResult = reply.result;
      return sleep(delay).then(function () { M.sendToolResult(reply.result); M.initialSent = true; });
    });
  }

  roleReady.then(function () { frame.src = "/panel.html"; });
})();
