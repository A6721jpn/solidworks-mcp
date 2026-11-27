from types import SimpleNamespace
import unittest
from unittest.mock import patch
from solidworks_mcp import sw_sketch


class SketchCreationTests(unittest.TestCase):
    def test_rollback_creation_renames_active_sketch_not_later_feature(self):
        current=SimpleNamespace(Name='NewSketch')
        future=SimpleNamespace(Name='FutureBendAxis')
        manager=SimpleNamespace(ActiveSketch=current,InsertSketch=lambda flag:None)
        doc=SimpleNamespace(SketchManager=manager,FeatureByName=lambda name:current if name=='NewSketch' else None)
        with patch.object(sw_sketch,'require_part',return_value=(object(),doc)), \
             patch.object(sw_sketch,'resolve_plane_name',return_value='Front'), \
             patch.object(sw_sketch,'clear_selection'), \
             patch.object(sw_sketch,'select_by_id',return_value=True), \
             patch.object(sw_sketch,'sketch_manager',return_value=manager), \
             patch.object(sw_sketch,'flag_methods',side_effect=lambda obj,*args:obj), \
             patch.object(sw_sketch,'latest_sketch',return_value=('FutureBendAxis',future)) as latest:
            out=sw_sketch.create_sketch({'name':'Recess'})
        self.assertTrue(out['ok'],out)
        self.assertEqual(current.Name,'Recess')
        self.assertEqual(future.Name,'FutureBendAxis')
        latest.assert_not_called()
