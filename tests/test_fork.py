"""Offline tests for the fork additions: snapshot rendering, diffs, compact output.

    ..\\venv\\Scripts\\python.exe -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import server, sw_snapshot  # noqa: E402


def _snap(features, **extra):
    base = {
        "doc": {"title": "Part1", "document_type": "part", "dirty": True},
        "features": features,
        "hidden": 3,
        "dims_included": True,
        "size_mm": [60.0, 40.0, 20.0],
        "bodies": 1,
        "volume_mm3": 48000.0,
        "mass_g": 0.0,
        "problems": {},
    }
    base.update(extra)
    return base


class RenderTests(unittest.TestCase):
    def test_render_is_terse(self) -> None:
        text = sw_snapshot.render_snapshot(_snap([
            {"i": 5, "name": "Base", "type": "Sketch", "sketch": "fully_defined", "dims": {"D1@Base": 60.0, "D2@Base": 40.0}},
            {"i": 6, "name": "BasePad", "type": "Boss-Extrude", "dims": {"D1@BasePad": 20.0, "A1@BasePad": "5deg"}},
        ]))
        lines = text.splitlines()
        self.assertEqual(lines[0], "doc: Part1 (part, unsaved)")
        self.assertIn("size 60 x 40 x 20 mm", lines[1])
        self.assertIn("vol 48000 mm3", lines[1])
        self.assertIn(" F5 Base [Sketch, fully_defined] D1@Base=60 D2@Base=40", lines)
        self.assertIn(" F6 BasePad [Boss-Extrude] D1@BasePad=20 A1@BasePad=5deg", lines)
        self.assertLess(len(text), 400)

    def test_render_marks_errors(self) -> None:
        text = sw_snapshot.render_snapshot(_snap(
            [{"i": 7, "name": "Fillet1", "type": "Fillet"}], problems={"Fillet1": "", "Ghost": "gone"}
        ))
        self.assertIn(" F7 Fillet1 [Fillet]  !ERR", text)
        self.assertIn(" !ERR Ghost: gone", text)


class DiffTests(unittest.TestCase):
    def test_no_change_is_empty(self) -> None:
        before = _snap([{"i": 5, "name": "Base", "type": "Sketch", "dims": {"D1@Base": 60.0}}])
        self.assertEqual(sw_snapshot.diff_snapshots(before, json.loads(json.dumps(before))), "")

    def test_added_removed_changed(self) -> None:
        before = _snap([
            {"i": 5, "name": "Base", "type": "Sketch", "sketch": "under_defined", "dims": {"D1@Base": 60.0}},
            {"i": 6, "name": "Old", "type": "Fillet"},
        ])
        after = _snap([
            {"i": 5, "name": "Base", "type": "Sketch", "sketch": "fully_defined", "dims": {"D1@Base": 65.0}},
            {"i": 6, "name": "Hole", "type": "Cut-Extrude", "dims": {"D1@Hole": 10.0}},
        ], size_mm=[65.0, 40.0, 20.0], volume_mm3=50000.0, problems={"Hole": ""})
        text = sw_snapshot.diff_snapshots(before, after)
        self.assertIn(" + F6 Hole [Cut-Extrude] D1@Hole=10", text)
        self.assertIn(" - Old [Fillet]", text)
        self.assertIn(" ~ Base: under_defined -> fully_defined", text)
        self.assertIn(" ~ D1@Base 60 -> 65", text)
        self.assertIn(" size 60 x 40 x 20 -> 65 x 40 x 20 mm", text)
        self.assertIn(" vol 48000 -> 50000 mm3 (+2000)", text)
        self.assertIn(" !ERR +Hole", text)


class CompactTests(unittest.TestCase):
    def test_compact_numbers(self) -> None:
        out = server.compact_numbers({"a": 12.3456789, "b": 0.000123456789, "c": 1e-15, "d": [1.0, 2.5], "e": "x"})
        self.assertEqual(out["a"], 12.346)
        self.assertEqual(out["b"], 0.000123457)
        self.assertEqual(out["c"], 0.0)
        self.assertEqual(out["d"], [1.0, 2.5])
        self.assertEqual(out["e"], "x")

    def test_render_compact_json_and_text_passthrough(self) -> None:
        old = server.COMPACT
        try:
            server.COMPACT = True
            [content] = server.render({"ok": True, "message": "m", "data": {"v": 1.23456}})
            self.assertEqual(content.text, '{"ok":true,"message":"m","data":{"v":1.235}}')
            [content] = server.render({"ok": True, "message": "m", "_text": "doc: x", "diff": " + F1 A [Sketch]"})
            self.assertEqual(content.text, "doc: x\ndiff:\n + F1 A [Sketch]")
            [content] = server.render({"ok": False, "message": "bad", "_text": "doc: x"})
            self.assertTrue(content.text.startswith("ERROR: bad\n"))
        finally:
            server.COMPACT = old

    def test_read_only_set_matches_registered_tools(self) -> None:
        names = {t.name for t in server.TOOLS}
        unknown = server.READ_ONLY_TOOLS - names
        self.assertEqual(unknown, set(), f"READ_ONLY_TOOLS names not registered: {unknown}")


if __name__ == "__main__":
    unittest.main()
