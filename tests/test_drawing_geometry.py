from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing_geometry as g
from solidworks_mcp import sw_drawing


class NativeDimension:
    def __init__(self, view, number, measured_mm):
        self.view = view
        self.Name = f'RD{number}'
        self.SystemValue = measured_mm / 1000

    @property
    def FullName(self):
        return f'{self.Name}@{self.view.name}@Drawing'


class NativeDisplayDimension:
    def __init__(self, view, number, measured_mm):
        self.param = NativeDimension(view, number, measured_mm)
        self.prefix = ''

    def GetDimension2(self, index):
        return self.param

    def GetText(self, part):
        return self.prefix

    def SetText(self, part, text):
        self.prefix = text

    def SetPrecision3(self, *args):
        return 0

    def GetAnnotation(self):
        return SimpleNamespace(IsDangling=False)


@contextmanager
def dimension_fixture(measured_values):
    selector = {'kind': 'line', 'start_mm': [0, 0, 0], 'end_mm': [5, 0, 0]}
    views = {}
    values = iter(measured_values)
    doc = SimpleNamespace(ClearSelection2=Mock(), created=0, selected_view=None)

    def create(*args):
        doc.created += 1
        view = doc.selected_view
        display = NativeDisplayDimension(view, doc.created, next(values))
        view.items.append(display)
        return display

    def find(doc, name):
        if name not in views:
            view = SimpleNamespace(name=name, ReferencedConfiguration='DRAWING', items=[])

            def select(*args):
                doc.selected_view = view
                return True

            view.SelectEntity = select
            views[name] = view
        return views[name]

    def remove(doc, display):
        display.param.view.items.remove(display)
        return True

    doc.AddHorizontalDimension2 = create
    with ExitStack() as stack:
        stack.enter_context(patch.object(g, 'drawing', return_value=(object(), doc)))
        guard = stack.enter_context(patch.object(g, 'require_document_path', return_value=(object(), doc)))
        stack.enter_context(patch.object(g, 'find_view', side_effect=find))
        stack.enter_context(patch.object(g, 'dimensions', side_effect=lambda v: list(v.items)))
        geometry = stack.enter_context(patch.object(g, 'catalog', return_value=[(selector, object())]))
        rebuild = stack.enter_context(patch.object(g, 'rebuild', return_value=True))
        stack.enter_context(patch.object(g, 'dimension_dialog_suppressed', return_value=nullcontext()))
        stack.enter_context(patch.object(g, '_delete_dimension', side_effect=remove))
        yield SimpleNamespace(doc=doc, views=views, selector=selector, catalog=geometry,
                              rebuild=rebuild, guard=guard)


def dimension_request(selector, name='Width', view='View', expected=5):
    return dict(view_name=view, configuration='DRAWING', name=name, kind='horizontal',
                entities=[selector], at_mm=[10, 10], expected_value_mm=expected)


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
             patch.object(g, 'require_document_path', return_value=(object(), doc)), \
             patch.object(g, 'find_view', return_value=view), patch.object(g, 'dimensions', return_value=[]), \
             patch.object(g, 'catalog', return_value=[(selector, object())]), \
             patch.object(g, 'dimension_dialog_suppressed', return_value=nullcontext()), \
             patch.object(g, 'rebuild', return_value=True), \
             patch.object(g, '_delete_dimension', return_value=True) as delete:
            out = g.add_drawing_dimension(args)
        self.assertFalse(out['ok'])
        self.assertEqual(out['data']['measured_mm'], 8)
        self.assertTrue(out['data']['removed'])
        delete.assert_called_once()

    def test_single_dimension_keeps_its_success_response(self):
        with dimension_fixture([5]) as fixture:
            out = g.add_drawing_dimension(dict(expected_document_path='drawing',
                                               **dimension_request(fixture.selector)))
        self.assertTrue(out['ok'])
        self.assertEqual(out['data'], {'dimension': 'Width@View@Drawing', 'measured_mm': 5, 'prefix': ''})
        fixture.rebuild.assert_called_once()

    def test_batch_creates_multiple_views_with_one_rebuild(self):
        with dimension_fixture([5, 6, 7]) as fixture:
            requests = [dimension_request(fixture.selector, 'First'),
                        dimension_request(fixture.selector, 'Second', expected=6),
                        dimension_request(fixture.selector, 'Third', view='Other', expected=7)]
            out = g.add_drawing_dimension(dict(expected_document_path='drawing', dimensions=requests))
        self.assertTrue(out['ok'])
        self.assertEqual(out['data']['count'], 3)
        self.assertEqual([d['measured_mm'] for d in out['data']['dimensions']], [5, 6, 7])
        self.assertTrue(all(d['status'] == 'verified' for d in out['data']['dimensions']))
        self.assertEqual(fixture.catalog.call_count, 2)
        fixture.rebuild.assert_called_once()

    def test_later_invalid_geometry_refuses_entire_batch_before_changes(self):
        with dimension_fixture([5]) as fixture:
            requests = [dimension_request(fixture.selector, 'First'),
                        dimension_request({'kind': 'line'}, 'Second')]
            out = g.add_drawing_dimension(dict(expected_document_path='drawing', dimensions=requests))
        self.assertFalse(out['ok'])
        self.assertFalse(out['data']['changed'])
        self.assertEqual(fixture.doc.created, 0)
        fixture.rebuild.assert_not_called()

    def test_duplicate_names_refuse_batch_and_existing_single_dimension(self):
        with dimension_fixture([5]) as fixture:
            request = dimension_request(fixture.selector)
            out = g.add_drawing_dimension(dict(expected_document_path='drawing', dimensions=[request, request]))
            self.assertFalse(out['ok'])
            self.assertTrue(out['data']['conflict'])
            self.assertEqual(fixture.doc.created, 0)
            single = dict(expected_document_path='drawing', **request)
            self.assertTrue(g.add_drawing_dimension(single)['ok'])
            self.assertFalse(g.add_drawing_dimension(single)['ok'])
            self.assertEqual(fixture.doc.created, 1)

    def test_bad_later_value_removes_only_that_dimension_and_reports_partial(self):
        with dimension_fixture([5, 8]) as fixture:
            requests = [dimension_request(fixture.selector, 'First'),
                        dimension_request(fixture.selector, 'Second', expected=7)]
            out = g.add_drawing_dimension(dict(expected_document_path='drawing', dimensions=requests))
        self.assertFalse(out['ok'])
        self.assertTrue(out['data']['partial'])
        self.assertEqual(out['data']['failed']['index'], 1)
        self.assertTrue(out['data']['failed']['removed'])
        self.assertEqual([d['name'] for d in out['data']['created']], ['First'])
        self.assertEqual([d.param.Name for d in fixture.views['View'].items], ['First'])
        self.assertEqual(out['data']['created'][0]['status'], 'verified')
        fixture.rebuild.assert_called_once()

    def test_rebuild_failure_never_reports_batch_success(self):
        with dimension_fixture([5]) as fixture:
            fixture.rebuild.return_value = False
            out = g.add_drawing_dimension(dict(expected_document_path='drawing',
                                               dimensions=[dimension_request(fixture.selector)]))
        self.assertFalse(out['ok'])
        self.assertFalse(out['data']['rebuild_ok'])
        self.assertTrue(out['data']['may_have_changes'])
        self.assertEqual(out['data']['created'][0]['status'], 'applied')

    def test_document_switch_stops_before_next_write(self):
        with dimension_fixture([5, 6]) as fixture:
            fixture.guard.side_effect = [(object(), fixture.doc), RuntimeError('Active document mismatch'),
                                         RuntimeError('Active document mismatch')]
            requests = [dimension_request(fixture.selector, 'First'),
                        dimension_request(fixture.selector, 'Second', expected=6)]
            out = g.add_drawing_dimension(dict(expected_document_path='drawing', dimensions=requests))
        self.assertFalse(out['ok'])
        self.assertEqual(out['data']['failed']['index'], 1)
        self.assertEqual(fixture.doc.created, 1)
        fixture.rebuild.assert_not_called()

    def test_batch_rejects_empty_and_mixed_input_modes(self):
        with dimension_fixture([]) as fixture:
            for fields in [{'dimensions': []},
                           {'dimensions': [dimension_request(fixture.selector)], 'name': 'Mixed'}]:
                with self.subTest(fields=fields):
                    out = g.add_drawing_dimension(dict(expected_document_path='drawing', **fields))
                    self.assertFalse(out['ok'])
                    self.assertFalse(out['data']['changed'])
            self.assertEqual(fixture.doc.created, 0)

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
