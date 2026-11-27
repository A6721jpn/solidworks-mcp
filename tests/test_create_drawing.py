"""Drawing template and inherited sheet settings; no COM attachment."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing


class Sheet:
    def __init__(self):
        self.properties = [8, 12, 10.0, 1.0, False, 0.42, 0.297, True]
        self.GetName = "Sheet1"
        self.GetTemplateName = r"C:\company\A3.slddrt"

    def GetProperties2(self):
        return tuple(self.properties)

    def SetScale(self, numerator, denominator, scale_positions, scale_text):
        self.properties[2:4] = [numerator, denominator]
        return True

    def SetProperties2(self, *properties):
        self.properties = list(properties)


class CreateDrawingTests(unittest.TestCase):
    def setUp(self):
        self.sheet = Sheet()
        self.doc = SimpleNamespace(GetCurrentSheet=self.sheet, GetSheetNames=["Sheet1"])
        self.app = SimpleNamespace(NewDocument=Mock(return_value=self.doc))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.running_app = self.stack.enter_context(patch.object(sw_drawing, "running_app", return_value=self.app))
        self.default = self.stack.enter_context(patch.object(sw_drawing, "new_document", return_value={"ok": True}))
        self.stack.enter_context(patch.object(sw_drawing, "require_drawing", return_value=(self.app, self.doc)))
        self.stack.enter_context(patch.object(sw_drawing, "document_info", return_value={"document_key": "untitled:Draw1"}))

    def test_explicit_template_is_normalized_and_its_settings_inherited(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "company.DRWDOT"
            template.touch()
            out = sw_drawing.create_drawing({"template_path": template.as_posix()})
            self.app.NewDocument.assert_called_once_with(str(template.resolve()), 0, 0.0, 0.0)
            self.assertEqual(out["data"]["template_path"], str(template.resolve()))
        self.assertTrue(out["ok"])
        self.default.assert_not_called()
        self.assertEqual(out["data"]["document"]["document_key"], "untitled:Draw1")
        self.assertEqual(out["data"]["sheets"], ["Sheet1"])
        self.assertEqual(out["data"]["sheet"], {
            "name": "Sheet1", "paper_size": "A3", "sheet_size_mm": [420.0, 297.0],
            "scale": [10.0, 1.0], "first_angle": False,
            "sheet_format_path": r"C:\company\A3.slddrt",
        })

    def test_invalid_explicit_template_is_refused_before_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            wrong_type = Path(folder) / "part.prtdot"
            wrong_type.touch()
            for path in [str(Path(folder) / "missing.drwdot"), str(wrong_type), "company.drwdot"]:
                with self.subTest(path=path):
                    out = sw_drawing.create_drawing({"template_path": path})
                    self.assertFalse(out["ok"])
        self.running_app.assert_not_called()
        self.default.assert_not_called()

    def test_omitted_template_keeps_existing_default_creation(self):
        out = sw_drawing.create_drawing({})
        self.assertTrue(out["ok"])
        self.default.assert_called_once_with("drawing")
        self.running_app.assert_not_called()
        self.assertEqual(out["data"]["sheet"]["scale"], [10.0, 1.0])
        self.assertFalse(out["data"]["sheet"]["first_angle"])

    def test_scale_only_override_preserves_paper_projection_and_format(self):
        out = sw_drawing.create_drawing({"scale_numerator": 5})
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["sheet"]["scale"], [5.0, 1.0])
        self.assertEqual(out["data"]["sheet"]["paper_size"], "A3")
        self.assertFalse(out["data"]["sheet"]["first_angle"])
        self.assertEqual(out["data"]["sheet"]["sheet_format_path"], r"C:\company\A3.slddrt")

    def test_failed_scale_override_is_not_success(self):
        self.sheet.SetScale = lambda *args: False
        out = sw_drawing.create_drawing({"scale_numerator": 5})
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["partial"])
        self.assertEqual(out["data"]["document"]["document_key"], "untitled:Draw1")

    def test_ignored_projection_override_is_not_success(self):
        self.sheet.SetProperties2 = lambda *args: None
        out = sw_drawing.create_drawing({"first_angle": True})
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["partial"])
        self.assertFalse(out["data"]["sheet"]["first_angle"])


if __name__ == "__main__":
    unittest.main()
