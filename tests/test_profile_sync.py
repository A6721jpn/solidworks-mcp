"""Profile synchronization behavior without a running CAD session."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import _profile_sync as sync
from solidworks_mcp import sw_sketch


class Segment:
    def __init__(self, sketch, native_id, construction=False, kind=0):
        self.sketch = sketch
        self.native_id = native_id
        self.kind = kind
        self.ConstructionGeometry = construction
        self.GetSketch = lambda: self.sketch
        self.GetID = lambda: [self.native_id, self.native_id + 1]
        self.GetType = lambda: self.kind
        self.geometry_reads = 0
        self.GetLength = self.length
        point = lambda x, y: SimpleNamespace(X=x, Y=y, Z=0.)
        self.GetStartPoint2 = lambda: point(.001, .002)
        self.GetEndPoint2 = lambda: point(.002, .003)
        self.GetCenterPoint2 = lambda: point(.0015, .0025)
        self.GetMajorPoint2 = lambda: point(.003, .0025)
        self.GetMinorPoint2 = lambda: point(.0015, .003)
        self.GetRadius = lambda: .001

    def length(self):
        self.geometry_reads += 1
        return .01


class Sketch:
    def __init__(self, name):
        self.Name = name
        self.segments = []
        self.relations = []
        self.relation_reads = 0
        self.GetSketchSegments = lambda: list(self.segments)
        self.RelationManager = SimpleNamespace(GetRelations=self.read_relations)

    def read_relations(self, scope):
        self.relation_reads += 1
        return list(self.relations)


class Relation:
    def __init__(self, *entities):
        self.entities = entities
        self.GetRelationType = lambda: 32
        self.GetDefinitionEntities = lambda: self.entities

    def GetEntities(self):
        raise AssertionError("Phantom GetEntities must never establish projection provenance.")


class NativeModel:
    def __init__(self):
        self.source, self.target = Sketch("MASTER"), Sketch("UPPER")
        self.source.segments = [Segment(self.source, 10), Segment(self.source, 20),
                                Segment(self.source, 30, construction=True, kind=2)]
        self.target.segments = [Segment(self.target, 100, construction=True),
                                Segment(self.target, 110), Segment(self.target, 120, kind=2)]
        self.target.relations = [Relation(self.source.segments[0], self.target.segments[0]),
                                 Relation(self.source.segments[1], self.target.segments[1])]
        self.boundary = "LATER"
        self.selection = []
        self.calls = []
        self.convert_fails = False
        self.group_succeeds = True
        self.undo_fails = False
        self.rollback_change = None
        self.saved = None
        self.features = []
        for index, name in enumerate(("MASTER", "UPPER", "MID", "LATER")):
            sketch = self.source if index == 0 else self.target if index == 1 else None
            feature = SimpleNamespace(Name=name, GetTypeName2=lambda: "ProfileFeature",
                                      GetSpecificFeature2=lambda sketch=sketch: sketch,
                                      IsSuppressed=False, GetErrorCode=0)
            feature.GetNextFeature = lambda index=index: self.features[index + 1] if index + 1 < 4 else None
            feature.IsRolledBack = lambda index=index: (self.boundary is not None and
                                                       index >= [f.Name for f in self.features].index(self.boundary))
            self.features.append(feature)
        self.manager = SimpleNamespace(ActiveSketch=None, InsertSketch=self.edit,
                                       SketchUseEdge3=self.convert)
        self.extension = SimpleNamespace(StartRecordingUndoObject=self.start,
                                         FinishRecordingUndoObject2=self.finish)
        self.doc = SimpleNamespace(SketchManager=self.manager, Extension=self.extension,
                                   FeatureByName=lambda name: next((f for f in self.features if f.Name == name), None),
                                   FirstFeature=lambda: self.features[0],
                                   ClearSelection2=lambda flag: self.selection.clear(),
                                   FeatureManager=SimpleNamespace(EditRollback=self.rollback),
                                   ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="FLAT")),
                                   EditUndo2=self.undo)
        self.app = SimpleNamespace(IsSame=lambda first, second: int(first is second))

    def edit(self, flag):
        self.calls.append("edit")
        self.manager.ActiveSketch = self.target if self.manager.ActiveSketch is None else None

    def rollback(self, operation, name):
        self.calls.append(("rollback", operation, name))
        self.boundary = name if operation == 3 else None
        if self.rollback_change:
            self.rollback_change(self.boundary)
        return True

    def select(self, doc, obj, mark=0, append=True):
        if not append:
            self.selection.clear()
        self.selection.append(obj)
        return True

    def start(self):
        self.calls.append("start")
        self.saved = (list(self.target.segments), list(self.target.relations),
                      [s.ConstructionGeometry for s in self.target.segments])

    def finish(self, name, merge):
        self.calls.append("finish")
        return self.group_succeeds

    def undo(self, count):
        self.calls.append("undo")
        if self.undo_fails:
            raise RuntimeError("Native undo failed.")
        segments, relations, flags = self.saved
        self.target.segments, self.target.relations = segments, relations
        for segment, flag in zip(segments, flags):
            segment.ConstructionGeometry = flag

    def convert(self, chain, inner_loops):
        assert chain is False and inner_loops is False
        self.calls.append("convert")
        for source in self.selection:
            segment = Segment(self.target, 200 + len(self.target.segments), kind=source.kind)
            self.target.segments.append(segment)
            self.target.relations.append(Relation(source, segment))
        return not self.convert_fails

    def rebuild(self, doc):
        self.calls.append("rebuild")
        return True


class ProfileSyncTests(unittest.TestCase):
    def setUp(self):
        self.model = NativeModel()
        self.args = {"selection": {"sketch_name": "MASTER", "sketch_segments": [0, 2]},
                     "chain": False, "inner_loops": False,
                     "sync": {"expected_document_path": "C:/copy.SLDPRT", "expected_configuration": "FLAT",
                              "target_sketch": "UPPER", "protected_target_segments": [{"type": 2, "id": [120, 121]}],
                              "rollback_before": "MID"}}
        patches = [patch.object(sync, "require_document_path", return_value=(self.model.app, self.model.doc)),
                   patch.object(sync, "flag_methods", side_effect=lambda obj, *names: obj),
                   patch.object(sync, "sketch_manager", return_value=self.model.manager),
                   patch.object(sync, "select_object", side_effect=self.model.select),
                   patch.object(sync, "rebuild", side_effect=self.model.rebuild)]
        for patched in patches:
            patched.start()
            self.addCleanup(patched.stop)

    def test_plan_apply_and_repeat_preserve_native_objects_and_rollback(self):
        original = list(self.model.target.segments)
        planned = sw_sketch.convert_entities(self.args)
        self.assertTrue(planned["ok"], planned)
        self.assertEqual((planned["data"]["plan"]["add"], planned["data"]["plan"]["flag_change"]), (1, 2))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(sum(s.geometry_reads for s in self.model.source.segments + self.model.target.segments), 0)
        self.assertEqual(self.model.source.relation_reads, 0)
        self.args["sync"]["apply"] = True
        applied = sw_sketch.convert_entities(self.args)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.model.target.segments[:3], original)
        self.assertEqual([s.ConstructionGeometry for s in self.model.target.segments], [False, True, False, True])
        self.assertEqual(self.model.boundary, "LATER")
        self.assertIsNone(self.model.manager.ActiveSketch)
        self.assertEqual(self.model.selection, [])
        self.assertEqual(self.model.calls.count("convert"), 1)
        self.model.target.segments = [self.model.target.segments[-1]] + self.model.target.segments[:-1]
        protected_segment = original[2]
        calls = list(self.model.calls)
        geometry_reads = [s.geometry_reads for s in self.model.source.segments + self.model.target.segments]
        source_relation_reads = self.model.source.relation_reads
        repeated = sw_sketch.convert_entities(self.args)
        self.assertTrue(repeated["ok"], repeated)
        self.assertTrue(repeated["data"]["noop"])
        self.assertEqual(self.model.calls, calls)
        self.assertEqual([s.geometry_reads for s in self.model.source.segments + self.model.target.segments], geometry_reads)
        self.assertEqual(self.model.source.relation_reads, source_relation_reads)
        self.assertFalse(protected_segment.ConstructionGeometry)
        self.assertEqual(repeated["data"]["plan"]["protected_target_ids"], [{"type": 2, "id": [120, 121]}])

    def test_explicit_scope_and_closed_configuration_guards_write_nothing(self):
        variants = []
        missing = deepcopy(self.args)
        del missing["sync"]["protected_target_segments"]
        variants.append(missing)
        wrong_config = deepcopy(self.args)
        wrong_config["sync"]["expected_configuration"] = "CURVED"
        variants.append(wrong_config)
        chain = deepcopy(self.args)
        del chain["chain"]
        variants.append(chain)
        for protected in ([2], [{"type": 2, "id": [120, 121]}] * 2,
                          [{"type": 2, "id": [999, 1000]}]):
            invalid = deepcopy(self.args)
            invalid["sync"]["protected_target_segments"] = protected
            variants.append(invalid)
        for args in variants:
            with self.subTest(args=args):
                output = sw_sketch.convert_entities(args)
                self.assertFalse(output["ok"], output)
                self.assertFalse(output["data"]["may_have_changes"])
        self.model.manager.ActiveSketch = self.model.source
        self.assertFalse(sw_sketch.convert_entities(self.args)["ok"])
        self.assertEqual(self.model.calls, [])
        self.model.manager.ActiveSketch = None
        original_relation = self.model.target.relations[0]
        temporary_relation = Relation(self.model.source.segments[2], self.model.target.segments[0])
        drift_cases = {
            "identity": lambda boundary: setattr(self.model.target.segments[0], "native_id", 999 if boundary == "MID" else 100),
            "flag": lambda boundary: setattr(self.model.target.segments[0], "ConstructionGeometry", boundary != "MID"),
            "target relation": lambda boundary: self.model.target.relations.__setitem__(0, temporary_relation if boundary == "MID" else original_relation),
        }
        for name, change in drift_cases.items():
            with self.subTest(rollback_drift=name):
                self.model.calls.clear()
                self.model.rollback_change = change
                args = deepcopy(self.args)
                args["sync"]["apply"] = True
                output = sw_sketch.convert_entities(args)
                self.assertFalse(output["ok"], output)
                self.assertIn("changed after rollback", output["message"])
                self.assertTrue(output["data"]["restored"], output)
                self.assertFalse(output["data"]["may_have_changes"], output)
                self.assertNotIn("edit", self.model.calls)
                self.assertNotIn("convert", self.model.calls)
                self.assertNotIn("undo", self.model.calls)
        self.model.rollback_change = None

    def test_ambiguous_provenance_and_protected_conflict_refuse_without_writes(self):
        self.model.target.relations[0] = Relation(self.model.source.segments[0],
                                                self.model.source.segments[1], self.model.target.segments[0])
        output = sw_sketch.convert_entities(self.args)
        self.assertFalse(output["ok"], output)
        self.assertIn("ambiguous", output["message"])
        self.model.target.relations[0] = Relation(self.model.source.segments[0], self.model.target.segments[0])
        self.args["sync"]["protected_target_segments"] = [{"type": 0, "id": [100, 101]},
                                                           {"type": 2, "id": [120, 121]}]
        output = sw_sketch.convert_entities(self.args)
        self.assertFalse(output["ok"], output)
        self.assertIn("protected", output["message"])
        self.assertEqual(self.model.calls, [])

    def test_partial_native_conversion_is_undone_and_verified(self):
        self.args["sync"]["apply"] = True
        self.model.convert_fails = True
        output = sw_sketch.convert_entities(self.args)
        self.assertFalse(output["ok"], output)
        self.assertTrue(output["data"]["undone"], output)
        self.assertFalse(output["data"]["may_have_changes"], output)
        self.assertEqual(len(self.model.target.segments), 3)
        self.assertEqual([s.ConstructionGeometry for s in self.model.target.segments], [True, False, False])
        self.assertEqual(self.model.boundary, "LATER")
        self.assertIsNone(self.model.manager.ActiveSketch)

    def test_other_sketch_projection_is_preserved_but_shared_managed_target_refuses(self):
        other = Sketch("OTHER")
        other.segments = [Segment(other, 500), Segment(other, 510)]
        self.model.target.relations.append(Relation(other.segments[0], self.model.target.segments[2]))
        unrelated = sw_sketch.convert_entities(self.args)
        self.assertTrue(unrelated["ok"], unrelated)
        self.assertEqual(unrelated["data"]["plan"]["flag_change"], 2)
        self.model.target.relations.append(Relation(other.segments[1], self.model.target.segments[0]))
        output = sw_sketch.convert_entities(self.args)
        self.assertFalse(output["ok"], output)
        self.assertIn("multiple native source ownership", output["message"])
        self.assertFalse(output["data"]["may_have_changes"])
        self.assertEqual(self.model.calls, [])
        self.assertEqual([s.ConstructionGeometry for s in self.model.target.segments], [True, False, False])

    def test_unconfirmed_undo_group_reports_partial_state_without_consuming_history(self):
        self.args["sync"]["apply"] = True
        self.model.convert_fails = True
        self.model.group_succeeds = False
        output = sw_sketch.convert_entities(self.args)
        self.assertFalse(output["ok"], output)
        self.assertTrue(output["data"]["may_have_changes"], output)
        self.assertFalse(output["data"]["restored"], output)
        self.assertNotIn("undo", self.model.calls)
        self.assertEqual(len(self.model.target.segments), 4)

    def test_legacy_conversion_keeps_existing_open_sketch_behavior(self):
        manager = SimpleNamespace(SketchUseEdge3=lambda chain, inner: chain and not inner)
        with patch.object(sw_sketch, "_require_open_sketch", return_value=(self.model.doc, manager)), \
             patch.object(sw_sketch, "require_selection") as selected:
            output = sw_sketch.convert_entities({"selection": {"edges": [0]}})
        self.assertTrue(output["ok"], output)
        selected.assert_called_once_with(self.model.doc, {"edges": [0]})


if __name__ == "__main__":
    unittest.main()
