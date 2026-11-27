from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_drawing_geometry as drawing
from solidworks_mcp import sw_sketch_edit as ops


class DrawingSketchInspectionTests(unittest.TestCase):
    def setUp(self):
        self.sheet = NS(GetConstrainedStatus=lambda: 3)
        self.sketch = NS(Name='Sketch70', GetConstrainedStatus=lambda: 2)
        self.origin_sketch = NS(Name='Origin')
        self.center = self.line('Line14', (9, 14), 0.0)
        self.line11 = self.line('Line11', (6, 11), -.005)
        self.origin = NS(GetID=lambda: (0, 1), GetSketch=lambda: self.origin_sketch, X=0., Y=0., Z=0.)
        self.sketch.GetSketchSegments = lambda: [self.center, self.line11]
        self.relation = NS(GetRelationType=lambda: 9, GetDefinitionEntities=lambda: [self.origin, self.center])
        self.sketch.RelationManager = NS(GetRelations=lambda _: [self.relation])
        self.dimension = NS(FullName='D2@Sketch70@Test.Drawing', GetType=lambda: 0, DrivenState=2, SystemValue=.005)
        self.annotation = NS(GetAttachedEntities3=lambda: [self.line('Line14', (9, 14), 0.), self.line11])
        self.display = NS(GetDimension2=lambda _: self.dimension, GetAnnotation=lambda: self.annotation, GetNext=lambda: None)
        self.view = NS(GetSketch=lambda: self.sketch, ReferencedConfiguration='Drawing',
                       GetReferencedModelName=lambda: 'C:/copy.SLDPRT', GetFirstDisplayDimension=lambda: self.display)
        self.doc = NS(GetType=lambda: 3, GetPathName=lambda: 'C:/copy.SLDDRW', GetSaveFlag=lambda: True,
                      SketchManager=NS(ActiveSketch=self.sheet), SelectionManager=object())
        for member in ('ActivateView', 'ClearSelection2', 'EditRebuild3', 'Save3', 'Parameter'):
            setattr(self.doc, member, Mock(side_effect=AssertionError('Inspection must not call ' + member)))
        self.app = NS(IsSame=lambda a, b: int(a is b))
        self.args = dict(expected_document_path='C:/copy.SLDDRW', view_name='View1', include_segments=True)
        self.addCleanup(patch.stopall)
        patch.object(ops, 'require_document_path', return_value=(self.app, self.doc)).start()
        patch.object(drawing, 'find_view', return_value=self.view).start()
        patch.object(ops, 'flag_methods', side_effect=lambda obj, *members: obj).start()

    def line(self, name, identity, y):
        return NS(GetName=lambda: name, GetID=lambda: identity, GetType=lambda: 0,
                  GetSketch=lambda: self.sketch, ConstructionGeometry=name == 'Line14',
                  GetStartPoint2=lambda: NS(X=-.02, Y=y, Z=0.),
                  GetEndPoint2=lambda: NS(X=.03, Y=y, Z=0.))

    def test_inactive_view_returns_native_names_dimensions_and_external_origin_without_edits(self):
        before_selection = self.doc.SelectionManager
        out = ops.inspect_sketch(self.args)
        self.assertTrue(out['ok'], out)
        data = out['data']
        self.assertEqual(data['segments'][1]['name'], 'Line11')
        self.assertEqual(data['segments'][1]['id'], [6, 11])
        self.assertEqual(data['segments'][1]['start_mm'], [-20., -5., 0.])
        dimension = data['dimensions'][0]
        self.assertEqual(dimension['full_name'], 'D2@Sketch70@Test.Drawing')
        self.assertEqual(dimension['value_mm'], 5)
        self.assertTrue(dimension['driving'])
        self.assertEqual([row['id'] for row in dimension['attachments']], [[9, 14], [6, 11]])
        relation = data['relations'][0]
        self.assertEqual(relation['entities'][0]['scope'], 'external')
        self.assertEqual(relation['entities'][0]['sketch'], 'Origin')
        self.assertTrue(relation['identity_complete'])
        self.assertIsNone(relation['relation_ref'])
        self.assertFalse(data['target_is_active_sketch'])
        self.assertIs(self.doc.SketchManager.ActiveSketch, self.sheet)
        self.assertIs(self.doc.SelectionManager, before_selection)
        for member in ('ActivateView', 'ClearSelection2', 'EditRebuild3', 'Save3', 'Parameter'):
            getattr(self.doc, member).assert_not_called()

    def test_wrong_target_and_explicit_drawing_equations_are_rejected(self):
        for extra in ({'sketch_name': 'Other'}, {'include_equations': True}):
            with self.subTest(extra=extra):
                self.assertFalse(ops.inspect_sketch({**self.args, **extra})['ok'])
        self.doc.GetType = lambda: 1
        self.assertFalse(ops.inspect_sketch(self.args)['ok'])

    def test_failed_native_reads_are_not_reported_as_empty_success(self):
        def relation_failure():
            raise RuntimeError('native relation read failed')
        with patch.object(self.relation, 'GetDefinitionEntities', new=relation_failure):
            out = ops.inspect_sketch(self.args)
        self.assertFalse(out['ok'])
        self.assertIn('native relation read failed', out['message'])
        def dimension_failure():
            raise RuntimeError('dimension enumeration failed')
        with patch.object(self.display, 'GetNext', new=dimension_failure):
            out = ops.inspect_sketch(self.args)
        self.assertFalse(out['ok'])
        self.assertIn('dimension enumeration failed', out['message'])

    def test_unsupported_attachments_remain_explicit_and_other_model_dimensions_are_counted(self):
        self.annotation.GetAttachedEntities3 = lambda: [object()]
        other = NS(GetDimension2=lambda _: NS(FullName='D1@ModelSketch@Test.Drawing'), GetNext=lambda: None)
        self.display.GetNext = lambda: other
        out = ops.inspect_sketch(self.args)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['data']['other_view_dimension_count'], 1)
        self.assertFalse(out['data']['dimensions'][0]['attachments_complete'])
        self.assertEqual(out['data']['dimensions'][0]['attachments'][0]['type'], 'unsupported')


if __name__ == '__main__':
    unittest.main()
