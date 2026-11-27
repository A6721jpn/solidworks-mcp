from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import sw_file


class ReplaceDrawingReferenceTests(unittest.TestCase):
    def setUp(self):
        self.drawing = Path.cwd() / "drawing.SLDDRW"
        self.old = Path.cwd() / "old.SLDPRT"
        self.new = Path.cwd() / "new.SLDPRT"
        self.other = Path.cwd() / "other.SLDPRT"
        self.args = {"drawing_path": str(self.drawing), "old_reference_path": str(self.old),
                     "new_reference_path": str(self.new)}
        self.app = SimpleNamespace(
            _FlagAsMethod=Mock(),
            GetOpenDocumentByName=Mock(return_value=None),
            GetDocumentDependencies2=Mock(side_effect=[
                ("old", str(self.old), "other", str(self.other)),
                ("new", str(self.new), "other", str(self.other)),
            ]),
            ReplaceReferencedDocument=Mock(return_value=True),
        )

    def run_tool(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(sw_file, "running_app", return_value=self.app):
            return sw_file.replace_drawing_reference(self.args)

    def test_replaces_one_reference_and_checks_others(self):
        output = self.run_tool()
        self.assertTrue(output["ok"])
        self.assertEqual([ref["path"] for ref in output["data"]["references"]], [str(self.new), str(self.other)])
        self.app.ReplaceReferencedDocument.assert_called_once_with(str(self.drawing), str(self.old), str(self.new))

    def test_refuses_an_open_drawing_or_unexpected_reference(self):
        self.app.GetOpenDocumentByName.return_value = object()
        with self.assertRaisesRegex(RuntimeError, "Close the drawing"):
            self.run_tool()
        self.app.ReplaceReferencedDocument.assert_not_called()
        self.app.GetOpenDocumentByName.return_value = None
        self.args["old_reference_path"] = str(Path.cwd() / "wrong.SLDPRT")
        with self.assertRaisesRegex(RuntimeError, "not found exactly once"):
            self.run_tool()
        self.app.ReplaceReferencedDocument.assert_not_called()

    def test_refuses_a_missing_replacement_part(self):
        with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path != self.new), \
             patch.object(sw_file, "running_app", return_value=self.app):
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                sw_file.replace_drawing_reference(self.args)
        self.app.ReplaceReferencedDocument.assert_not_called()

    def test_reports_uncertain_changes_when_readback_does_not_match(self):
        self.app.GetDocumentDependencies2.side_effect = [("old", str(self.old)), ("old", str(self.old))]
        output = self.run_tool()
        self.assertFalse(output["ok"])
        self.assertTrue(output["data"]["may_have_changes"])

    def test_reports_uncertain_changes_when_native_call_fails(self):
        self.app.ReplaceReferencedDocument.side_effect = RuntimeError("native failure")
        output = self.run_tool()
        self.assertFalse(output["ok"])
        self.assertTrue(output["data"]["may_have_changes"])


if __name__ == "__main__":
    unittest.main()
