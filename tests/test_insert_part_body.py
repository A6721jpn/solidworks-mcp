from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import sw_file, sw_sheet, sw_surface_ops


class Body:
    def __init__(self, name, box, volume):
        self.Name = name
        self.box = box
        self.volume = volume
        self.Check2 = 0

    def Copy(self):
        return SimpleNamespace(IsTemporaryBody=True, source=self)


class Feature:
    def __init__(self, body):
        self.Name = "Imported"
        self.body = body
        self.states = {"curved": False, "flat": False, "flat_child": False}

    def SetSuppression2(self, action, _, names):
        for name in names:
            self.states[name] = action == 0
            if name == "flat":
                self.states["flat_child"] = action == 0
        return True


class Part:
    def __init__(self, bodies, config, dirty=False):
        self.solids = bodies
        self.surfaces = []
        self.config = config
        self.GetSaveFlag = dirty
        self.SketchManager = SimpleNamespace(ActiveSketch=None)
        self.features = []

    def GetBodies2(self, kind, _):
        return self.solids if kind == 0 else self.surfaces

    def CreateFeatureFromBody3(self, temporary, as_surface, options):
        assert not as_surface and options == 1
        source = temporary.source
        body = Body("inserted", source.box[:], source.volume)
        self.solids.append(body)
        feature = Feature(body)
        self.features.append(feature)
        return feature


class InsertPartBodyTests(unittest.TestCase):
    def setUp(self):
        self.existing = Body("existing", [0, 0, 0, 1, 2, 3], 6)
        self.source_body = Body("chosen", [10, 20, 30, 13, 24, 35], 60)
        self.other_body = Body("other", [70, 70, 70, 71, 71, 71], 1)
        self.target = Part([self.existing], "curved", True)
        self.source = Part([self.source_body, self.other_body], "source", True)
        self.args = {"expected_document_path": "C:/target.SLDPRT",
                     "target_configuration": "curved", "source_document_path": "C:/source.SLDPRT",
                     "source_configuration": "source", "source_body_name": "chosen", "feature_name": "COPY"}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def setpatch(module, name, **kwargs):
            return self.stack.enter_context(patch.object(module, name, **kwargs))
        setpatch(sw_file, "require_document_path", return_value=(object(), self.target))
        setpatch(sw_file, "open_document_by_path", return_value=self.source)
        setpatch(sw_file, "document_type", return_value=1)
        setpatch(sw_file, "flag_methods", side_effect=lambda item, *names: item)
        setpatch(sw_sheet, "_configurations", side_effect=lambda part: (
            ["curved", "flat", "flat_child"] if part is self.target else ["source"], part.config))
        setpatch(sw_sheet, "_features", side_effect=lambda part: part.features)
        setpatch(sw_sheet, "_body_box_mm", side_effect=lambda body: body.box[:])
        setpatch(sw_sheet, "_volume_mm3", side_effect=lambda body: body.volume)
        setpatch(sw_sheet, "_suppression", side_effect=lambda feature, names: {
            name: feature.states[name] for name in names})
        setpatch(sw_sheet, "_configuration_array", side_effect=lambda names: names)
        setpatch(sw_surface_ops, "_feature_bodies", side_effect=lambda feature, kind: [feature.body])

    def test_one_named_solid_is_copied_with_no_link_or_save(self):
        result = sw_file.insert_part_body(self.args)
        self.assertTrue(result["ok"], result)
        self.assertEqual([body.Name for body in self.target.solids], ["existing", "inserted"])
        self.assertEqual(result["data"]["bounding_box_mm"], self.source_body.box)
        self.assertEqual(result["data"]["suppressed"], {"curved": False, "flat": True, "flat_child": True})
        self.assertTrue(result["data"]["source_dirty"])
        self.assertTrue(result["data"]["independent"])
        self.assertFalse(result["data"]["saved"])
        self.assertEqual(self.source.config, "source")
        self.assertEqual(self.target.config, "curved")
        self.assertEqual([body.Name for body in self.source.solids], ["chosen", "other"])

    def test_configuration_mismatch_does_not_modify_target(self):
        result = sw_file.insert_part_body({**self.args, "source_configuration": "wrong"})
        self.assertFalse(result["ok"])
        self.assertFalse(result["data"]["may_have_changes"])
        self.assertEqual(len(self.target.solids), 1)

    def test_child_configuration_is_isolated_after_parent_suppression(self):
        self.target.config = "flat_child"
        result = sw_file.insert_part_body({**self.args, "target_configuration": "flat_child"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["suppressed"],
                         {"curved": True, "flat": True, "flat_child": False})

    def test_failed_postcheck_reports_remaining_feature(self):
        self.stack.enter_context(patch.object(sw_surface_ops, "_feature_bodies", return_value=[]))
        result = sw_file.insert_part_body(self.args)
        self.assertFalse(result["ok"])
        self.assertTrue(result["data"]["may_have_changes"])
        self.assertEqual(result["data"]["partial_features"], ["COPY"])
        self.assertFalse(result["data"]["rollback_performed"])


if __name__ == "__main__":
    unittest.main()
