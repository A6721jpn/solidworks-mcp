from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import sw_file


class DrawingReferenceTests(unittest.TestCase):
    def test_reads_saved_paths_without_opening_or_switching_documents(self):
        drawing = Path.cwd() / "drawing.SLDDRW"
        part = Path.cwd() / "part.SLDPRT"
        missing_part = Path.cwd() / "missing.SLDPRT"
        active = object()
        app = SimpleNamespace(
            ActiveDoc=active,
            GetDocumentDependencies2=Mock(return_value=("part", str(part), "missing", str(missing_part))),
            _FlagAsMethod=Mock(),
        )
        with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path != missing_part), \
             patch.object(sw_file, "running_app", return_value=app):
            output = sw_file.inspect_document_references({"path": str(drawing)})

        self.assertEqual(output["data"]["references"], [
            {"name": "part", "path": str(part), "exists": True},
            {"name": "missing", "path": str(missing_part), "exists": False},
        ])
        app.GetDocumentDependencies2.assert_called_once_with(str(drawing), False, False, False)
        self.assertIs(app.ActiveDoc, active)

    def test_missing_drawing_is_rejected(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                sw_file.inspect_document_references({"path": str(Path.cwd() / "missing.SLDDRW")})

    def test_incomplete_reference_data_is_rejected(self):
        drawing = Path.cwd() / "drawing.SLDDRW"
        app = SimpleNamespace(GetDocumentDependencies2=Mock(return_value=("part",)), _FlagAsMethod=Mock())
        with patch.object(Path, "is_file", return_value=True), patch.object(sw_file, "running_app", return_value=app):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                sw_file.inspect_document_references({"path": str(drawing)})


if __name__ == "__main__":
    unittest.main()
