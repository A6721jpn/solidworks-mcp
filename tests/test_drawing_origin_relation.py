from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_sketch as sketch
from solidworks_mcp import sw_drawing_geometry as drawing


class DrawingOriginRelationTests(unittest.TestCase):
    def test_definition_matches_only_the_view_origin_and_the_requested_line_owner(self):
        app = SimpleNamespace(IsSame=lambda a, b: int(a is b))
        owner, other_owner, origin, other_origin = object(), object(), object(), object()
        line = SimpleNamespace(GetID=(9, 14), GetSketch=owner)
        wrong_line = SimpleNamespace(GetID=(9, 14), GetSketch=other_owner)
        def relation(*entities):
            return SimpleNamespace(GetRelationType=9, GetDefinitionEntities=entities)
        good = relation(origin, line)
        manager = SimpleNamespace(GetRelations=lambda _: [good, relation(other_origin, line), relation(origin, wrong_line)])
        with patch.object(sketch, 'flag_methods', side_effect=lambda obj, *args: obj):
            self.assertEqual(sketch._origin_line_matches(app, manager, owner, [9, 14], origin), [good])

    def test_changed_endpoints_are_rejected_before_adding_a_relation(self):
        points = [SimpleNamespace(X=0., Y=0., Z=0.), SimpleNamespace(X=.010, Y=0., Z=0.)]
        line = SimpleNamespace(GetID=(9, 14), GetType=0, GetStartPoint2=points[0], GetEndPoint2=points[1])
        view_sketch = SimpleNamespace(GetSketchSegments=[line])
        model = SimpleNamespace(GetPathName='C:/copy.SLDPRT')
        view = SimpleNamespace(GetReferencedModelName='C:/copy.SLDPRT', ReferencedConfiguration='Default',
                               ReferencedDocument=model, GetSketch=view_sketch)
        doc = SimpleNamespace(SketchManager=SimpleNamespace(ActiveSketch=view_sketch),
                              ActivateView=lambda _: True, SketchAddConstraints=Mock())
        app = SimpleNamespace(IsSame=lambda a, b: int(a is b))
        target = dict(expected_document_path='C:/copy.SLDDRW', expected_model_path='C:/copy.SLDPRT',
                      expected_configuration='Default', view_name='View1', segment_id=[9, 14],
                      expected_endpoints_mm=[[0, 1, 0], [10, 1, 0]])
        args = dict(relation='coincident', selection=dict(sketch_segments=[0], drawing_model_origin=target))
        with patch.object(sketch, 'require_document_path', return_value=(app, doc)), \
             patch.object(drawing, 'find_view', return_value=view), \
             patch.object(sketch, 'flag_methods', side_effect=lambda obj, *args: obj):
            with self.assertRaisesRegex(RuntimeError, 'endpoints changed'):
                sketch.add_relation(args)
        doc.SketchAddConstraints.assert_not_called()


if __name__ == '__main__':
    unittest.main()
