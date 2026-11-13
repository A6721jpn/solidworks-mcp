import math
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing as drawing


class DrawingSectionTests(unittest.TestCase):
    def setUp(self):
        self.line = object()
        self.created_view = object()
        self.doc = SimpleNamespace(
            ActivateView=Mock(return_value=True),
            ClearSelection2=Mock(),
            CreateSectionViewAt5=Mock(return_value=self.created_view),
        )
        self.manager = SimpleNamespace(CreateLine=Mock(return_value=self.line))
        self.extension = SimpleNamespace(DeleteSelection2=Mock(return_value=True))
        self.parent = SimpleNamespace(
            Type=7, GetName2="Parent", Angle=math.pi / 2,
            Position=[.325, .155],
            ModelToViewTransform=SimpleNamespace(
                ArrayData=[1, 0, 0, 0, 1, 0, 0, 0, 1, .325, .1578, -.01666, 8, 0, 0, 0]
            ),
        )
        self.args = dict(parent_view="Parent", x1_mm=325, y1_mm=126,
                         x2_mm=325, y2_mm=184, place_x_mm=325, place_y_mm=100)
        self.patches = [
            patch.object(drawing, "require_drawing", return_value=(object(), self.doc)),
            patch.object(drawing, "_iter_views", return_value=[self.parent]),
            patch.object(drawing, "sketch_manager", return_value=self.manager),
            patch.object(drawing, "extension", return_value=self.extension),
            patch.object(drawing, "select_object", return_value=True),
            patch.object(drawing, "rebuild", return_value=True),
            patch.object(drawing, "_view_entry", return_value={"name": "Section A-A"}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_sheet_endpoints_follow_rotated_scaled_view(self):
        out = drawing.insert_section_view(self.args)
        self.assertTrue(out["ok"])
        actual = self.manager.CreateLine.call_args.args
        for value, expected in zip(actual, [-.003975, 0, 0, .003275, 0, 0]):
            self.assertAlmostEqual(value, expected, places=12)
        self.extension.DeleteSelection2.assert_not_called()

    def test_unrotated_view_uses_projected_origin_and_scale(self):
        self.parent.Angle = 0
        self.parent.ModelToViewTransform.ArrayData[12] = 2
        args = dict(self.args, x1_mm=329, y1_mm=159.8, x2_mm=333, y2_mm=161.8)
        out = drawing.insert_section_view(args)
        self.assertTrue(out["ok"])
        for value, expected in zip(self.manager.CreateLine.call_args.args, [.002, .001, 0, .004, .002, 0]):
            self.assertAlmostEqual(value, expected, places=12)

    def test_failed_creation_removes_only_generated_line(self):
        self.doc.CreateSectionViewAt5.return_value = None
        out = drawing.insert_section_view(self.args)
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["generated_line_removed"])
        self.assertFalse(out["data"]["partial"])
        self.assertFalse(out["data"]["rollback_performed"])
        self.extension.DeleteSelection2.assert_called_once_with(0)
        self.assertTrue(all(c.args[1] is self.line for c in drawing.select_object.call_args_list))

    def test_failed_cleanup_reports_remaining_generated_line(self):
        self.doc.CreateSectionViewAt5.side_effect = RuntimeError("native section failed")
        self.extension.DeleteSelection2.return_value = False
        out = drawing.insert_section_view(self.args)
        self.assertFalse(out["ok"])
        self.assertFalse(out["data"]["generated_line_removed"])
        self.assertTrue(out["data"]["partial"])

    def test_line_selection_failure_also_removes_generated_line(self):
        drawing.select_object.side_effect = [False, True]
        out = drawing.insert_section_view(self.args)
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["generated_line_removed"])
        self.doc.CreateSectionViewAt5.assert_not_called()

    def test_existing_selection_is_neither_created_nor_deleted(self):
        for native_view in [self.created_view, None]:
            with self.subTest(section_created=native_view is not None):
                self.doc.CreateSectionViewAt5.return_value = native_view
                out = drawing.insert_section_view(dict(place_x_mm=325, place_y_mm=100, use_selection=True))
                self.assertEqual(out["ok"], native_view is not None)
        self.manager.CreateLine.assert_not_called()
        self.extension.DeleteSelection2.assert_not_called()
        drawing.select_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
