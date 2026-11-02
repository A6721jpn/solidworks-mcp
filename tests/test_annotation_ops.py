"""Atomic annotation operations without a CAD connection."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_annotation_ops as ops
from tests.test_drawing_assets import Drawing, Note, Table


class RevisionTable(Table):
    def __init__(self):
        super().__init__()
        self.types = [302, 303]
        self.props = ['', '']
        self.writes = []

    def GetCellUseDocTextFormat(self, row, column):
        return False

    def Invoke(self, dispid, lcid, flags, result_wanted, row, column, *text):
        if flags == 4:
            self.writes.append((row, column, text[0]))
        return super().Invoke(dispid, lcid, flags, result_wanted, row, column, *text)


class AnnotationOperationTests(unittest.TestCase):
    def setUp(self):
        self.note = Note('NOTE', '<FONT style=B>Heading\r\n$PRP:"Title"', owner=2)
        self.note.HasMultipleFonts = lambda: True
        self.table = RevisionTable()
        self.doc = Drawing(r'C:\target.slddrw', [self.note], self.table)

    def note_args(self, **updates):
        args = dict(expected_document_path=self.doc.path, sheet_name='Sheet1', note_name='NOTE',
                    expected_raw_text=self.note.PropertyLinkedText,
                    raw_text='<FONT style=B>Heading\r\n$PRP:"Title"\r\nNew line')
        return {**args, **updates}

    def cell_args(self, **updates):
        args = dict(expected_document_path=self.doc.path, sheet_name='Sheet1', table_name='RevisionTable1',
                    revision_id=2, expected_revision='B', column=1, expected_column_type=303,
                    expected_text='Change B', text='New description')
        return {**args, **updates}

    def invoke(self, fn, args, rebuilt=None):
        with patch.object(ops, 'require_document_path', return_value=(object(), self.doc)), \
             patch.object(ops, 'rebuild', side_effect=rebuilt or (lambda doc: True)):
            return fn(args)

    def test_formatted_note_is_reacquired_and_template_mode_restored(self):
        original = self.note
        requested = self.note_args()['raw_text']
        def rebuild(doc):
            fresh = deepcopy(original)
            fresh.PropertyLinkedText = requested
            doc.notes = [fresh]
            original.GetText = lambda: 'stale old object'
            return True
        out = self.invoke(ops.set_drawing_note_text, self.note_args(), rebuild)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['data']['note']['raw_text'], requested)
        self.assertIn('Resolved title', out['data']['note']['text'])
        self.assertEqual(self.doc.edits, ['format', 'sheet'])
        self.assertTrue(self.doc.GetEditSheet())
        self.assertEqual(self.doc.notes[0].annotation.position, [.02, .03, 0])

    def test_stale_note_and_wrong_sheet_do_not_write_or_change_mode(self):
        before = self.note.PropertyLinkedText
        for args in [self.note_args(expected_raw_text='stale'), self.note_args(sheet_name='Other')]:
            out = self.invoke(ops.set_drawing_note_text, args)
            self.assertFalse(out['ok'])
            self.assertEqual(self.note.PropertyLinkedText, before)
            self.assertEqual(self.doc.edits, [])

    def test_note_restoration_failure_is_not_reported_as_success(self):
        with patch.object(self.doc, 'EditSheet', return_value=False):
            out = self.invoke(ops.set_drawing_note_text, self.note_args())
        self.assertFalse(out['ok'])
        self.assertTrue(out['data']['may_have_changes'])
        self.assertEqual(out['data']['partial_artifacts'][0]['name'], 'NOTE')
        self.assertIn('Restoring', out['message'])

    def test_revision_cell_uses_native_id_after_row_reordering(self):
        self.table.cells[1], self.table.cells[2] = self.table.cells[2], self.table.cells[1]
        self.table.GetIdForRowNumber = lambda row: {0: -1, 1: 2, 2: 1}[row]
        before = ops.assets._table_asset(self.table).snapshot
        self.doc.EditTemplate()
        out = self.invoke(ops.set_revision_table_cell, self.cell_args())
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.table.writes, [(1, 1, 'New description')])
        self.assertFalse(self.doc.GetEditSheet())
        self.assertEqual(out['data']['table']['revision_rows'], before['revision_rows'])
        self.assertEqual(out['data']['table']['column_types'], before['column_types'])
        self.assertEqual(out['data']['table']['fonts'], before['fonts'])

    def test_revision_identity_type_binding_and_old_text_are_preflighted(self):
        cases = [self.cell_args(expected_revision='A'), self.cell_args(expected_column_type=304),
                 self.cell_args(column=0, expected_column_type=302, expected_text='B'),
                 self.cell_args(expected_text='stale')]
        for args in cases:
            out = self.invoke(ops.set_revision_table_cell, args)
            self.assertFalse(out['ok'], out)
        self.table.props[1] = 'Description'
        self.assertFalse(self.invoke(ops.set_revision_table_cell, self.cell_args())['ok'])
        self.assertEqual(self.table.writes, [])

    def test_revision_metadata_side_effect_is_a_partial_failure(self):
        def rebuild(doc):
            self.table.widths[1] = .4
            return True
        out = self.invoke(ops.set_revision_table_cell, self.cell_args(), rebuild)
        self.assertFalse(out['ok'])
        self.assertIn('column_widths_m[1]', out['data']['mismatch_fields'])
        self.assertTrue(out['data']['may_have_changes'])
        self.assertFalse(out['data']['rollback_performed'])

    def test_already_desired_values_are_reused_without_writes(self):
        note = self.invoke(ops.set_drawing_note_text,
                           self.note_args(raw_text=self.note.PropertyLinkedText, expected_raw_text='old'))
        cell = self.invoke(ops.set_revision_table_cell,
                           self.cell_args(text='Change B', expected_text='old'))
        self.assertTrue(note['ok'], note)
        self.assertTrue(cell['ok'], cell)
        self.assertFalse(note['data']['changed'])
        self.assertFalse(cell['data']['changed'])
        self.assertEqual(self.table.writes, [])


if __name__ == '__main__':
    unittest.main()
