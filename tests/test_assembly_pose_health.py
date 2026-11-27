from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from solidworks_mcp import sw_assembly


POSE = {"rotation": [-1, 0, 0, 0, -1, 0, 0, 0, 1], "translation_mm": [-0.01, -30, 19]}


class Component:
    Name2 = "component-1"
    ReferencedConfiguration = "Default"
    Transform2 = SimpleNamespace(ArrayData=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])

    def __init__(self, solid_count=1, config="Default", state=2, virtual_ok=True):
        self.solid_count = solid_count
        self.state = state
        self.virtual_ok = virtual_ok
        self.virtual = False
        self.body_queries = 0
        self.model = SimpleNamespace(GetType=1, GetSaveFlag=True,
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name=config)))

    def GetPathName(self): return "component.sldprt"
    def IsSuppressed(self): return self.state == 0
    def IsFixed(self): return False
    def IsHidden(self, consider_suppressed): return False
    def GetSuppression(self): return self.state
    def GetModelDoc2(self): return self.model
    def GetBodies3(self, body_type, info):
        self.body_queries += 1
        return [object()] * self.solid_count if body_type == 0 else []
    def MakeVirtual2(self, also_children):
        self.virtual = self.virtual_ok
        if self.virtual:
            self.Name2 = "virtual-component-1"
        return self.virtual
    def IsVirtual(self): return self.virtual


class AssemblyOperationTests(unittest.TestCase):
    def setUp(self):
        self.flags = patch.object(sw_assembly, "flag_methods", side_effect=lambda obj, *names: obj)
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def test_pose_rejects_invalid_rotation_before_accessing_cad(self):
        for invalid in ([1, 0, 0, 0, 1, 0, 0, 0, -1], [float('nan')] * 9):
            with self.subTest(rotation=invalid), patch.object(sw_assembly, "require_assembly") as get_assembly:
                out = sw_assembly.insert_component({"path": "unused", "pose": {"rotation": invalid, "translation_mm": [0, 0, 0]}})
                self.assertFalse(out["ok"])
                get_assembly.assert_not_called()

    def test_expected_assembly_guard_runs_before_insert(self):
        with patch.object(sw_assembly, "require_document_path", side_effect=RuntimeError("wrong assembly")), \
             patch.object(sw_assembly, "require_assembly") as legacy:
            with self.assertRaisesRegex(RuntimeError, "wrong assembly"):
                sw_assembly.insert_component({"path": "unused.sldprt", "expected_document_path": "target.sldasm"})
            legacy.assert_not_called()

    def test_pose_and_virtual_are_verified(self):
        component = Component()
        util = SimpleNamespace(CreateTransform=lambda matrix: SimpleNamespace(ArrayData=matrix))
        app = SimpleNamespace(OpenDoc6=lambda *args: None, GetMathUtility=lambda: util)
        doc = SimpleNamespace(AddComponent5=lambda *args: component)
        path = Path("source.sldprt").resolve()
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(sw_assembly, "require_assembly", return_value=(app, doc)), \
             patch.object(sw_assembly, "double_array", side_effect=lambda x: x), \
             patch.object(sw_assembly, "rebuild", return_value=True):
            out = sw_assembly.insert_component({"path": str(path), "pose": POSE, "virtual": True})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["pose"], POSE)
        self.assertTrue(out["data"]["virtual"])
        self.assertEqual(out["data"]["component"], "virtual-component-1")

    def test_virtual_failure_reports_component_left_in_assembly(self):
        component = Component(virtual_ok=False)
        app = SimpleNamespace(OpenDoc6=lambda *args: None)
        doc = SimpleNamespace(AddComponent5=lambda *args: component)
        path = Path("source.sldprt").resolve()
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(sw_assembly, "require_assembly", return_value=(app, doc)):
            out = sw_assembly.insert_component({"path": str(path), "virtual": True})
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["may_have_changes"])
        self.assertEqual(out["data"]["component"], "component-1")

    def test_health_distinguishes_empty_from_unavailable(self):
        good, empty, mismatch, suppressed = Component(), Component(0), Component(config="Other"), Component(state=0)
        with patch.object(sw_assembly, "require_assembly", return_value=(None, object())), \
             patch.object(sw_assembly, "iter_components", return_value=[good, empty, mismatch, suppressed]):
            out = sw_assembly.list_components({"include_health": True, "include_pose": True})
        self.assertTrue(out["ok"])
        entries = out["data"]["components"]
        self.assertEqual([e["health"]["geometry_state"] for e in entries], ["present", "empty", "unknown", "unknown"])
        self.assertEqual(mismatch.body_queries, 0)
        self.assertEqual(suppressed.body_queries, 0)
        self.assertIsNone(entries[2]["health"]["solid_bodies"])
        self.assertTrue(entries[0]["health"]["dirty"])
        self.assertEqual(entries[0]["pose"]["translation_mm"], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
