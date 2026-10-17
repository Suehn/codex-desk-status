/* Browser acceptance for the panel inside the MOCK HOST (harness/).
 * Not a Codex host: proves the bundle's bridge/lifecycle/layout logic only.
 * Data is SYNTHETIC fixture data served by the real backend over stdio.
 *
 *   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/panel.test.cjs [--shots DIR]
 */
"use strict";
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const shotsArg = process.argv.indexOf("--shots");
const SHOTS = shotsArg > 0 ? path.resolve(process.argv[shotsArg + 1]) : null;
const PORT = Number(process.env.HARNESS_PORT || 8790);

let passed = 0, failed = 0;
const results = [];
function assert(cond, msg) { if (!cond) throw new Error(msg); }
async function test(name, fn) {
  try { await fn(); passed++; results.push("ok   " + name); console.log("ok   " + name); }
  catch (e) { failed++; results.push("FAIL " + name + " — " + e.message); console.log("FAIL " + name + "\n     " + (e.stack || e.message).split("\n").slice(0, 3).join("\n     ")); }
}

function startHarness() {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", ["-B", path.join(ROOT, "harness", "serve.py"), "--port", String(PORT)], { cwd: ROOT });
    let buf = "";
    p.stdout.on("data", d => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl >= 0) { try { resolve({ proc: p, info: JSON.parse(buf.slice(0, nl)) }); } catch (e) { reject(e); } }
    });
    p.on("exit", c => reject(new Error("harness exited " + c)));
    setTimeout(() => reject(new Error("harness start timeout")), 15000);
  });
}

(async () => {
  const { proc, info } = await startHarness();
  const F = info.fixtures;
  const base = `http://127.0.0.1:${PORT}/host.html`;
  const browser = await chromium.launch();
  const errors = [];

  async function open(q, opts) {
    const w = Number((q.match(/w=(\d+)/) || [0, 380])[1]);
    const page = await browser.newPage({ viewport: { width: w + 40, height: 900 }, deviceScaleFactor: 2, reducedMotion: (opts && opts.reducedMotion) || "no-preference" });
    page.on("pageerror", e => errors.push(String(e)));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(base + "?" + q);
    const f = page.frameLocator("iframe");
    return { page, f };
  }
  const calls = page => page.evaluate(() => window.mockHost.calls.slice());
  const lastCall = async page => { const c = await calls(page); return c[c.length - 1] || {}; };
  async function waitFor(fn, ms, msg) {
    const t0 = Date.now();
    while (Date.now() - t0 < (ms || 8000)) { if (await fn()) return; await new Promise(r => setTimeout(r, 100)); }
    throw new Error("timeout: " + (msg || "condition"));
  }
  const text = (f, sel) => f.locator(sel).first().textContent();
  // the session picker lives in the footer's menu, so it is opened first
  async function pick(f, value) {
    if (!(await f.locator("#session-select").isVisible())) await f.locator("#btn-menu").click();
    await f.locator("#session-select").selectOption(value);
  }
  const sessionIdShown = async f => (await f.locator(".id-line span").first().textContent().catch(() => null));
  async function showsSession(page, f, id) {
    await f.locator("#tab-session").click();
    await waitFor(async () => (await sessionIdShown(f)) === id, 8000, "session " + id.slice(-4));
  }
  async function noOverflow(page) {
    const frame = page.frames().find(fr => fr !== page.mainFrame());
    return frame.evaluate(() => {
      const d = document.documentElement;
      const wide = Array.from(document.querySelectorAll("body *")).filter(el => {
        if (el.closest("#ribbon") || el.closest(".sprite") || el.closest("#menu") || el.closest(".sr-only")) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && (r.right > d.clientWidth + 0.5 || r.left < -0.5);
      }).map(el => el.className || el.tagName);
      return { scroll: d.scrollWidth, client: d.clientWidth, offenders: wide.slice(0, 5) };
    });
  }

  // ---------------------------------------------------------------- layout & screenshots
  for (const [w, theme] of [[380, "light"], [380, "dark"], [1100, "light"], [1100, "dark"], [320, "light"], [320, "dark"]]) {
    await test(`layout ${w}px ${theme}: every tab renders without horizontal overflow`, async () => {
      const { page, f } = await open(`w=${w}&theme=${theme}&thread=${F.alpha}`);
      await waitFor(async () => (await text(f, "#tab-context .tab-value")) !== "—", 8000, "ribbon values");
      for (const tab of ["context", "quota", "cache", "speed", "session"]) {
        await f.locator("#tab-" + tab).click();
        await f.locator('#panel .d-title').first().waitFor();
        const o = await noOverflow(page);
        assert(o.scroll <= o.client && o.offenders.length === 0, `${tab}: overflow ${JSON.stringify(o)}`);
        if (SHOTS && (w !== 320 || tab === "cache") && (w === 380 || tab === "quota" || tab === "speed" || w === 320)) {
          await page.waitForTimeout(450);
          await page.locator("iframe").screenshot({ path: path.join(SHOTS, `mock-${w}-${theme}-${tab}.png`) });
        }
      }
      await page.close();
    });
  }

  // ---------------------------------------------------------------- ARIA tabs
  await test("ARIA tabs: roles, roving tabindex, arrow/Home/End keys", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await f.locator('[role="tablist"]').waitFor();
    assert(await f.locator('[role="tab"]').count() === 5, "5 tabs");
    await f.locator("#tab-context").click();
    await page.keyboard.press("ArrowRight");
    assert(await f.locator("#tab-quota").getAttribute("aria-selected") === "true", "quota selected");
    assert(await f.locator("#tab-quota").getAttribute("tabindex") === "0", "roving tabindex");
    assert(await f.locator("#tab-context").getAttribute("tabindex") === "-1", "others -1");
    assert(await f.locator("#panel").getAttribute("aria-labelledby") === "tab-quota", "panel labelled");
    await page.keyboard.press("End");
    assert(await f.locator("#tab-session").getAttribute("aria-selected") === "true", "End");
    const focused = await page.frames()[1].evaluate(() => document.activeElement.id);
    assert(focused === "tab-session", "focus follows selection");
    await page.keyboard.press("Home");
    assert(await f.locator("#tab-context").getAttribute("aria-selected") === "true", "Home");
    await page.keyboard.press("ArrowLeft");
    assert(await f.locator("#tab-session").getAttribute("aria-selected") === "true", "wraps");
    await page.close();
  });

  await test("320px ribbon scrolls the keyboard-selected tab into view", async () => {
    const { page, f } = await open(`w=320&thread=${F.alpha}`);
    await waitFor(async () => (await text(f, "#tab-session .tab-value")) !== "—");
    await f.locator("#tab-context").focus();
    await page.keyboard.press("End");
    await page.waitForTimeout(500);
    const vis = await page.frames()[1].evaluate(() => {
      const r = document.getElementById("ribbon").getBoundingClientRect(), t = document.getElementById("tab-session").getBoundingClientRect();
      return t.left >= r.left - 1 && t.right <= r.right + 1;
    });
    assert(vis, "session tab visible after End");
    await page.close();
  });

  await test("active tab survives polling, manual refresh and session switch", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await f.locator("#tab-cache").click();
    await page.waitForTimeout(2600);
    assert(await f.locator("#tab-cache").getAttribute("aria-selected") === "true", "after poll");
    await f.locator("#btn-refresh").click();
    await pick(f, F.beta);
    await waitFor(async () => (await text(f, "#foot-binding")) === "手动选择");
    assert(await f.locator("#tab-cache").getAttribute("aria-selected") === "true", "after switch");
    assert((await text(f, "#panel .d-title")) === "提示缓存", "cache detail shown");
    await page.close();
  });

  // ---------------------------------------------------------------- host lifecycle (findings 4–7)
  await test("finding 5: host context A → B rebinds to B; A → none unbinds", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await page.evaluate(id => window.mockHost.setContext({ threadId: id }), F.beta);
    await showsSession(page, f, F.beta);
    assert((await lastCall(page)).hostSessionId === F.beta, "poll carries B");
    await page.evaluate(() => window.mockHost.setContext({ threadId: null }));
    await waitFor(async () => (await f.locator(".state h2").count()) > 0 && (await text(f, ".state h2")) === "未识别当前会话", 8000, "unbound state");
    await page.waitForTimeout(2300);
    const c = await lastCall(page);
    assert(!c.hostSessionId && !c.sessionId, "no stale A/B id after removal: " + JSON.stringify(c));
    await page.close();
  });

  await test("finding 5: host id that is not local shows unbound, never a stale previous session", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await page.evaluate(() => window.mockHost.setContext({ threadId: "abcdef01-2345-4678-89ab-cdef01234567" }));
    await waitFor(async () => (await f.locator(".state h2").count()) > 0, 8000, "unbound");
    assert((await text(f, ".state p")).includes("不在本机"), "explains non-local");
    await page.close();
  });

  await test("finding 4: related/parent ids in host context never bind; conflicting keys are ambiguous", async () => {
    const { page, f } = await open(`w=380&initial=0`);
    await page.evaluate(([a, b]) => window.mockHost.setContext({ parentThreadId: a, relatedThreadId: b, sessionId: a }), [F.alpha, F.beta]);
    await waitFor(async () => (await f.locator(".state h2").count()) > 0, 8000, "unbound");
    let c = await lastCall(page);
    assert(!c.hostSessionId, "parent/related ignored: " + JSON.stringify(c));
    await page.evaluate(([a, b]) => window.mockHost.setContext({ threadId: a, "openai/threadId": b }), [F.alpha, F.beta]);
    await page.waitForTimeout(2300);
    c = await lastCall(page);
    assert(!c.hostSessionId, "ambiguous not sent: " + JSON.stringify(c));
    assert((await text(f, ".state h2")) === "未识别当前会话", "still unbound");
    await page.close();
  });

  await test("finding 5/6: manual C survives host A → B; back-to-current then targets B", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await pick(f, F.legacy);
    await showsSession(page, f, F.legacy);
    await page.evaluate(id => window.mockHost.setContext({ threadId: id }), F.beta);
    await page.waitForTimeout(2500);
    assert((await sessionIdShown(f)) === F.legacy, "manual kept");
    assert((await lastCall(page)).sessionId === F.legacy, "poll keeps manual");
    await f.locator("#binding-note .link-btn").click();
    await showsSession(page, f, F.beta);
    assert((await text(f, "#foot-binding")) === "当前会话", "auto again");
    await page.close();
  });

  await test("finding 6: explicitly picking the current chat A is sticky when host moves to B", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await pick(f, F.alpha);
    await waitFor(async () => (await text(f, "#foot-binding")) === "手动选择", 8000, "manual");
    await page.evaluate(id => window.mockHost.setContext({ threadId: id }), F.beta);
    await page.waitForTimeout(2500);
    assert((await sessionIdShown(f)) === F.alpha, "still A");
    await pick(f, "__auto");
    await showsSession(page, f, F.beta);
    await page.close();
  });

  await test("finding 7: a late launching tool-result cannot overwrite a newer manual choice", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}&initialDelay=3500`);
    await f.locator("#session-select:not([disabled])").waitFor({ state: "attached", timeout: 8000 });
    await waitFor(async () => (await f.locator(`#session-select option[value="${F.legacy}"]`).count()) > 0, 8000, "list");
    await pick(f, F.legacy);
    await showsSession(page, f, F.legacy);
    await waitFor(() => page.evaluate(() => window.mockHost.initialSent === true), 8000, "initial sent");
    await page.waitForTimeout(600);
    assert((await sessionIdShown(f)) === F.legacy, "late initial ignored");
    await page.close();
  });

  await test("finding 7: a late launching result cannot override a newer host context", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}&initialDelay=3000`);
    await page.waitForTimeout(300);
    await page.evaluate(id => window.mockHost.setContext({ threadId: id }), F.beta);
    await showsSession(page, f, F.beta);
    await waitFor(() => page.evaluate(() => window.mockHost.initialSent === true), 8000, "initial sent");
    await page.waitForTimeout(2500);
    assert((await sessionIdShown(f)) === F.beta, "still B");
    assert((await lastCall(page)).hostSessionId === F.beta, "polls B");
    await page.close();
  });

  await test("stale reply from before a switch is dropped (out-of-order)", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await page.evaluate(() => { window.mockHost.delayQueue.push(2500); });
    await page.waitForTimeout(2200); // a slow poll for A is now in flight
    await pick(f, F.beta);
    await showsSession(page, f, F.beta);
    await page.waitForTimeout(2000);
    assert((await sessionIdShown(f)) === F.beta, "never flips back to A");
    await page.close();
  });

  // ---------------------------------------------------------------- honesty & safety
  await test("finding 2: missing cache field shows 未记录, not 0%", async () => {
    const { page, f } = await open(`w=380&initial=0`);
    await waitFor(async () => (await f.locator(`#session-select option[value="${F.nocache}"]`).count()) > 0, 8000, "list");
    await pick(f, F.nocache);
    await f.locator("#tab-cache").click();
    await waitFor(async () => (await text(f, "#panel .figure-note").catch(() => "")) === "未记录", 8000, "未记录");
    assert((await text(f, "#tab-cache .tab-value")) === "—", "ribbon shows —");
    const shown = (await f.locator("#panel .figure").textContent()) + (await f.locator("#panel .stats").first().textContent());
    assert(!/(^|[^\d.])0(\.0)?%/.test(shown), "no 0% in figure/readings: " + shown);
    await page.close();
  });

  await test("finding 3: renamed foreign log is flagged and never auto-bound", async () => {
    const { page, f } = await open(`w=380&thread=${F.renamed}`);
    await waitFor(async () => (await f.locator(".state h2").count()) > 0, 8000, "unbound");
    await pick(f, F.renamed);
    await waitFor(async () => (await f.locator(".banner").count()) > 0, 8000, "banner");
    assert((await text(f, ".banner")).includes("不一致"), "mismatch banner");
    await page.close();
  });

  await test("hostile log strings render as text (no script execution)", async () => {
    const { page, f } = await open(`w=380&initial=0`);
    await waitFor(async () => (await f.locator(`#session-select option[value="${F.hostile}"]`).count()) > 0, 8000, "list");
    await pick(f, F.hostile);
    await f.locator("#tab-session").click();
    await waitFor(async () => (await sessionIdShown(f)) === F.hostile);
    const frame = page.frames()[1];
    const r = await frame.evaluate(() => ({ xss: window.__xss, imgs: document.querySelectorAll("#panel img, #panel script, #app svg[onload]").length, model: document.getElementById("panel").textContent }));
    assert(r.xss === undefined && r.imgs === 0, "no injected nodes");
    assert(r.model.includes("<b onmouseover"), "literal text shown");
    await page.close();
  });

  await test("failures: offline banner after 3 errors, recovery clears it", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await page.evaluate(() => { window.mockHost.failNext = 3; });
    await waitFor(async () => (await f.locator('.banner[data-tone="crit"]').count()) > 0, 20000, "offline banner");
    assert((await sessionIdShown(f)) === F.alpha, "keeps last data");
    await f.locator('.banner .btn').click();
    await waitFor(async () => (await f.locator(".banner").count()) === 0, 10000, "recovered");
    await page.close();
  });

  await test("standalone (no host): plain guidance, no fabricated data", async () => {
    const page = await browser.newPage({ viewport: { width: 420, height: 700 } });
    await page.goto(`http://127.0.0.1:${PORT}/panel.html`);
    await page.locator(".state h2").waitFor();
    assert((await page.locator(".state h2").textContent()) === "请在 Codex 中打开", "title");
    assert(await page.locator("#ribbon-wrap").isHidden(), "no metrics");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "standalone-420-light.png") });
    await page.close();
  });

  await test("teardown stops polling", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`);
    await showsSession(page, f, F.alpha);
    await page.evaluate(() => window.mockHost.teardown());
    await page.waitForTimeout(300);
    const n = (await calls(page)).length;
    await page.waitForTimeout(4500);
    assert((await calls(page)).length === n, "no polls after teardown");
    await page.close();
  });

  await test("reduced motion stills the refresh spinner", async () => {
    const { page, f } = await open(`w=380&thread=${F.alpha}`, { reducedMotion: "reduce" });
    await showsSession(page, f, F.alpha);
    const dur = await page.frames()[1].evaluate(() => { const b = document.getElementById("btn-refresh"); b.setAttribute("data-busy", "true"); return parseFloat(getComputedStyle(b.querySelector("svg")).animationDuration); });
    assert(dur < 0.01, "spinner duration " + dur);
    await page.close();
  });

  await test("no page errors in any scenario", async () => {
    assert(errors.length === 0, errors.slice(0, 3).join(" | "));
  });

  await browser.close();
  proc.kill();
  console.log(`\n${passed} passed, ${failed} failed (MOCK HOST — not a real Codex side panel)`);
  if (SHOTS) fs.writeFileSync(path.join(SHOTS, "browser-results.txt"), results.join("\n") + `\n${passed} passed, ${failed} failed\n`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
