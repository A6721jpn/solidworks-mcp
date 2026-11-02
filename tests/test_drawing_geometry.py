from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing_geometry as g
from solidworks_mcp import sw_drawing


class DrawingGeometryTests(unittest.TestCase):
    def test_geometric_selector_is_independent_of_edge_enumeration(self):
        wanted = {'kind': 'line', 'start_mm': [0, 0, 0], 'end_mm': [10, 0, 0]}
        other = {'kind': 'line', 'start_mm': [0, 1, 0], 'end_mm': [10, 1, 0]}
        selected, different = object(), object()
        for rows in [[(wanted, selected), (other, different)], [(other, different), (wanted, selected)]]:
            self.assertIs(g.resolve_entity(rows, wanted, .00001), selected)

    def test_overlapping_indistinguishable_geometry_refuses_selection(self):
        wanted = {'kind': 'circle', 'center_mm': [2, 3, 4], 'radius_mm': 1.35}
        with self.assertRaisesRegex(RuntimeError, 'matched 2'):
            g.resolve_entity([(wanted, object()), (wanted, object())], wanted, .00001)

    def test_rotated_translated_view_transform_uses_mm(self):
        transform = [0, -1, 0, 1, 0, 0, 0, 0, 1, .05, .18, 0, 4, 0, 0, 0]
        self.assertEqual(g.model_to_sheet([-6, 20, 0], transform), [130, 204, 0])

    def test_wrong_dimension_value_is_removed_and_reported(self):
        param = SimpleNamespace(SystemValue=.008, FullName='RD1@View@Drawing')
        dim = SimpleNamespace(GetDimension2=lambda i: param, _FlagAsMethod=lambda *a: None)
        doc = SimpleNamespace(AddHorizontalDimension2=lambda *a: dim, _FlagAsMethod=lambda *a: None,
                              ClearSelection2=lambda *a: None)
        view = SimpleNamespace(ReferencedConfiguration='DRAWING', SelectEntity=lambda *a: True)
        selector = {'kind': 'line', 'start_mm': [0, 0, 0], 'end_mm': [8, 0, 0]}
        args = dict(expected_document_path='drawing', view_name='View', configuration='DRAWING',
                    name='Width', kind='horizontal', entities=[selector], at_mm=[10, 10], expected_value_mm=7)
        with patch.object(g, 'drawing', return_value=(object(), doc)), \
             patch.object(g, 'find_view', return_value=view), patch.object(g, 'dimensions', return_value=[]), \
             patch.object(g, 'catalog', return_value=[(selector, object())]), \
             patch.object(g, 'dimension_dialog_suppressed', return_value=nullcontext()), \
             patch.object(g, '_delete_dimension', return_value=True) as delete:
            out = g.add_drawing_dimension(args)
        self.assertFalse(out['ok'])
        self.assertEqual(out['data']['measured_mm'], 8)
        self.assertTrue(out['data']['removed'])
        delete.assert_called_once()

    def test_geometry_fit_respects_reserved_region(self):
        self.assertTrue(g.box_in_region([5, 5, 95, 45], [0, 0, 100, 50], 3))
        self.assertFalse(g.box_in_region([2, 5, 95, 45], [0, 0, 100, 50], 3))
        self.assertFalse(g.box_in_region([5, 5, 105, 45], [0, 0, 100, 50], 3))

    def test_moving_one_coordinate_preserves_the_other(self):
        view = SimpleNamespace(GetName2='View', Position=[.1, .2])
        with patch.object(sw_drawing, 'require_drawing', return_value=(object(), object())), \
             patch.object(sw_drawing, '_iter_views', return_value=[view]), \
             patch.object(sw_drawing, '_apply_view_options'), patch.object(sw_drawing, 'rebuild'), \
             patch.object(sw_drawing, '_view_entry', return_value={}):
            sw_drawing.set_drawing_view({'name': 'View', 'x_mm': 50})
        self.assertEqual(list(view.Position.value), [.05, .2])


if __name__ == '__main__':
    unittest.main()
