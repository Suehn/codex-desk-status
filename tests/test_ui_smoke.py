"""Runs the built panel script in node (tests/ui_smoke.cjs, a minimal DOM, no browser) against
snapshots the real server builds from SYNTHETIC boundary fixtures, and checks what the cards
say: one cost everywhere, retention as a rule not a fact, sample counts, provenance, the speed
of a switched model. Skipped without node."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from fixture_builder import build_edge_fixtures, build_harness_fixtures  # noqa: E402
from desk_status.discovery import SessionIndex  # noqa: E402
from desk_status.gitinfo import GitCache  # noqa: E402
from desk_status.status import StatusService  # noqa: E402

ROLES = ["low", "long", "missing", "switched", "multi", "fresh", "quota-joined", "quota-apart", "gaps", "many", "untimed", "alpha", "nocache", "legacy"]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="desk-status-ui-")
        now = int(time.time() * 1000)
        a, b, c = (os.path.join(cls.tmp, x) for x in ("a", "b", "edge"))
        ids = build_harness_fixtures(a, b, now)
        ids.update(build_edge_fixtures(c, now))
        svc = StatusService(SessionIndex([a, b, c]), GitCache(enabled=False))
        snaps = {r: svc.snapshot(manual_id=ids[r], include_sessions=True) for r in ROLES}
        # a thread run on into a second file: joined, and with a copy of its first file beside it
        # (which does not join, and is said to leave the record possibly short)
        from test_segments import T, OTHER, base_thread, next_file  # noqa: E402
        for role, copy in (("seg-joined", False), ("seg-unjoined", True)):
            root = os.path.join(cls.tmp, role)
            base, keep = base_thread()
            p1 = base.write(root, name_time="2026-09-22T10-00-00")
            next_file(base, keep).write(root, name_time="2026-09-22T10-20-00")
            if copy:
                shutil.copyfile(p1, os.path.join(os.path.dirname(p1), f"rollout-2026-09-22T10-05-00-{T}_{OTHER}.jsonl"))
            snaps[role] = StatusService(SessionIndex([root]), GitCache(enabled=False)).snapshot(host_meta={"threadId": T})
        path = os.path.join(cls.tmp, "snaps.json")
        with open(path, "w") as fh:
            json.dump(snaps, fh)
        res = subprocess.run(["node", os.path.join(HERE, "ui_smoke.cjs"), path], capture_output=True, text=True, timeout=60)
        if res.returncode:
            raise AssertionError(res.stderr)
        cls.out = json.loads(res.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_record_that_may_be_short_says_so(self):
        # 0.2.3: a thread whose further log files could not all be joined never shows its older
        # data as if whole; the one banner line says so, and nothing else on the panel changes
        ok, short = self.out["seg-joined"], self.out["seg-unjoined"]
        self.assertEqual(ok["errors"], [])
        self.assertEqual(short["errors"], [])
        self.assertEqual(ok["banner"], "")
        self.assertEqual(short["banner"], "这个会话还有后续日志未能核对衔接，显示的可能不是最新数据。")
        for tab in ("context", "quota", "cache", "speed", "session"):
            self.assertEqual(ok[tab]["text"], short[tab]["text"].replace(short["banner"], ""), tab)

    def test_every_card_renders_for_every_fixture(self):
        for role in ROLES:
            self.assertEqual(self.out[role]["errors"], [], role)
            for tab in ("context", "quota", "cache", "speed", "session"):
                self.assertTrue(self.out[role][tab]["text"], (role, tab))

    def test_long_context_cost_is_one_number(self):
        s = self.out["long"]["session"]
        self.assertIn("$9.90", s["text"])
        # a column a request: its own cost, and the running total to it
        self.assertTrue(any(v.endswith("本次 $4.95，累计 $9.90，长上下文请求") for v in s["sliders"]), s["sliders"])
        self.assertIn("$9.90", self.out["long"]["session"]["ribbon"])

    def test_a_cost_record_cut_to_its_latest_requests_still_ends_at_the_total(self):
        s = self.out["many"]["session"]
        hero = s["text"].split("API 价估算")[0].split("次请求")[-1]
        self.assertTrue(hero.startswith("$"), s["text"][:80])
        self.assertIn("最近 240 次请求", s["text"])
        # what came before the record is the running total's base, and says so
        self.assertIn("更早的 20 次请求合计", s["text"])
        self.assertTrue(s["sliders"][0].startswith("浏览每次请求费用的历史读数 = 第 240 / 240 个"), s["sliders"])
        self.assertTrue(s["sliders"][0].endswith("累计 " + hero), (s["sliders"], hero))

    def test_a_long_record_is_a_window_that_moves(self):
        """240 requests are not 240 hairlines: a window of at most 24 small columns, and the keyboard reaches the
        first request, the window moving with it."""
        s = self.out["many"]["session"]
        self.assertGreater(s["cols"], 0)
        self.assertLessEqual(s["cols"], 24)
        w = self.out["many"]["window"]
        self.assertEqual(w["home"]["now"], "1")
        self.assertTrue(w["home"]["reading"] and "本次" in w["home"]["reading"], w)
        self.assertEqual(w["end"]["now"], "240")
        self.assertTrue(w["end"]["reading"].endswith("累计 " + s["text"].split("API 价估算")[0].split("次请求")[-1]), w)
        # a window turned back: the slider's place and the first key start from its last column
        pg = w["paged"]
        self.assertLess(int(pg["atRest"]), 240, pg)
        self.assertEqual(int(pg["afterKey"]), int(pg["atRest"]) - 1, pg)

    def test_history_is_a_layer_asked_for(self):
        """Each card leads with its own graphic; its history is an entry saying what it holds
        and how many, closed until opened. The speed's columns are its graphic, not a layer."""
        # alpha's model has no built-in price, so its bill is tokens and has no cost record
        for role, tab, title in (("alpha", "context", "占用历史"), ("alpha", "quota", "已用历史"), ("alpha", "cache", "命中率历史"),
                                 ("long", "session", "每次请求费用")):
            hs = self.out[role][tab]["hist"]
            self.assertEqual(len(hs), 1, (tab, hs))
            self.assertTrue(hs[0].startswith("false " + title), (tab, hs))
            self.assertRegex(hs[0], r"\d+ 次(响应|读数|请求)$")
        self.assertEqual(self.out["alpha"]["speed"]["hist"], [])
        self.assertEqual(self.out["alpha"]["session"]["hist"], [])

    def test_cache_retention_is_a_rule_and_the_hit_rate_stays_measured(self):
        c = self.out["long"]["cache"]
        self.assertIn("超过最短保留时长", c["text"])
        self.assertIn("状态未确认", c["text"])
        self.assertNotIn("已过期", c["text"])
        self.assertIn("33%", c["text"])
        self.assertIn("下次若命中", c["text"])
        self.assertIn("下次若未命中", c["text"])
        self.assertIn("状态未确认", self.out["long"]["cache"]["ribbon"])
        # within the rule's minimum: a reference worked out from the rule, never a promise
        within = self.out["missing"]["cache"]
        self.assertIn("保留参考", within["text"])
        self.assertIn("规则推算", within["text"])
        self.assertNotIn("至少到", within["text"])
        self.assertNotIn("至少还保留", within["ribbon"])
        self.assertIn("保留参考", within["ribbon"])

    def test_estimates_are_called_estimates(self):
        c = self.out["missing"]["cache"]
        self.assertIn("API 价", c["text"])
        self.assertIn("下次若未命中", c["text"])
        self.assertFalse(any("实付" in t for t in c["tips"]), c["tips"])

    def test_next_input_says_its_tokens_are_the_last_requests(self):
        """The next request's figures rest on the last request's input plus output: said in the
        group's head, where it is always seen, and worked out in each readout. What the cache
        saved belongs to the last request and stands with its parts, not under "下次"."""
        c = self.out["missing"]["cache"]
        text = c["text"]
        head = text.index("下次输入 · 按上次输入 + 输出量")
        self.assertLess(text.index("本次节省"), head)
        self.assertGreater(text.index("下次若命中"), head)
        self.assertGreater(text.index("下次若未命中"), head)
        nxt = [t for t in c["tips"] if "假设" in t]
        self.assertEqual(len(nxt), 2, c["tips"])
        for t in nxt:
            self.assertRegex(t, r"^下次输入按上次输入 [\d,]+ \+ 输出 [\d,]+ = [\d,]+ tokens 计")
        # after a switch the head names both models, the one each cell is priced on
        sw = self.out["switched"]["cache"]["text"]
        self.assertIn("下次输入 · 按上次输入 + 输出量", sw)
        self.assertRegex(sw, r"API 价 · \S+ → \S+ · ")

    def test_cache_miss_input_and_write_are_kept_apart(self):
        # gpt-5.6-sol prices cache writes apart from input: a miss is a range from the input
        # rate to the write rate, and the readout says which is which
        c = self.out["missing"]["cache"]
        self.assertIn("下次若未命中$0.19–0.24", c["text"])
        tip = [t for t in c["tips"] if "假设下次请求全部读不到缓存" in t][0]
        self.assertIn("未缓存输入价", tip)
        self.assertIn("写入价", tip)
        # a switch of model is a switch, not a rewrite known to happen
        sw = self.out["switched"]["cache"]
        self.assertNotIn("重写", sw["text"] + sw["ribbon"])
        self.assertIn("新模型若未命中", sw["text"])
        self.assertIn("预计读不到原模型的缓存", sw["text"])
        self.assertIn("换模型", sw["ribbon"])
        self.assertIn("99.1%命中", sw["ribbon"].replace(" ", ""))

    def test_no_stray_separator_when_the_time_is_hidden(self):
        # no rule for this model: the time and its separator are gone together
        r = self.out["alpha"]["cache"]["ribbon"]
        self.assertFalse(r.rstrip().endswith("·"), r)
        self.assertNotIn("命中 ·", r)

    def test_cache_sample_count(self):
        self.assertIn("近 5 次中 1 次有记录 · 100%", self.out["missing"]["cache"]["text"])
        s = self.out["missing"]["session"]["text"]
        self.assertIn("4 次请求未记录缓存用量，按全部未缓存估算", s)
        self.assertNotIn("偏高", s)

    def test_usage_gaps_mark_the_bill_incomplete(self):
        s = self.out["gaps"]["session"]
        self.assertIn("API 价估算 · 不完整", s["text"])
        self.assertIn("2 次请求缺少输入或输出用量，金额只含已记录的部分", s["text"])
        self.assertIn("1 次请求未记录缓存用量", s["text"])
        self.assertIn("不完整", s["ribbon"])
        # the line's latest point is the request without its cache-write count
        self.assertTrue(s["sliders"][0].endswith("累计 $0.10，缓存未记录，按未缓存估算"), s["sliders"])
        # a complete bill says nothing of the kind
        self.assertNotIn("不完整", self.out["long"]["session"]["text"])

    def test_threads_total_says_what_it_leaves_out(self):
        t = self.out["multi"]["threads"]
        self.assertIn("已计价 $", t)
        self.assertNotIn("全部 $", t)
        self.assertNotIn("合计 $", t)
        self.assertIn("1 个线程未计价或用量不全，未全部计入", t)

    def test_row_names_follow_the_ticker(self):
        tick = self.out["missing"]["tick"]
        row = [i for i, n in enumerate(tick["before"]) if n.startswith("保留参考")]
        self.assertTrue(row, tick)
        i = row[0]
        self.assertNotEqual(tick["before"][i], tick["after"][i])
        self.assertTrue(tick["before"][i].startswith("保留参考，15 分") or tick["before"][i].startswith("保留参考，14 分"), tick["before"][i])
        self.assertTrue(tick["after"][i].startswith("保留参考，12 分") or tick["after"][i].startswith("保留参考，11 分"), tick["after"][i])

    def test_price_source_is_one_footnote_and_readings(self):
        s = self.out["long"]["session"]
        self.assertIn("gpt-6-astra · high · 内置 API 价 2026-10", s["text"])
        self.assertIn("长上下文2次", s["text"])
        self.assertNotIn("不是账单；", s["text"])  # the terms are the footnote's readout
        self.assertTrue(any("不是账单" in t for t in s["tips"]), s["tips"])

    def test_speed_average_and_its_columns_agree(self):
        """Fourteen untimed replies after six timed ones: the average is of five, and all five
        are among the columns drawn (the review's no_timing case)."""
        t = self.out["untimed"]["speed"]
        # the six replies are the small columns beside the figure, said in a reading and in the slider's name
        self.assertIn("图中回复6次", t["text"])
        self.assertIn("近 5 次平均", t["text"])
        self.assertIn("近 5 次平均", t["ribbon"])
        self.assertTrue(t["sliders"][0].startswith("浏览输出速度（最近 6 次回复）的历史读数 = 第 6 / 6 个"), t["sliders"])
        self.assertIn("上次计时", t["text"])
        self.assertIn("最近一次回复不计时：仅工具调用。", t["text"])
        self.assertNotIn("无计时", t["text"])

    def test_quota_reads_used_with_remaining_beside_it(self):
        q = self.out["low"]["quota"]
        self.assertIn("98% 已用", q["text"].replace("98%已用", "98% 已用"))
        self.assertIn("剩余 2.0%", q["text"])
        self.assertIn("已用 98%，剩余 2.0%", q["ribbon"])

    def test_quota_provenance(self):
        self.assertIn("本会话记录", self.out["quota-apart"]["quota"]["text"])
        self.assertIn("当前读数来自会话", self.out["quota-apart"]["quota"]["text"])
        self.assertNotIn("本会话记录", self.out["quota-joined"]["quota"]["text"])
        self.assertTrue(any("来自会话" in v for v in self.out["quota-joined"]["quota"]["sliders"]))

    def test_switched_model(self):
        sp = self.out["switched"]["speed"]
        self.assertIn("切换前", sp["text"])
        # the ribbon itself says so, not only its accessible name
        visible = sp["ribbon"].split(" | ", 1)[1]
        self.assertIn("切换前", visible)
        self.assertIn("tok/s", visible)
        self.assertNotIn("切换前", self.out["alpha"]["speed"]["ribbon"])

    def test_a_chosen_reply_reads_under_the_figure_not_over_the_columns(self):
        # F final review M1: at 320 the chosen reply's reading ran from under the figure into the
        # 116px columns beside it. The line it takes is now the card's width, under both.
        r = self.out["alpha"]["speedRead"]
        self.assertFalse(r["besideChart"])
        self.assertRegex(r["rest"], r"^近 \d+ 次平均$")
        self.assertTrue(r["reading"])
        self.assertEqual(r["now"], r["max"])
        self.assertIn("tok/s", r["chosen"])
        self.assertIn("tokens /", r["chosen"])
        self.assertEqual(r["back"], r["rest"])
        # the measuring copy holds nothing once measured, so the card's words are only its own
        self.assertTrue(r["probeEmpty"])

    def test_speed_says_how_it_was_measured(self):
        t = self.out["alpha"]["speed"]["text"]
        self.assertIn("日志估算", t)
        self.assertNotIn("第一个 token", t)
        self.assertIn("tok/s", self.out["alpha"]["speed"]["ribbon"])

    def test_readings_are_name_then_figure(self):
        """The F card's quiet rows: each reading is its name, then its figure; the parts of a
        whole carry a square of their segment's colour."""
        c = self.out["switched"]["cache"]["text"]
        self.assertIn("缓存读取232k", c)
        self.assertIn("本次节省$0.44", c)
        ctx = self.out["alpha"]["context"]["text"]
        # the context's two stretches named under the bar: used and free, nothing invented
        self.assertRegex(ctx, r"已用[\d.]+k剩余[\d.]+k")
        for made_up in ("对话", "工具", "MCP", "Skills", "系统提示"):
            self.assertNotIn(made_up, ctx)

    def test_unknown_is_not_zero(self):
        self.assertIn("—", self.out["fresh"]["context"]["ribbon"])
        self.assertIn("未记录", self.out["nocache"]["cache"]["text"])

    def test_background_reads_stay_quiet(self):
        # the panel read every fixture at least once on its own; none turned the refresh mark
        for role in ROLES:
            self.assertEqual(self.out[role]["busy"], [], role)

    def test_charts_are_history_browsers(self):
        sl = self.out["alpha"]["context"]["sliders"]
        self.assertTrue(sl and sl[0].startswith("浏览上下文占用的历史读数 = 第 "), sl)


if __name__ == "__main__":
    unittest.main()
