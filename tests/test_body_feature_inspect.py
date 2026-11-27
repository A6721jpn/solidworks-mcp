"""Focused read-only body and feature inspection checks without SOLIDWORKS."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_inspect as inspect


def body(name, kind, visible):
    return SimpleNamespace(Name=name, GetType=lambda: kind, Visible=visible, GetBodyBox=None)


def feature(name, faces=()):
    return SimpleNamespace(
        Name=name, GetTypeName2="BaseBody", GetNextFeature=None,
        GetFaceCount=lambda: len(faces), GetFaces=lambda: list(faces),
        GetParents=[], GetChildren=None, IGetChildCount=0,
    )


def fail(message):
    def raise_error():
        raise RuntimeError(message)
    return raise_error


class InspectTests(unittest.TestCase):
    def call(self, doc, method, args):
        with patch.object(inspect, "active_document", return_value=(None, doc)):
            return method(args)

    def test_body_types_visibility_and_separate_surface_indices(self):
        solid_a, solid_b = body("A", 0, True), body("B", 0, False)
        sheet = body("Sheet", 1, None)
        doc = SimpleNamespace(GetType=1, GetBodies2=lambda kind, visible: [solid_a, solid_b] if kind == 0 else [sheet])
        out = self.call(doc, inspect.list_bodies, {"include_surfaces": True})
        self.assertTrue(out["ok"], out)
        self.assertEqual([(b["index"], b["name"], b["body_type"], b["visible"]) for b in out["data"]["bodies"]],
                         [(0, "A", "solid", True), (1, "B", "solid", False)])
        self.assertEqual(out["data"]["surface_bodies"], [
            {"surface_index": 0, "name": "Sheet", "body_type": "surface", "visible": None}])

    def test_body_read_failure_is_not_reported_as_empty(self):
        def get_bodies(kind, visible):
            if kind == 1:
                raise RuntimeError("surface unavailable")
            return [body("A", 0, True)]
        doc = SimpleNamespace(GetType=1, GetBodies2=get_bodies)
        out = self.call(doc, inspect.list_bodies, {"include_surfaces": True})
        self.assertFalse(out["ok"])
        self.assertFalse(out["data"]["complete"])
        self.assertEqual(out["data"]["bodies"][0]["name"], "A")
        self.assertIn("surface unavailable", out["data"]["read_errors"][0])

    def test_assembly_fallback_keeps_index_without_assuming_solid(self):
        fallback = body("Fallback", 1, True)
        component = SimpleNamespace(Name2="C", IsSuppressed=False, GetBodies3=lambda kind, info: [], GetBody=fallback)
        doc = SimpleNamespace(GetType=2, GetComponents=lambda top_only: [component])
        with patch.object(inspect, "empty_variant", return_value=None), patch.object(inspect, "component_transform", return_value=None):
            out = self.call(doc, inspect.list_bodies, {"include_surfaces": True})
        self.assertEqual(out["data"]["bodies"][0]["index"], 0)
        self.assertEqual(out["data"]["bodies"][0]["body_type"], "surface")
        self.assertEqual(out["data"]["surface_bodies"], [])

    def test_feature_uses_all_faces_and_direct_relationships(self):
        a, b = body("A", 0, True), body("B", 1, True)
        faces = [SimpleNamespace(GetBody=a)] * 327 + [SimpleNamespace(GetBody=b)]
        target = feature("Import2", faces)
        target.GetParents = [SimpleNamespace(Name="Parent")]
        target.GetChildren = [SimpleNamespace(Name="Child")]
        doc = SimpleNamespace(FirstFeature=target)
        out = self.call(doc, inspect.list_features, {"feature_name": "Import2"})
        self.assertTrue(out["ok"], out)
        info = out["data"]["feature"]
        self.assertEqual((info["face_count"], info["faces_examined"]), (328, 328))
        self.assertEqual(info["body_names"], ["A", "B"])
        self.assertTrue(info["body_names_complete"])
        self.assertEqual((info["parents"], info["children"]), (["Parent"], ["Child"]))

    def test_incomplete_and_failed_feature_reads_are_explicit(self):
        a = body("A", 0, True)
        target = feature("Large", [SimpleNamespace(GetBody=a)] * 1001)
        target.GetParents = fail("parent read failed")
        target.GetChildren = None
        del target.IGetChildCount
        out = self.call(SimpleNamespace(FirstFeature=target), inspect.list_features, {"feature_name": "Large"})
        info = out["data"]["feature"]
        self.assertEqual((info["face_count"], info["faces_examined"]), (1001, 1000))
        self.assertFalse(info["body_names_complete"])
        self.assertIsNone(info["parents"])
        self.assertIsNone(info["children"])
        self.assertFalse(info["parents_complete"])
        self.assertFalse(info["children_complete"])
        target.GetFaces = fail("faces unavailable")
        info = self.call(SimpleNamespace(FirstFeature=target), inspect.list_features, {"feature_name": "Large"})["data"]["feature"]
        self.assertEqual(info["face_count"], 1001)
        self.assertIsNone(info["body_names"])
        self.assertFalse(info["body_names_complete"])

    def test_exact_name_must_be_present_unique_and_enumerable(self):
        first, second = feature("Same"), feature("Same")
        first.GetNextFeature = second
        doc = SimpleNamespace(FirstFeature=first)
        self.assertIn("ambiguous", self.call(doc, inspect.list_features, {"feature_name": "Same"})["message"])
        self.assertIn("No feature", self.call(doc, inspect.list_features, {"feature_name": "Missing"})["message"])
        self.assertFalse(self.call(doc, inspect.list_features, {"feature_name": ""})["ok"])
        first.GetNextFeature = fail("tree unavailable")
        out = self.call(doc, inspect.list_features, {"feature_name": "Missing"})
        self.assertFalse(out["ok"])
        self.assertIn("tree unavailable", out["message"])

    def test_unnamed_listing_keeps_legacy_shape(self):
        features = [{"name": "Any", "type": "BaseBody"}]
        with patch.object(inspect, "iter_features", return_value=features):
            out = self.call(object(), inspect.list_features, {})
        self.assertEqual(out, {"ok": True, "message": "Read feature tree.", "data": {"features": features}})


if __name__ == "__main__":
    unittest.main()
