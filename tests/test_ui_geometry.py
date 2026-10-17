"""The panel's quantity geometry (the pure block of ui/src/panel.js), run in node: a small value
stays small, a near-full ring stays open, a zero draws nothing, a sliver lends no width.
Skipped without node. No browser."""

import json
import math
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "ui", "src", "panel.js")


def run_geom(expr_js: str):
    with open(JS, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("/* pure:begin")
    end = src.index("/* pure:end */")
    block = src[start:end]
    prog = block + "\nprocess.stdout.write(JSON.stringify(" + expr_js + "));\n"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True, timeout=20)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class GeometryTest(unittest.TestCase):
    C = 2 * math.pi * 5.25

    def test_ring_arc_is_true_length(self):
        r = run_geom("[0, 0.01, 0.05, 0.5, 0.98, 0.999, 1].map(Geom.ringArc)")
        self.assertIsNone(r[0])
        # 1%: flat ends, the true length (0.33px), not a dot of round caps
        self.assertEqual(r[1]["cap"], "butt")
        self.assertAlmostEqual(r[1]["dash"], self.C * 0.01, places=6)
        # round ends: the dash gives back the length the caps add
        for i, p in ((3, 0.5), (4, 0.98), (5, 0.999)):
            self.assertEqual(r[i]["cap"], "round")
            self.assertAlmostEqual(r[i]["dash"] + 2, self.C * p, places=6)
            self.assertNotIn("full", r[i])
        # 98% used leaves a visible gap: 2% of the circumference, about 0.66px
        self.assertAlmostEqual(self.C - r[4]["visible"], self.C * 0.02, places=6)
        self.assertTrue(r[6]["full"])

    def test_bar_parts_keep_true_boundaries(self):
        g = run_geom("Geom.barParts([0.01, 0, 0.6, 0.39])")
        self.assertEqual([round(p["start"], 6) for p in g["parts"]], [0, 0.01, 0.61])
        self.assertEqual([round(p["end"], 6) for p in g["parts"]], [0.01, 0.61, 1.0])
        # the sliver's gap comes out of its wider neighbour
        self.assertEqual(g["gaps"][0]["align"], "after")
        self.assertEqual(g["gaps"][1]["align"], "centre")
        one = run_geom("Geom.barParts([0.01])")
        self.assertEqual(one["gaps"], [])
        self.assertAlmostEqual(one["end"], 0.01)

    def test_columns_have_no_floor(self):
        h = run_geom("Geom.columns([1, 100, 0, 50], 48)")
        self.assertAlmostEqual(h[0], 0.48)
        self.assertEqual(h[1], 48)
        self.assertEqual(h[2], 0)
        self.assertEqual(h[3], 24)
        self.assertEqual(run_geom("Geom.columns([0, 50], 40, 100)"), [0, 20])

    def test_sand_is_by_area(self):
        top = run_geom("[0, 0.5, 1].map(Geom.sandTop)")
        self.assertEqual(top[0], 0)
        self.assertAlmostEqual(top[2], 4.2)
        k = 1.8 / 4.2
        area = lambda h: h + k * h * h  # noqa: E731
        self.assertAlmostEqual(area(top[1]) / area(4.2), 0.5, places=6)
        bot = run_geom("[0, 0.25, 1].map(Geom.sandBottom)")
        a = 1.4 / 3.6
        barea = lambda z: 4.6 * z - a * z * z  # noqa: E731
        self.assertEqual(bot[0], 0)
        self.assertAlmostEqual(barea(bot[1]) / barea(3.6), 0.25, places=6)
        self.assertAlmostEqual(bot[2], 3.6)

    def test_leader_labels_stand_on_their_lines(self):
        # F render fix: the context's two labels under the bar, at the content widths of the 320,
        # 380 and 478 sidebars and the 448 column, for used 0%, 1%, 39%, high and full. Label
        # widths are those of "■ 已用 101k" / "□ 剩余 158k" and of wider figures ("1.02M").
        cases = []
        for W in (232, 264, 324, 388):
            for u in (0, 0.01, 0.39, 0.6, 0.9, 0.97, 1):
                for wu, wf in ((70, 73), (84, 88)):
                    cases.append((W, u, wu, wf))
        expr = "[" + ",".join(
            "Geom.leaderLayout(%d,[{at:%r,w:%d},{at:%r,w:%d}])" % (W, u / 2, wu, u + (1 - u) / 2, wf)
            for W, u, wu, wf in cases) + "]"
        out = run_geom(expr)
        for (W, u, wu, wf), g in zip(cases, out):
            tag = "W=%d used=%s" % (W, u)
            a, b = g["items"]
            for o, w in ((a, wu), (b, wf)):
                # inside the content, never past its right edge (the bug: 17px past it at 478)
                self.assertGreaterEqual(o["left"], 0, tag)
                self.assertLessEqual(o["left"] + w, W + 1e-6, tag)
                # its line ends on it, at the square's middle unless held in at an edge
                self.assertTrue(o["left"] <= o["x"] <= o["left"] + w, tag)
                self.assertTrue(0 < o["x"] < W, tag)
                if 3.5 <= o["x"] <= W - w + 3.5:
                    self.assertAlmostEqual(o["left"] + 3.5, o["x"], places=6, msg=tag)
                # its readout opens toward the room it has
                self.assertEqual(o["edge"], "end" if o["left"] + w / 2 > W / 2 else "start", tag)
            self.assertEqual(a["row"], 0, tag)
            if b["row"] == 0:
                self.assertGreaterEqual(b["left"], a["left"] + wu + 12 - 1e-6, tag)
            else:
                # dropped a row: its longer line must not run through the first label
                self.assertFalse(a["left"] <= b["x"] <= a["left"] + wu, tag)
            self.assertEqual(g["rows"], 1 + b["row"], tag)
        # at the 478 sidebar both labels share one row for every share
        self.assertTrue(all(g["rows"] == 1 for (W, *_), g in zip(cases, out) if W == 388))

    def test_a_compared_pair_stacks_as_a_set(self):
        # F narrow polish: at 320 "下次若命中 $0.02" held its line and "下次若未命中 $0.19–0.24"
        # dropped its range alone to the next. Widths as rendered: names at 11px, figures at 13px.
        hit, miss = {"k": 55, "v": 36}, {"k": 66, "v": 72}
        new_miss, wide = {"k": 77, "v": 72}, {"k": 77, "v": 88}
        r = run_geom("[%s]" % ",".join("Geom.phrasesStack(%d,28,8,%s)" % (W, json.dumps(cells)) for W, cells in (
            (272, [hit, miss]),          # 320 sidebar: the range would not fit, so both go full width
            (388, [hit, miss]),          # 478: two compact columns
            (388, [new_miss, wide]),     # 478 after a model switch, a wider range: still two
            (324, [new_miss, miss]),     # 380, switched: one too wide, the set stacks
            (272, [{"k": 22, "v": 30}, {"k": 22, "v": 30}]),  # short readings keep two columns
        )))
        self.assertEqual(r, [True, False, False, True, False])
        # on one column every row is full width: the widest of these fits a 272px line
        self.assertLessEqual(wide["k"] + 8 + wide["v"], 272)

    def test_a_chosen_reading_stays_within_the_chart(self):
        # F final review M1: a history's reading was set nowrap at max(0, W - w); one wider than
        # the chart (a request with two marks, a reading from another session, another day) ran
        # past the card. Content widths of the 320 and 380 sidebars; readings of 120px to 460px
        # (the session's "本次 … · 累计 … · 缓存未记录，按未缓存估算 · 用量不全，只计已记录部分").
        cases = [(W, w, x) for W in (232, 264) for w in (120, 230, 240, 290, 460) for x in (3, 60, 131, 229, 261)
                 if x < W]
        out = run_geom("[" + ",".join("Geom.readingPlace(%d,%d,%d)" % c for c in cases) + "]")
        for (W, w, x), r in zip(cases, out):
            tag = "W=%d w=%d x=%d" % (W, w, x)
            self.assertGreaterEqual(r["left"], 0, tag)
            self.assertLessEqual(r["left"] + r["width"], W + 1e-6, tag)
            self.assertEqual(r["wrap"], w > W, tag)
            if not r["wrap"]:
                self.assertEqual(r["width"], w, tag)
                # under its column while it fits there
                if w / 2 <= x <= W - w / 2:
                    self.assertAlmostEqual(r["left"] + w / 2, x, places=6, msg=tag)
            else:
                self.assertEqual((r["left"], r["width"]), (0, W), tag)


class StyleNamesTest(unittest.TestCase):
    """One class name, one job. A name the cards build must not also be the loading placeholder's
    shimmering bar (R3: the readings' names, .sk, took the skeleton's .sk shimmer)."""

    def test_built_classes_are_not_loading_placeholders(self):
        import re
        with open(os.path.join(ROOT, "ui", "src", "panel.css"), encoding="utf-8") as fh:
            css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        with open(JS, encoding="utf-8") as fh:
            js = fh.read()
        loading = set()
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if "shimmer" in body:
                for part in sel.split(","):
                    last = re.findall(r"\.([\w-]+)", part.strip().split(" ")[-1])
                    loading.update(last)
        built = set()
        for cls in re.findall(r'class: "([^"]+)"', js):
            built.update(cls.split())
        skeleton = {c for c in built if c.startswith("skel")}
        clash = (loading & built) - skeleton - {"skeleton"}
        self.assertEqual(clash, set(), "class names shared with a loading placeholder: %s" % sorted(clash))
        # and the readings' own names are theirs alone
        for name in ("ph-k", "ph-v", "ph-u", "lg-k", "lg-v", "swatch"):
            self.assertIn(name, built)
            self.assertNotIn(name, loading)


class ReadoutPriorityTest(unittest.TestCase):
    """One readout at a time. R3: the pointer resting on one cell and the keyboard on the next
    opened both data-tip readouts, one over the other. Every rule that opens a readout on hover
    must hold back while some readout has keyboard focus; the focused one opens unconditionally."""

    def test_hover_readout_yields_to_keyboard_focus(self):
        import re
        with open(os.path.join(ROOT, "ui", "src", "panel.css"), encoding="utf-8") as fh:
            css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        hover, focus = [], []
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if not re.search(r"opacity:\s*1\b", body):
                continue
            for part in (p.strip() for p in sel.split(",")):
                if "::after" not in part or "data-tip" not in part:
                    continue
                (hover if ":hover" in part else focus).append(part)
        self.assertTrue(hover and focus, "readout rules not found")
        gate = ":root:not(:has([data-tip]:focus-visible))"
        for part in hover:
            self.assertTrue(part.startswith(gate + " "), "a hover readout that ignores keyboard focus: " + part)
        for part in focus:
            self.assertIn(":focus-visible", part)
            self.assertNotIn(":not(:has(", part)


class ReadoutFrameTest(unittest.TestCase):
    """F render fix: the readout's frame (position: relative on every [data-tip]) came later than
    .ld's position: absolute with the same weight and won, so the context's leader labels fell
    into the flow, one after the other, off their lines and past the card. The frame must carry
    no weight; the leader labels must stay absolute."""

    def test_readout_frame_does_not_outweigh_a_placing(self):
        import re
        with open(os.path.join(ROOT, "ui", "src", "panel.css"), encoding="utf-8") as fh:
            css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        framed = False
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if not re.search(r"(^|;)\s*position\s*:", body):
                continue
            for part in (p.strip() for p in sel.split(",")):
                if "data-tip" not in part or "::after" in part:
                    continue
                self.assertTrue(part.startswith(":where("), "a weighted readout frame: " + part)
                framed = True
        self.assertTrue(framed, "readout frame rule not found")
        ld = re.search(r"(?:^|\})\s*\.ld\s*\{([^}]*)\}", css)
        self.assertIsNotNone(ld)
        self.assertRegex(ld.group(1), r"position:\s*absolute")


class ReadingLineTest(unittest.TestCase):
    """F final review M1: a chosen column's reading must wrap within its line, never run on past
    it: the speed's line under the figure is not nowrap, and a history's has a wrapping form."""

    def test_readings_wrap_within_their_line(self):
        import re
        with open(os.path.join(ROOT, "ui", "src", "panel.css"), encoding="utf-8") as fh:
            css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        for sel, body in rules:
            if re.search(r"\.(lead-sub|lead-text|is-reading)\b", sel):
                self.assertNotIn("nowrap", body, sel.strip())
        wrap = [b for sel, b in rules if ".tc-reading[data-wrap" in sel]
        self.assertTrue(wrap and "white-space: normal" in wrap[0])
        probe = [b for sel, b in rules if sel.strip() == ".rd-probe"]
        self.assertTrue(probe and "visibility: hidden" in probe[0] and "position: absolute" in probe[0])


class ChartLanguageTest(unittest.TestCase):
    """0.2.2: the one chart is the desktop mod's columns. No polyline, area or scattered
    vertices are drawn or styled; the line kinds are gone from the script."""

    def test_no_lines_or_areas(self):
        import re
        with open(os.path.join(ROOT, "ui", "src", "panel.css"), encoding="utf-8") as fh:
            css = fh.read()
        with open(JS, encoding="utf-8") as fh:
            js = fh.read()
        for cls in ("tc-line", "tc-area", "tc-vtx", "tc-last", "tc-dot", "tc-hair", "tc-ref"):
            self.assertNotIn("." + cls, css, cls)
            self.assertNotIn('"' + cls, js, cls)
        self.assertNotIn('kind === "step"', js)
        self.assertIsNone(re.search(r'svg\("path", \{ d: d', js))


if __name__ == "__main__":
    unittest.main()
