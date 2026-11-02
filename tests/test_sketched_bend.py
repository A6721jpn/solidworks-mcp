import math
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_sheet


class Bend:
    def __init__(self, data):
        self.Name = "Bend"
        self.GetTypeName2 = "SM3dBend"
        self.GetDefinition = data
        self.GetNextFeature = None
        self.GetErrorCode = 0
        self.IsRolledBack = False


class SketchedBendTests(unittest.TestCase):
    def setUp(self):
        self.allowance = SimpleNamespace(Type=2, KFactor=.42)
        self.data = SimpleNamespace(
            BendAngle=math.radians(72), BendRadius=.0018, ReverseDirection=False,
            PositionType=4, UseDefaultBendRadius=False, UseDefaultBendAllowance=False,
            UseGaugeTable=False, GetCustomBendAllowance=self.allowance)
        self.bend = Bend(self.data)
        self.doc = SimpleNamespace(
            FirstFeature=self.bend, GetConfigurationNames=["Default"],
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default")),
            SketchManager=SimpleNamespace(ActiveSketch=None), GetSaveFlag=True)
        self.guard = patch.object(sw_sheet, "require_document_path", return_value=(None, self.doc))
        self.flags = patch.object(sw_sheet, "flag_methods", side_effect=lambda obj, *names: obj)
        self.guard.start()
        self.flags.start()
        self.addCleanup(patch.stopall)

    def inspect(self):
        return sw_sheet.get_sketched_bend(dict(expected_document_path="C:/part.SLDPRT", feature_name="Bend"))

    def test_inspection_reads_native_values_and_keeps_existing_state(self):
        self.doc.ClearSelection2 = unittest.mock.Mock(side_effect=AssertionError("selection changed"))
        self.doc.ForceRebuild3 = unittest.mock.Mock(side_effect=AssertionError("rebuilt"))
        self.doc.ShowConfiguration2 = unittest.mock.Mock(side_effect=AssertionError("configuration changed"))
        self.data.AccessSelections = unittest.mock.Mock(side_effect=AssertionError("selection access"))
        for dirty in (False, True):
            self.doc.GetSaveFlag = dirty
            self.bend.IsRolledBack = dirty
            out = self.inspect()
            self.assertTrue(out["ok"], out)
            actual = out["data"]
            self.assertAlmostEqual(actual["angle_deg"], 72)
            self.assertAlmostEqual(actual["inner_radius_mm"], 1.8)
            self.assertEqual(actual["k_factor"], .42)
            self.assertFalse(actual["reverse_direction"])
            self.assertEqual(actual["configuration"], "Default")
            self.assertFalse(actual["stationary_reference"]["available"])
            self.assertEqual(self.doc.GetSaveFlag, dirty)
            self.assertEqual(self.bend.IsRolledBack, dirty)

    def test_default_allowance_does_not_claim_custom_k_is_effective(self):
        self.data.UseDefaultBendAllowance = True
        actual = self.inspect()["data"]
        self.assertIsNone(actual["k_factor"])
        self.assertEqual(actual["custom_bend_allowance"], {"type": 2, "k_factor": .42})
        self.assertEqual(actual["k_factor_source"], "default_unavailable")
        self.data.GetCustomBendAllowance = None
        out = self.inspect()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["custom_bend_allowance"], {"type": None, "k_factor": None})

    def test_read_failure_does_not_change_selection_or_rollback(self):
        self.data.GetCustomBendAllowance = None
        self.bend.IsRolledBack = True
        self.doc.ClearSelection2 = unittest.mock.Mock(side_effect=AssertionError("selection changed"))
        out = self.inspect()
        self.assertFalse(out["ok"])
        self.assertIn("allowance is unavailable", out["message"])
        self.assertTrue(self.bend.IsRolledBack)
        self.doc.ClearSelection2.assert_not_called()

    def test_wrong_type_and_active_sketch_rejected(self):
        self.bend.GetTypeName2 = "Imported"
        self.assertFalse(self.inspect()["ok"])
        self.bend.GetTypeName2 = "SM3dBend"
        self.doc.SketchManager.ActiveSketch = object()
        self.assertFalse(self.inspect()["ok"])

    def prepare_creation(self):
        sketch = SimpleNamespace(
            Name="BendLine", GetTypeName2="ProfileFeature", GetNextFeature=None,
            GetSpecificFeature2=SimpleNamespace(GetSketchSegments=[
                SimpleNamespace(ConstructionGeometry=False, GetType=0)]),
            Select2=lambda append, mark: True)
        face = SimpleNamespace(GetSurface=SimpleNamespace(Identity=4001),
                               GetBody=SimpleNamespace(IsSheetMetal=True),
                               GetClosestPointOn=lambda *p: p)
        self.doc.FirstFeature = sketch
        self.doc.GetBodies2 = lambda *args: [SimpleNamespace(IsSheetMetal=True)]
        self.doc.ClearSelection2 = unittest.mock.Mock()
        self.doc.ForceRebuild3 = unittest.mock.Mock()
        self.doc.SelectionManager = SimpleNamespace(GetSelectedObject6=lambda *args: face)
        self.doc.FeatureManager = SimpleNamespace(
            CreateDefinition=lambda kind: self.data,
            CreateCustomBendAllowance=lambda: self.allowance,
            CreateFeature=lambda definition: self.bend)
        self.data.SetCustomBendAllowance = lambda allowance: None
        self.extension = patch.object(sw_sheet, "extension", return_value=SimpleNamespace(SelectByID2=lambda *args: True))
        self.extension.start()

    def create(self):
        return sw_sheet.create_sketched_bend(dict(
            expected_document_path="C:/part.SLDPRT", sketch_name="BendLine", feature_name="CreatedBend",
            stationary_point_mm=[1, 2, 0], angle_deg=90, inner_radius_mm=2,
            reverse_direction=True, k_factor=.5))

    def test_creator_uses_actual_readback_and_reports_remaining_mismatch(self):
        self.prepare_creation()
        native = SimpleNamespace(**vars(self.data))
        native.BendAngle = math.radians(80)
        native.BendRadius = .0015
        native.GetCustomBendAllowance = SimpleNamespace(Type=2, KFactor=.4)
        self.bend.GetDefinition = native
        out = self.create()
        self.assertFalse(out["ok"], out)
        self.assertIn("native readback differs", out["message"])
        self.assertEqual(out["data"]["remaining_feature"], "CreatedBend")
        self.assertFalse(out["data"]["rollback_performed"])
        actual = out["data"]["native_readback"]
        self.assertAlmostEqual(actual["angle_deg"], 80)
        self.assertAlmostEqual(actual["inner_radius_mm"], 1.5)
        self.assertEqual(actual["k_factor"], .4)
        self.doc.ClearSelection2.assert_called_with(True)

    def test_creator_success_returns_readback(self):
        self.prepare_creation()
        out = self.create()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["angle_deg"], 90)
        self.assertEqual(out["data"]["k_factor"], .5)
        self.assertEqual(out["data"]["custom_bend_allowance"], {"type": 2, "k_factor": .5})


if __name__ == "__main__":
    unittest.main()
