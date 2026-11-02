"""Fake-COM checks of drawing configuration isolation and sheet dimensions."""

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_sheet


class Body:
    def __init__(self, name="Silicone", box=None, volume_mm3=600):
        self.Name = name
        self.GetBodyBox = box or [-.005, -.015, 0, .005, .015, .002]
        self.volume_mm3 = volume_mm3
        self.selected = []

    def GetMassProperties(self, density):
        return [0, 0, 0, self.volume_mm3 / 1e9]

    def Select2(self, append, data):
        self.selected.append((append, data.Mark))
        return True


class Feature:
    def __init__(self, doc, body=None):
        self.Name = "Imported1"
        self.doc = doc
        self.states = {n: False for n in doc.names}
        self.GetNextFeature = None
        self.GetFaces = [SimpleNamespace(GetBody=lambda: body)] if body else []

    def SetSuppression2(self, state, mode, names):
        assert mode == 3
        names = names.value if hasattr(names, 'varianttype') else names
        for name in names:
            self.states[name] = state == 0
        return True

    def IsSuppressed2(self, mode, names):
        names = names.value if hasattr(names, 'varianttype') else names
        return [self.states[n] for n in names]


class Document:
    def __init__(self):
        self.names = ["Default", "FLAT_DRAWING", "Other"]
        self.current = "Default"
        self.activation = []
        self.features = []
        self.bodies = [Body()]
        self.created = []
        self.move_args = None
        self.inserted_override = None
        self.fail_move = False
        self.ignore_move = False
        self.selected = []
        self.ConfigurationManager = SimpleNamespace(AddConfiguration2=self.add_configuration)
        self.Extension = SimpleNamespace(SelectByID2=self.select)
        self.FeatureManager = SimpleNamespace(InsertMoveCopyBody2=self.move)
        self.sync()

    def sync(self):
        self.ConfigurationManager.ActiveConfiguration = SimpleNamespace(Name=self.current)
        self.FirstFeature = self.features[0] if self.features else None
        for left, right in zip(self.features, self.features[1:]):
            left.GetNextFeature = right

    def GetConfigurationNames(self):
        return tuple(self.names)

    def ShowConfiguration2(self, name):
        self.activation.append(name)
        if name not in self.names:
            return False
        self.current = name
        self.sync()
        return True

    def add_configuration(self, name, comment, alternate, options, source, description, rebuild):
        self.created.append((name, source, self.current))
        self.names.append(name)
        self.current = name
        self.sync()
        return SimpleNamespace(Name=name)

    def GetBodies2(self, kind, visible_only):
        return self.bodies

    def ClearSelection2(self, clear_all):
        pass

    def select(self, name, kind, x, y, z, append, mark, callout, options):
        self.selected.append((name, kind, append, mark))
        return True

    def add_feature(self, body=None):
        feature = Feature(self, body)
        self.features.append(feature)
        self.sync()
        return feature

    def move(self, *args):
        self.move_args = args
        if self.fail_move:
            raise RuntimeError("injected move failure")
        if not self.ignore_move:
            selected_names = {entry[0] for entry in self.selected}
            for body in self.bodies:
                if body.Name in selected_names:
                    body.GetBodyBox = [coordinate + args[i % 3]
                                       for i, coordinate in enumerate(body.GetBodyBox)]
        return self.add_feature()

    def CreateFeatureFromBody3(self, body, reference, options):
        self.created.append((self.current, reference, options))
        return self.add_feature(self.inserted_override or body)


class Modeler:
    def __init__(self):
        self.parameters = None

    def CreateBodyFromBox3(self, parameters):
        self.parameters = list(parameters)
        x, y, z, ax, ay, az, width, length, thickness = self.parameters
        assert [ax, ay, az] == [0, 0, 1]
        return Body("Temporary", [x-width/2, y-length/2, z, x+width/2, y+length/2, z+thickness],
                    width * length * thickness * 1e9)


class SheetToolsTests(unittest.TestCase):
    def setUp(self):
        self.doc = Document()
        self.modeler = Modeler()
        self.app = SimpleNamespace(GetModeler=lambda: self.modeler)
        self.guard = patch.object(sw_sheet, "require_document_path", return_value=(self.app, self.doc))
        self.flags = patch.object(sw_sheet, "flag_methods", side_effect=lambda obj, *names: obj)
        self.arrays = patch.object(sw_sheet, "double_array", side_effect=lambda values: values)
        self.extension = patch.object(sw_sheet, "extension", side_effect=lambda doc: doc.Extension)
        self.nothing = patch.object(sw_sheet, "nothing", return_value=None)
        self.guard.start()
        self.flags.start()
        self.arrays.start()
        self.extension.start()
        self.nothing.start()
        self.addCleanup(patch.stopall)

    def move_args(self, **changes):
        return dict(expected_document_path="C:/part.SLDPRT", configuration="FLAT_DRAWING",
                    body_names=["Silicone"], translation_mm=[10, -20, 0], feature_name="DrawingMove",
                    **changes)

    def sheet_args(self, **changes):
        args = dict(expected_document_path="C:/part.SLDPRT", configuration="FLAT_DRAWING",
                    source_body_name="Silicone", width_mm=10, thickness_mm=2, origin_mm=[1, 2, 3],
                    feature_name="DrawingSheet", length_mm=30)
        args.update(changes)
        return args

    def test_move_isolated_to_explicit_root_drawing_configuration(self):
        out = sw_sheet.move_bodies_for_drawing(self.move_args())
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.doc.current, "Default")
        self.assertEqual(self.doc.features[0].states,
                         {"Default": True, "FLAT_DRAWING": False, "Other": True})
        self.assertEqual(self.doc.selected, [("Silicone", "SOLIDBODY", True, 1)])
        self.assertEqual(self.doc.move_args, (.01, -.02, 0, 0, 0, 0, 0, 0, 0, 0, False, 1))
        for actual, expected in zip(out["data"]["bounding_boxes_mm"][0], [5, -35, 0, 15, -5, 2]):
            self.assertAlmostEqual(actual, expected)

    def test_unchanged_geometry_reports_remaining_move_and_restores_configuration(self):
        self.doc.ignore_move = True
        out = sw_sheet.move_bodies_for_drawing(self.move_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["remaining_feature"], "DrawingMove")
        self.assertFalse(out["data"]["rollback_performed"])
        self.assertEqual(self.doc.current, "Default")

    def test_move_exception_restores_original_configuration(self):
        self.doc.fail_move = True
        out = sw_sheet.move_bodies_for_drawing(self.move_args())
        self.assertFalse(out["ok"])
        self.assertEqual(self.doc.current, "Default")
        self.assertIn("injected", out["message"])

    def test_suppression_failure_reports_feature_and_restores_configuration(self):
        with patch.object(Feature, "SetSuppression2", return_value=False):
            out = sw_sheet.move_bodies_for_drawing(self.move_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["remaining_feature"], "Imported1")
        self.assertFalse(out["data"]["rollback_performed"])
        self.assertEqual(self.doc.current, "Default")

    def test_collision_rejected_without_activation_or_new_feature(self):
        self.doc.add_feature().Name = "DrawingMove"
        out = sw_sheet.move_bodies_for_drawing(self.move_args())
        self.assertFalse(out["ok"])
        self.assertEqual(self.doc.activation, [])
        self.assertIsNone(self.doc.move_args)

    def test_missing_and_duplicate_bodies_rejected_before_geometry_mutation(self):
        for bodies in [[], [Body(), Body()]]:
            self.doc.bodies = bodies
            out = sw_sheet.move_bodies_for_drawing(self.move_args())
            self.assertFalse(out["ok"])
            self.assertIsNone(self.doc.move_args)
            self.assertEqual(self.doc.current, "Default")
        args = self.move_args()
        args["body_names"] = ["Silicone", "Silicone"]
        self.doc.activation.clear()
        self.assertFalse(sw_sheet.move_bodies_for_drawing(args)["ok"])
        self.assertEqual(self.doc.activation, [])

    def test_create_configuration_restores_original_and_existing_name_is_read_only(self):
        args = dict(expected_document_path="C:/part.SLDPRT", name="NEW_DRAWING", source_configuration="Other")
        out = sw_sheet.create_drawing_configuration(args)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.doc.created, [("NEW_DRAWING", "Other", "Other")])
        self.assertEqual(self.doc.current, "Default")
        self.doc.activation.clear()
        self.assertFalse(sw_sheet.create_drawing_configuration(args)["data"]["created"])
        self.assertEqual(self.doc.activation, [])

    def test_missing_source_and_target_configuration_do_not_create_features(self):
        out = sw_sheet.create_drawing_configuration(dict(expected_document_path="p", name="NEW", source_configuration="missing"))
        self.assertFalse(out["ok"])
        args = self.move_args()
        args["configuration"] = "missing"
        self.assertFalse(sw_sheet.move_bodies_for_drawing(args)["ok"])
        self.assertEqual(self.doc.created, [])
        self.assertEqual(self.doc.features, [])
        self.assertEqual(self.doc.activation, [])

    def test_explicit_box_exact_dimensions_position_volume_and_scope(self):
        source = self.doc.bodies[0]
        out = sw_sheet.create_flat_sheet_body(self.sheet_args())
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.modeler.parameters, [.001, .002, .003, 0, 0, 1, .01, .03, .002])
        self.assertEqual(out["data"]["dimensions_mm"], [10, 30, 2])
        self.assertAlmostEqual(out["data"]["volume_mm3"], 600)
        self.assertEqual(self.doc.current, "Default")
        self.assertIs(self.doc.bodies[0], source)
        self.assertEqual(self.doc.features[0].states,
                         {"Default": True, "FLAT_DRAWING": False, "Other": True})
        self.assertEqual(self.doc.created, [("FLAT_DRAWING", False, 0)])

    def test_preserve_volume_length_is_derived_only_when_requested(self):
        args = self.sheet_args(length_mode="preserve_volume")
        del args["length_mm"]
        self.doc.bodies[0].volume_mm3 = 900
        out = sw_sheet.create_flat_sheet_body(args)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["dimensions_mm"], [10, 45, 2])

    def test_ambiguous_length_policy_and_nonfinite_values_rejected_before_mutation(self):
        cases = [dict(length_mode="preserve_volume"), dict(length_mm=None), dict(length_mode="guess"),
                 dict(width_mm=float("nan")), dict(thickness_mm=0), dict(origin_mm=[0, float("inf"), 0])]
        for changes in cases:
            with self.subTest(changes=changes):
                out = sw_sheet.create_flat_sheet_body(self.sheet_args(**changes))
                self.assertFalse(out["ok"])
                self.assertIsNone(self.modeler.parameters)
                self.assertEqual(self.doc.activation, [])
                self.assertEqual(self.doc.created, [])

    def test_inserted_body_wrong_orientation_reports_remaining_isolated_feature(self):
        self.doc.inserted_override = Body(box=[0, 0, 0, .002, .03, .01])
        out = sw_sheet.create_flat_sheet_body(self.sheet_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["remaining_feature"], "DrawingSheet")
        self.assertFalse(out["data"]["rollback_performed"])
        self.assertEqual(self.doc.current, "Default")
        self.assertTrue(self.doc.features[0].states["Default"])

    def test_configuration_list_is_read_only_and_supports_suppression(self):
        feature = self.doc.add_feature()
        feature.states["Other"] = True
        with patch.object(sw_sheet, "active_document", return_value=(self.app, self.doc)):
            out = sw_sheet.list_configurations({"include_suppression": True})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["features"][0]["suppressed"]["Other"], True)
        self.assertEqual(self.doc.activation, [])

    def suppression_args(self, **changes):
        args = dict(expected_document_path="C:/part.SLDPRT", configuration="FLAT_DRAWING",
                    feature_names=["Imported1"], suppressed=True)
        args.update(changes)
        return args

    def test_feature_suppression_changes_only_target_and_restores_caller(self):
        feature = self.doc.add_feature()
        feature.states["Other"] = True
        out = sw_sheet.set_configuration_features(self.suppression_args())
        self.assertTrue(out["ok"], out)
        self.assertEqual(feature.states, {"Default": False, "FLAT_DRAWING": True, "Other": True})
        self.assertEqual(self.doc.current, "Default")
        self.assertEqual(out["data"]["suppressed"]["Imported1"], feature.states)

    def test_feature_suppression_failure_restores_caller_without_claiming_rollback(self):
        self.doc.add_feature()
        with patch.object(Feature, "SetSuppression2", side_effect=RuntimeError("injected suppression failure")):
            out = sw_sheet.set_configuration_features(self.suppression_args())
        self.assertFalse(out["ok"])
        self.assertEqual(self.doc.current, "Default")
        self.assertEqual(out["data"]["attempted_features"], ["Imported1"])
        self.assertFalse(out["data"]["rollback_performed"])

    def test_all_feature_names_preflight_before_suppression_or_activation(self):
        first = self.doc.add_feature()
        cases = [["Imported1", "missing"], ["Imported1", "Imported1"]]
        with patch.object(Feature, "SetSuppression2") as mutate:
            for names in cases:
                out = sw_sheet.set_configuration_features(self.suppression_args(feature_names=names))
                self.assertFalse(out["ok"])
            self.doc.add_feature()  # Ambiguous existing feature name.
            out = sw_sheet.set_configuration_features(self.suppression_args())
            self.assertFalse(out["ok"])
            mutate.assert_not_called()
        self.assertEqual(self.doc.activation, [])
        self.assertFalse(any(first.states.values()))


if __name__ == "__main__":
    unittest.main()
