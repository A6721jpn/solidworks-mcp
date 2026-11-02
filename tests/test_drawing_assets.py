"""Drawing assets exercised offline; never attaches to SOLIDWORKS."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing_assets as assets


class Annotation:
    def __init__(self, name, owner=1):
        self.name = name
        self.OwnerType = owner
        self.Layer = "Notes"
        self.position = [0.02, 0.03, 0]
        self.formats = [SimpleNamespace(TypefaceName="Arial", CharHeight=0.003, Bold=False)]

    def GetName(self):
        return self.name

    def SetName(self, name):
        self.name = name
        return True

    def GetPosition(self):
        return self.position

    def SetPosition2(self, *position):
        self.position = list(position)
        return True

    def GetTextFormatCount(self):
        return len(self.formats)

    def GetTextFormat(self, index):
        return self.formats[index]

    def GetUseDocTextFormat(self, index):
        return False

    def SetTextFormat(self, index, use_doc, fmt):
        self.formats[index] = deepcopy(fmt)
        return True


class Note:
    def __init__(self, name, text, owner=1):
        self.annotation = Annotation(name, owner)
        self.PropertyLinkedText = text
        self.Angle = 0
        self.AllUpperCase = False
        self.BehindSheet = False
        self.justification = 1

    def GetAnnotation(self):
        return self.annotation

    def GetText(self):
        return self.PropertyLinkedText.replace('$PRP:"Title"', "Resolved title")

    def GetTextJustification(self):
        return self.justification

    def SetTextJustification(self, val):
        self.justification = val

    def HasMultipleFonts(self):
        return False

    def GetTextCount(self):
        return 1

    def GetTextAtIndex(self, index):
        return self.PropertyLinkedText

    def GetTextFontAtIndex(self, index):
        return self.annotation.formats[0].TypefaceName

    def GetTextHeightAtIndex(self, index):
        return self.annotation.formats[0].CharHeight


class Sheet:
    def __init__(self, name="Sheet1", table=None):
        self.name, self.RevisionTable = name, table
        self.insert_count = 0

    def GetName(self):
        return self.name

    def GetTemplateName(self):
        return r"C:\formats\a3.slddrt"

    def GetProperties2(self):
        return [8, 12, 1, 1, True, .42, .297]

    def InsertRevisionTable2(self, anchored, x, y, anchor_type, template, shape, zoom):
        self.insert_count += 1
        self.RevisionTable = Table([])
        return self.RevisionTable


class Drawing:
    def __init__(self, path, notes=(), table=None):
        self.path = path
        self.notes = list(notes)
        self.sheet = Sheet(table=table)
        self.insert_count = 0
        self.edits = []

    def GetPathName(self):
        return self.path

    def GetType(self):
        return 3

    def GetCurrentSheet(self):
        return self.sheet

    def GetSheetNames(self):
        return [self.sheet.name]

    def Sheet(self, name):
        assert name == self.sheet.name
        return self.sheet

    def GetViews(self):
        return [[SimpleNamespace(GetName2=self.sheet.name, GetNotes=lambda: self.notes)]]

    def InsertNote(self, text):
        self.insert_count += 1
        note = Note("NewNote", text)
        note.annotation.Owner = self.sheet
        self.notes.append(note)
        return note

    def ClearSelection2(self, val):
        pass

    def EditTemplate(self):
        self.edits.append("format")

    def GetEditSheet(self):
        return not self.edits or self.edits[-1] == 'sheet'

    def EditSheet(self):
        self.edits.append("sheet")


class Table:
    def __init__(self, revisions=("A", "B")):
        self.annotation = Annotation("RevisionTable1")
        self.revisions = list(revisions)
        self.cells = [["REV", "DESCRIPTION"]] + [[rev, f"Change {rev}"] for rev in revisions]
        self.types = [400, 403]
        self.props = ["", "Description"]
        self.Type = 3
        self.AnchorType, self.Anchored = 1, True
        self.BorderLineWeight, self.GridLineWeight = 1, 1
        self.BorderLineWeightCustom, self.GridLineWeightCustom = .0002, .0001
        self.Title, self.TitleVisible = "REVISIONS", True
        self.widths = [.02, .08]
        self.heights = [.005] * len(self.cells)
        self._oleobj_ = self

    @property
    def RowCount(self):
        return len(self.cells)

    @property
    def ColumnCount(self):
        return 2

    @property
    def CurrentRevision(self):
        return self.revisions[-1] if self.revisions else ""

    def GetAnnotation(self):
        return self.annotation

    def Text(self, row, column):
        return self.cells[row][column]

    def GetIDsOfNames(self, name):
        assert name == "Text"
        return 42

    def Invoke(self, dispid, lcid, flags, result_wanted, row, column, *text):
        if flags == 2:
            return self.cells[row][column]
        assert flags == 4
        self.cells[row][column] = text[0]

    def GetCellTextFormat(self, row, column):
        return self.annotation.formats[0]

    def SetCellTextFormat(self, row, column, use_doc, fmt):
        return True

    def GetIdForRowNumber(self, row):
        return row if row else -1

    def GetRevisionForId(self, revision_id):
        return self.revisions[revision_id - 1]

    def GetColumnType2(self, col, hidden):
        return self.types[col]

    def GetColumnCustomProperty(self, col):
        return self.props[col]

    def SetColumnCustomProperty(self, col, prop):
        self.props[col] = prop
        return True

    def GetColumnWidth(self, col):
        return self.widths[col]

    def SetColumnWidth(self, col, width, behavior):
        self.widths[col] = width
        return width

    def GetRowHeight(self, row):
        return self.heights[row]

    def SetRowHeight(self, row, height, behavior):
        self.heights[row] = height
        return height

    def GetHeaderStyle(self):
        return 1

    def GetHeaderCount(self):
        return 1

    def SaveAsTemplate(self, path):
        Path(path).write_text("native template placeholder", encoding="utf8")
        return True

    def AddRevision(self, revision):
        self.revisions.append(revision)
        self.cells.append([revision, ""])
        self.heights.append(.005)
        return len(self.revisions)


class DrawingAssetsTests(unittest.TestCase):
    def test_native_dispatch_in_nested_views_and_indexed_sheet_is_usable(self):
        from win32com.server.util import wrap
        class SheetView:
            _public_methods_ = ['GetName', 'GetName2', 'GetNotes']
            def GetName(self):
                return 'Sheet1'
            def GetName2(self):
                return 'Sheet1'
            def GetNotes(self):
                return []
        raw = wrap(SheetView())
        native_doc = SimpleNamespace(_oleobj_=SimpleNamespace(
            GetIDsOfNames=lambda name: 1, Invoke=lambda *args: raw))
        sheet = assets._indexed_property(native_doc, 'Sheet', 'Sheet1')
        self.assertEqual(assets.value(sheet, 'GetName'), 'Sheet1')
        nested_doc = SimpleNamespace(GetViews=lambda: ((raw,),))
        self.assertEqual(assets._notes(nested_doc), [])

    def run_tool(self, fn, target, source=None, **args):
        app = SimpleNamespace(GetDocuments=[target, source] if source else [target])
        with patch.object(assets, "require_document_path", return_value=(app, target)):
            return fn({"expected_document_path": target.path, **args})

    def test_copy_linked_note_twice_preserves_raw_link_without_duplicate(self):
        source_note = Note("Title", '$PRP:"Title"')
        source_note.annotation.formats[0].Bold = True
        source = Drawing(r"C:\source.slddrw", [source_note])
        target = Drawing(r"C:\target.slddrw")
        kwargs = dict(source_document_path=source.path, source_note_name="Title", target_x_mm=20, target_y_mm=30)
        first = self.run_tool(assets.copy_drawing_note, target, source, **kwargs)
        second = self.run_tool(assets.copy_drawing_note, target, source, **kwargs)
        self.assertTrue(first["ok"], first)
        self.assertFalse(second["data"]["changed"])
        self.assertEqual(target.insert_count, 1)
        self.assertEqual(target.notes[0].PropertyLinkedText, '$PRP:"Title"')
        self.assertTrue(target.notes[0].annotation.formats[0].Bold)
        self.assertEqual(source_note.GetText(), "Resolved title")

    def test_conflicting_note_is_not_overwritten_without_replace(self):
        source = Drawing(r"C:\source.slddrw", [Note("Title", "source")])
        target = Drawing(r"C:\target.slddrw", [Note("Title", "target")])
        out = self.run_tool(assets.copy_drawing_note, target, source, source_document_path=source.path,
                            source_note_name="Title", target_x_mm=20, target_y_mm=30)
        self.assertFalse(out["ok"])
        self.assertIn("raw_text", out["data"]["mismatch_fields"])
        self.assertEqual(target.notes[0].PropertyLinkedText, "target")
        self.assertEqual(target.insert_count, 0)

    def test_copy_does_not_inspect_unrelated_note_content(self):
        unrelated = SimpleNamespace(GetAnnotation=lambda: SimpleNamespace(GetName=lambda: 'Other'))
        source = Drawing(r"C:\source.slddrw", [Note('NOTE', 'source'), unrelated])
        target = Drawing(r"C:\target.slddrw")
        out = self.run_tool(assets.copy_drawing_note, target, source, source_document_path=source.path,
                            source_note_name='NOTE', target_x_mm=20, target_y_mm=30)
        self.assertTrue(out['ok'], out)

    def test_copy_sheet_note_restores_template_mode_and_binds_sheet_owner(self):
        source = Drawing(r"C:\source.slddrw", [Note("NOTE", "source")])
        target = Drawing(r"C:\target.slddrw")
        target.EditTemplate()
        out = self.run_tool(assets.copy_drawing_note, target, source, source_document_path=source.path,
                            source_note_name="NOTE", target_note_name="COPIED_NOTE", target_x_mm=20, target_y_mm=30)
        self.assertTrue(out["ok"], out)
        self.assertIs(target.notes[0].annotation.Owner, target.sheet)
        self.assertEqual(target.notes[0].annotation.OwnerType, 1)
        self.assertEqual(target.edits, ["format", "sheet", "format"])
        self.assertEqual(target.notes[0].annotation.name, "COPIED_NOTE")

    def test_view_owned_source_is_rejected_before_inserting(self):
        source = Drawing(r"C:\source.slddrw", [Note("NOTE", "source", owner=0)])
        target = Drawing(r"C:\target.slddrw")
        out = self.run_tool(assets.copy_drawing_note, target, source, source_document_path=source.path,
                            source_note_name="NOTE", target_x_mm=20, target_y_mm=30)
        self.assertFalse(out["ok"])
        self.assertEqual(target.insert_count, 0)
        self.assertEqual(target.edits, [])

    def test_title_fields_preflight_all_names_before_any_mutation(self):
        target = Drawing(r"C:\target.slddrw", [Note("DrawingNumber", "original", owner=2)])
        out = self.run_tool(assets.set_title_block_fields, target,
                            fields={"DrawingNumber": "new", "Missing": "bad"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["invalid_fields"], {"Missing": 0})
        self.assertEqual(target.notes[0].PropertyLinkedText, "original")
        self.assertEqual(target.edits, [])

    def test_blank_title_field_clears_and_preserves_format(self):
        target = Drawing(r"C:\target.slddrw", [Note("DrawingNumber", "original", owner=2)])
        target.notes[0].annotation.formats[0].Bold = True
        out = self.run_tool(assets.set_title_block_fields, target, fields={"DrawingNumber": ""})
        self.assertTrue(out["ok"], out)
        self.assertEqual(target.notes[0].PropertyLinkedText, "")
        self.assertTrue(target.notes[0].annotation.formats[0].Bold)
        self.assertEqual(target.edits, ["format", "sheet"])

    def test_copy_revision_table_preserves_semantic_metadata_and_reuses(self):
        source = Drawing(r"C:\source.slddrw", table=Table())
        source.sheet.RevisionTable.widths[1] = .12
        source.sheet.RevisionTable.heights[2] = .008
        target = Drawing(r"C:\target.slddrw")
        kwargs = dict(source_document_path=source.path)
        first = self.run_tool(assets.copy_revision_table, target, source, **kwargs)
        second = self.run_tool(assets.copy_revision_table, target, source, **kwargs)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["data"]["changed"])
        self.assertEqual(target.sheet.insert_count, 1)
        snapshot = first["data"]["table"]
        self.assertEqual(snapshot["column_types"], [400, 403])
        self.assertEqual(snapshot["column_custom_properties"], ["", "Description"])
        self.assertEqual(snapshot["current_revision"], "B")
        self.assertEqual(snapshot["cells"], source.sheet.RevisionTable.cells)
        self.assertEqual(snapshot["column_widths_m"], [.02, .12])
        self.assertEqual(snapshot["row_heights_m"], [.005, .005, .008])

    def test_same_cells_with_wrong_column_binding_is_rejected(self):
        source = Drawing(r"C:\source.slddrw", table=Table())
        target = Drawing(r"C:\target.slddrw", table=Table())
        target.sheet.RevisionTable.props[1] = "Author"
        out = self.run_tool(assets.copy_revision_table, target, source, source_document_path=source.path)
        self.assertFalse(out["ok"])
        self.assertIn("column_custom_properties[1]", out["data"]["mismatch_fields"])
        self.assertEqual(target.sheet.insert_count, 0)

    def test_template_semantic_mismatch_is_reported_after_copy(self):
        source = Drawing(r"C:\source.slddrw", table=Table())
        source.sheet.RevisionTable.types[0] = 499
        target = Drawing(r"C:\target.slddrw")
        out = self.run_tool(assets.copy_revision_table, target, source, source_document_path=source.path)
        self.assertFalse(out["ok"])
        self.assertIn("column_types[0]", out["data"]["mismatch_fields"])
        self.assertEqual(out["data"]["partial_artifacts"][0]["kind"], "created_revision_table")

    def test_failed_source_template_does_not_delete_existing_target(self):
        source = Drawing(r"C:\source.slddrw", table=Table())
        target = Drawing(r"C:\target.slddrw", table=Table())
        original = target.sheet.RevisionTable
        original.cells[1][1] = "Existing target entry"
        with patch.object(source.sheet.RevisionTable, "SaveAsTemplate", return_value=False), \
             patch.object(assets, "_checked_select_delete") as deletion:
            out = self.run_tool(assets.copy_revision_table, target, source,
                                source_document_path=source.path, replace_existing=True)
        self.assertFalse(out["ok"])
        deletion.assert_not_called()
        self.assertIs(target.sheet.RevisionTable, original)
        self.assertEqual(out["data"]["partial_artifacts"], [])

    def test_inspection_includes_exact_note_names_and_sheet_format(self):
        target = Drawing(r"C:\target.slddrw", [Note("Author_Exact", "", owner=2)])
        out = self.run_tool(assets.inspect_drawing_assets, target)
        self.assertEqual(out["data"]["title_fields"][0]["name"], "Author_Exact")
        self.assertEqual(out["data"]["sheets"][0]["sheet_format_path"], r"C:\formats\a3.slddrt")


if __name__ == "__main__":
    unittest.main()
