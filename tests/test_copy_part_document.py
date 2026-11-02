from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from solidworks_mcp import sw_file


class CopyPartTests(unittest.TestCase):
    def test_copy_restores_sketch_and_keeps_source_unsaved(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'copy.SLDPRT'
            sketch=object()
            manager=SimpleNamespace(ActiveSketch=sketch)
            feature=SimpleNamespace(Select2=lambda *args:True,Name='Master',GetTypeName2='ProfileFeature',GetSpecificFeature2=sketch)
            app=SimpleNamespace(IsSame=lambda a,b:1 if a is b else 0)
            doc=SimpleNamespace(SketchManager=manager, FeatureByName=lambda name:feature)
            doc.EditSketch=lambda:setattr(manager,'ActiveSketch',sketch)
            def save(*args):
                self.assertEqual(args[2],3)
                target.write_bytes(b'copy')
                manager.ActiveSketch=None
                return True
            info={'path':str(Path(folder)/'source.SLDPRT'),'dirty':True}
            with patch.object(sw_file,'require_document_path',return_value=(app,doc)), \
                 patch.object(sw_file,'iter_feature_objects',return_value=[feature]), \
                 patch.object(sw_file,'validated_output_path',return_value=target), \
                 patch.object(sw_file,'document_info',return_value=info), \
                 patch.object(sw_file,'extension',return_value=SimpleNamespace(SaveAs=save)), \
                 patch.object(sw_file,'flag_methods',side_effect=lambda x,*args:x):
                out=sw_file.copy_part_document({'expected_document_path':info['path'],'path':str(target)})
            self.assertTrue(out['ok'],out)
            self.assertIs(manager.ActiveSketch,sketch)
            self.assertTrue(out['data']['unsaved_state_preserved'])

    def test_existing_destination_is_rejected_before_native_save(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'copy.SLDPRT'
            target.touch()
            with patch.object(sw_file,'require_document_path',return_value=(None,object())), \
                 patch.object(sw_file,'OUTPUT_ROOT',Path(folder)), \
                 patch.object(sw_file,'extension') as extension:
                with self.assertRaisesRegex(RuntimeError,'overwrite'):
                    sw_file.copy_part_document({'expected_document_path':'source','path':str(target)})
                extension.assert_not_called()
