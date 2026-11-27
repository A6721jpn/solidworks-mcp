"""Scoped native editing contracts without a CAD session."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_sketch_edit as ops


class Dimension:
    def __init__(self, name, kind=0):
        self.FullName = name + "@Part.Part"
        self.GetType = lambda: kind
        self.DrivenState = 2
        self.SystemValue = .01
        self.values = [.01, .02]
        self.writes = []
        self.status = 0

    def GetSystemValue3(self, scope, names):
        return self.values if scope == 2 else self.values[:1]

    def SetSystemValue3(self, target, scope, names):
        self.writes.append((target, scope))
        if self.status == 0:
            self.SystemValue = target
            self.values = [target, target if scope == 2 else self.values[1]]
        return self.status

    @property
    def Name(self):
        return self.FullName.split("@")[0]

    @Name.setter
    def Name(self, name):
        self.FullName = name + "@" + self.FullName.split("@", 1)[1]


class Equations:
    def __init__(self):
        self.rows = []
        self.Status = -1
        self.writes = []
        self.option = 2
        self.failed_evaluation = False

    def GetCount(self):
        return len(self.rows)

    def Equation(self, index):
        return self.rows[index]

    def Value(self, index):
        self.Status = -1 if self.failed_evaluation else index
        return 0 if self.failed_evaluation else 20

    def Add2(self, index, equation, solve):
        self.writes.append(("Add2", equation))
        self.rows.append(equation)
        return len(self.rows) - 1

    def Add3(self, index, equation, solve, scope, names):
        self.writes.append(("Add3", equation, scope))
        self.rows.append(equation)
        return len(self.rows) - 1

    def SetEquation(self, index, equation):
        self.writes.append(("Equation", equation))
        self.rows[index] = equation

    def GetConfigurationOption(self, index):
        return self.option

    def SetEquationAndConfigurationOption(self, index, equation, scope, names):
        self.writes.append(("SetEquationAndConfigurationOption", equation, scope))
        self.rows[index] = equation
        return index


class Relations:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []

    def GetRelations(self, relation_filter):
        return list(self.rows)

    def DeleteRelation(self, relation):
        self.deleted.append(relation)
        self.rows.remove(relation)
        return True


class ScopedSketchTests(unittest.TestCase):
    def setUp(self):
        self.dimensions = [Dimension("D1@Sketch1"), Dimension("D2@Sketch1")]
        self.equations = Equations()
        self.relations = Relations([])
        self.sketch = SimpleNamespace(RelationManager=self.relations, GetConstrainedStatus=lambda: 2,
                                      GetSketchSegments=lambda: [])
        self.feature = SimpleNamespace(Name="Sketch1", GetTypeName2=lambda: "ProfileFeature",
                                       GetSpecificFeature2=lambda: self.sketch, GetFirstDisplayDimension=lambda: None)
        self.doc = SimpleNamespace(
            GetPathName=lambda: "C:/part.SLDPRT", GetTitle=lambda: "part",
            GetSaveFlag=lambda: True,
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default")),
            SketchManager=SimpleNamespace(ActiveSketch=None), GetEquationMgr=lambda: self.equations,
            GetConfigurationNames=lambda: ["Default", "Other"],
            FeatureByName=lambda name: self.feature if name == "Sketch1" else None,
            Parameter=lambda name: next((d for d in self.dimensions if d.FullName.rsplit("@", 1)[0] == name), None))
        self.app = SimpleNamespace(IsSame=lambda a, b: 1 if a is b else 0)
        self.args = {"expected_document_path": "C:/part.SLDPRT"}
        self.rebuilt = []
        self.attach = patch.object(ops, "require_document_path", return_value=(self.app, self.doc))
        self.attach.start()
        self.rebuild = patch.object(ops, "rebuild", side_effect=lambda doc: self.rebuilt.append(doc) or True)
        self.rebuild.start()
        self.addCleanup(self.attach.stop)
        self.addCleanup(self.rebuild.stop)

    def updates(self, **kwargs):
        return {**self.args, "updates": [{"full_name": "D1@Sketch1", "value_mm": 20},
                                        {"full_name": "D2@Sketch1", "value_mm": 30}], **kwargs}

    def relation(self, kind=4, ids=(0, 1), point=False):
        entity = SimpleNamespace(GetID=lambda: ids, GetSketch=lambda: self.sketch)
        if point:
            entity.X = entity.Y = entity.Z = 0
        else:
            entity.GetType = lambda: 0
        return SimpleNamespace(GetRelationType=lambda: kind, GetDefinitionEntities=lambda: [entity])

    def inspect(self, **kwargs):
        return ops.inspect_sketch({**self.args, "sketch_name": "Sketch1", **kwargs})

    def test_dimension_validation_is_complete_before_writes(self):
        cases = [self.updates(updates=[{"full_name": "D1@Sketch1", "value_mm": 20, "value_deg": 20}]),
                 self.updates(updates=[{"full_name": "D1@Sketch1", "value_mm": float("nan")}]),
                 self.updates(updates=[{"full_name": "D1@Sketch1", "value_mm": True}]),
                 self.updates(updates=[{"full_name": "D1@Sketch1", "value_mm": 2}] * 2),
                 self.updates(updates=[{"full_name": "D1@Sketch1", "value_deg": 2}]),
                 self.updates(updates=[{"full_name": "D1@Sketch1", "value_mm": 2},
                                       {"full_name": "Missing@Sketch1", "value_mm": 3}])]
        for args in cases:
            self.assertFalse(ops.set_dimensions(args)["ok"])
        self.dimensions[1].DrivenState = 1
        self.assertFalse(ops.set_dimensions(self.updates())["ok"])
        self.assertFalse(any(d.writes for d in self.dimensions))
        self.assertEqual(self.rebuilt, [])

    def test_equation_controlled_dimension_is_rejected_before_any_write(self):
        self.equations.rows = ['"D2@Sketch1" = 10']
        self.assertFalse(ops.set_dimensions(self.updates())["ok"])
        self.assertFalse(any(d.writes for d in self.dimensions))

    def test_bulk_dimensions_default_active_and_rebuild_once(self):
        out = ops.set_dimensions(self.updates())
        self.assertTrue(out["ok"], out)
        self.assertEqual([d.writes for d in self.dimensions], [[(.02, 1)], [(.03, 1)]])
        self.assertEqual([d.values[1] for d in self.dimensions], [.02, .02])
        self.assertEqual(len(self.rebuilt), 1)

    def test_all_configuration_write_reports_active_only_verification(self):
        out = ops.set_dimensions(self.updates(configuration_scope="all"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.dimensions[1].values, [.03, .03])
        after = out["data"]["items"][1]["after"]
        self.assertEqual(after["verified_configurations"], ["Default"])
        self.assertEqual(after["unverified_configurations"], ["Other"])

    def test_native_failure_stops_and_reports_applied_items(self):
        self.dimensions[1].status = 1
        self.dimensions.append(Dimension("D3@Sketch1"))
        args = self.updates()
        args["updates"].append({"full_name": "D3@Sketch1", "value_mm": 40})
        out = ops.set_dimensions(args)
        self.assertFalse(out["ok"], out)
        self.assertTrue(out["data"]["partial"])
        self.assertEqual(out["data"]["applied"], ["D1@Sketch1"])
        self.assertEqual(self.dimensions[2].writes, [])
        self.assertEqual(len(self.rebuilt), 1)

    def test_partial_failure_rereads_values_after_rebuild(self):
        self.dimensions[1].status = 1
        def rebuild(doc):
            self.dimensions[0].values[0] = .012
            return True
        with patch.object(ops, "rebuild", side_effect=rebuild):
            out = ops.set_dimensions(self.updates())
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["partial"])
        self.assertEqual(out["data"]["items"][0]["after"]["system_values"], [.012])
        self.assertNotIn("D1@Sketch1", out["data"]["applied"])

    def test_incomplete_dimension_enumeration_is_not_success(self):
        self.feature.GetFirstDisplayDimension = lambda: SimpleNamespace(GetDimension2=lambda i:self.dimensions[0])
        self.feature.GetNextDisplayDimension = lambda display: (_ for _ in ()).throw(RuntimeError("COM read failed"))
        out = ops.inspect_sketch({"expected_document_path":"C:/part.SLDPRT", "sketch_name":"Sketch1",
                                  "include_relations":False, "include_equations":False})
        self.assertFalse(out["ok"])
        self.assertIn("COM read failed",out["message"])

    def test_final_verification_reads_all_attempted_items(self):
        def rebuild(doc):
            self.dimensions[0].values[0] = .012
            self.dimensions[1].values[0] = .013
            return True
        with patch.object(ops, "rebuild", side_effect=rebuild):
            out = ops.set_dimensions(self.updates())
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["applied"], [])
        self.assertEqual([i["after"]["system_values"] for i in out["data"]["items"]], [[.012],[.013]])

    def test_open_sketch_is_preserved_and_never_rebuilt(self):
        self.doc.SketchManager.ActiveSketch = self.sketch
        self.assertTrue(ops.set_dimensions(self.updates())["ok"])
        self.assertIs(self.doc.SketchManager.ActiveSketch, self.sketch)
        self.assertEqual(self.rebuilt, [])

    def test_rebuild_failure_reports_partial(self):
        with patch.object(ops, "rebuild", return_value=False):
            out = ops.set_dimensions(self.updates())
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["partial"])

    def test_post_rebuild_value_changes_are_detected(self):
        def rebuild(doc):
            self.dimensions[0].values[0] = .01
            return True
        with patch.object(ops, "rebuild", side_effect=rebuild):
            out = ops.set_dimensions(self.updates())
        self.assertFalse(out["ok"])
        self.assertIn("final verification", out["message"])

    def test_relation_signatures_survive_order_but_reject_wrong_type_and_duplicates(self):
        a, b = self.relation(), self.relation(5, (0, 2), point=True)
        self.relations.rows = [a, b]
        refs = {r["type"]: r["relation_ref"] for r in self.inspect()["data"]["relations"]}
        self.relations.rows.reverse()
        self.assertEqual(refs, {r["type"]: r["relation_ref"] for r in self.inspect()["data"]["relations"]})
        self.relations.rows = [a, a]
        self.assertTrue(all(r["relation_ref"] is None for r in self.inspect()["data"]["relations"]))

    def test_relation_deletion_requires_open_named_sketch_and_fresh_signature(self):
        relation = self.relation()
        self.relations.rows = [relation]
        ref = self.inspect()["data"]["relations"][0]["relation_ref"]
        args = {**self.args, "sketch_name": "Sketch1", "relation_refs": [ref]}
        self.assertFalse(ops.delete_sketch_relations(args)["ok"])
        self.doc.SketchManager.ActiveSketch = self.sketch
        relation.GetRelationType = lambda: 5
        self.assertFalse(ops.delete_sketch_relations(args)["ok"])
        self.assertEqual(self.relations.deleted, [])
        args["relation_refs"] = [self.inspect()["data"]["relations"][0]["relation_ref"]]
        out = ops.delete_sketch_relations(args)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.relations.rows, [])
        self.assertEqual(self.rebuilt, [])

    def test_relation_filter_avoids_unrequested_entity_reads_and_empty_definition_falls_back(self):
        ignored = self.relation(5)
        ignored.GetDefinitionEntities = lambda: self.fail("unrequested definition entities were read")
        wanted = self.relation(4)
        original = wanted.GetDefinitionEntities
        wanted.GetDefinitionEntities = lambda: None
        wanted.GetEntities = original
        self.relations.rows = [ignored, wanted]
        out = self.inspect(relation_types=["horizontal"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["data"]["relations"]), 1)
        self.assertEqual(out["data"]["relations"][0]["entities_source"], "GetEntities")

    def test_changed_signature_immediately_before_deletion_is_rejected(self):
        self.doc.SketchManager.ActiveSketch = self.sketch
        self.relations.rows = [self.relation()]
        rows = ops._relations(self.app, self.doc, self.sketch, "Sketch1")
        args = {**self.args, "sketch_name": "Sketch1", "relation_refs": [rows[0][0]["relation_ref"]]}
        with patch.object(ops, "_relations", side_effect=[rows, []]):
            out = ops.delete_sketch_relations(args)
        self.assertFalse(out["ok"])
        self.assertEqual(self.relations.deleted, [])

    def test_rename_rejects_collision_and_reads_native_new_name(self):
        args = {**self.args, "full_name": "D1@Sketch1", "new_name": "D2"}
        self.assertFalse(ops.rename_dimension(args)["ok"])
        args["new_name"] = "Width"
        out = ops.rename_dimension(args)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["items"][0]["after"]["full_name"], "Width@Sketch1")

    def test_equation_cas_and_scope_are_preflighted(self):
        self.equations.rows = ['"Width@Sketch1" = 10']
        args = {**self.args, "equation": '"Width@Sketch1" = 20', "configuration_scope": "all"}
        self.assertFalse(ops.set_equation(args)["ok"])
        args["expected_equation"] = '"Width@Sketch1" = 11'
        self.assertFalse(ops.set_equation(args)["ok"])
        args["expected_equation"] = self.equations.rows[0]
        self.assertFalse(ops.set_equation({**args, "configuration_scope": "active"})["ok"])
        self.assertEqual(self.equations.writes, [])
        self.assertTrue(ops.set_equation(args)["ok"])
        self.assertEqual(self.equations.writes[0][0], "SetEquationAndConfigurationOption")

    def test_equation_evaluation_failure_is_not_accepted_as_add_success(self):
        self.equations.failed_evaluation = True
        out = ops.set_equation({**self.args, "equation": '"Width" = 20', "configuration_scope": "all"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["partial"])
        self.assertEqual(out["data"]["items"][0]["after"]["evaluation_status"], -1)

    def test_single_configuration_equations_use_add2(self):
        self.doc.GetConfigurationNames = lambda: ["Default"]
        out = ops.set_equation({**self.args, "equation": '"Width" = 20'})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.equations.writes[0][0], "Add2")


if __name__ == "__main__":
    unittest.main()
