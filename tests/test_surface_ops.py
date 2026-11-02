"""Native-call and state-boundary checks without a running CAD session."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_surface_ops as surface


class Body:
    def __init__(self, name, kind):
        self.Name, self.kind = name, kind
        self.faces = [Face(self)]

    def GetType(self):
        return self.kind

    def GetFaces(self):
        return self.faces

    def GetBodyBox(self):
        return [0., 0., 0., .01, .01, .00005 if self.kind == 0 else 0.]

    def GetMassProperties(self, density):
        return [0., 0., 0., 5e-9]


class Face:
    def __init__(self, body):
        self.body = body

    def GetBody(self):
        return self.body

    def GetSurface(self):
        return object()

    def GetArea(self):
        return .0001


class Feature:
    def __init__(self, doc, name, bodies=(), definition=None):
        self.doc, self.Name = doc, name
        self.bodies, self.definition = list(bodies), definition
        self.states = {n: False for n in doc.names}
        self.GetNextFeature = None

    def GetFaces(self):
        return [face for body in self.bodies for face in body.faces]

    def GetDefinition(self):
        return self.definition

    def SetSuppression2(self, state, mode, names):
        for name in names.value:
            self.states[name] = state == 0
        return True

    def IsSuppressed2(self, mode, names):
        return [self.states[name] for name in names.value]

    def Select2(self, append, mark):
        self.doc.selected.append((self.Name, append, mark))
        return True


class Document:
    def __init__(self):
        self.names = ['Original', 'Target', 'Other']
        self.current = 'Original'
        self.activations, self.selected, self.calls = [], [], []
        self.sheets = [Body('SourceSheet', 1)]
        self.solids = [Body('ExistingSolid', 0)]
        self.features = [Feature(self, 'Source', self.sheets)]
        self.ConfigurationManager = SimpleNamespace()
        self.FeatureManager = SimpleNamespace(FeatureBossThicken=self.thicken)
        self.fail_offset_after_create = False
        self.fail_restore = False
        self.wrong_side = False
        self.sync()

    def sync(self):
        self.ConfigurationManager.ActiveConfiguration = SimpleNamespace(Name=self.current)
        self.FirstFeature = self.features[0]
        for left, right in zip(self.features, self.features[1:]):
            left.GetNextFeature = right

    def GetConfigurationNames(self):
        return self.names

    def ShowConfiguration2(self, name):
        self.activations.append(name)
        if name == 'Original' and self.fail_restore:
            return False
        self.current = name
        self.sync()
        return True

    def ClearSelection2(self, value):
        self.selected.clear()

    def GetBodies2(self, kind, visible_only):
        return self.solids if kind == 0 else self.sheets

    def InsertOffsetSurface(self, distance, reverse):
        self.calls.append(('offset', distance, reverse))
        sheet = Body('NativeOffset', 1)
        self.sheets.append(sheet)
        self.features.extend([Feature(self, 'SurfaceFolder'), Feature(self, 'NativeOffset', [sheet],
                                  SimpleNamespace(Distance=distance, Flip=reverse))])
        self.sync()
        if self.fail_offset_after_create:
            raise RuntimeError('native error after creation')

    def thicken(self, *args):
        self.calls.append(('thicken', args, list(self.selected)))
        thickness, direction, face_index, fill, merge, scope, auto = args
        body = Body('NativeThicken', 0)
        self.solids.append(body)
        feature = Feature(self, 'NativeThicken', [body], SimpleNamespace(
            Thickness=thickness, ThicknessSide=direction + int(self.wrong_side), Merge=merge))
        self.features.append(feature)
        self.sync()
        return feature


class SurfacePrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.doc = Document()
        self.guard = patch.object(surface, 'require_document_path', return_value=(object(), self.doc)).start()
        patch.object(surface, 'resolve_ref', side_effect=self.resolve).start()
        patch.object(surface, 'apply_selection', side_effect=lambda doc, spec: len(spec['refs'])).start()
        self.addCleanup(patch.stopall)

    def resolve(self, doc, ref):
        self.assertEqual(doc.current, 'Target')
        if ref != 'f1':
            raise RuntimeError('stale ref')
        return doc.sheets[0].faces[0]

    def offset_args(self, **changes):
        args = dict(expected_document_path='C:/part.SLDPRT', configuration='Target',
                    face_refs=['f1'], offset_mm=0., reverse=False, feature_name='Offset')
        return {**args, **changes}

    def thicken_args(self, **changes):
        args = dict(expected_document_path='C:/part.SLDPRT', configuration='Target',
                    surface_feature_name='Source', thickness_mm=.05, direction='side2',
                    merge=False, feature_name='Thicken')
        return {**args, **changes}

    def test_zero_offset_accepts_void_return_and_ignores_organizational_folder(self):
        out = surface.offset_surface(self.offset_args())
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.doc.calls, [('offset', 0., False)])
        self.assertEqual(self.doc.current, 'Original')
        self.assertEqual(out['data']['sheet_bodies'][0]['area_mm2'], 100.)
        self.assertEqual(out['data']['suppressed'], {'Original': True, 'Target': False, 'Other': True})
        self.assertEqual(out['data']['verification']['unverified_native_fields'], [])

    def test_stale_ref_and_name_collision_refuse_before_native_creation(self):
        for args in [self.offset_args(face_refs=['f9']), self.offset_args(feature_name='Source')]:
            out = surface.offset_surface(args)
            self.assertFalse(out['ok'])
            self.assertFalse(out['data']['may_have_changes'])
            self.assertEqual(self.doc.current, 'Original')
        self.assertEqual(self.doc.calls, [])

    def test_native_exception_after_offset_creation_identifies_remaining_feature(self):
        self.doc.fail_offset_after_create = True
        out = surface.offset_surface(self.offset_args())
        self.assertFalse(out['ok'])
        self.assertTrue(out['data']['may_have_changes'])
        self.assertIn({'kind': 'feature', 'name': 'NativeOffset'}, out['data']['partial_artifacts'])
        self.assertTrue(out['data']['configuration_restored'])
        self.assertFalse(out['data']['rollback_performed'])

    def test_thicken_maps_explicit_parameters_and_reads_back_native_side_merge(self):
        out = surface.thicken_surface(self.thicken_args())
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.doc.calls[0], ('thicken', (.00005, 1, 0, False, False, False, True),
                                            [('Source', False, 1)]))
        self.assertEqual(out['data']['direction'], 'side2')
        self.assertFalse(out['data']['merge'])
        self.assertEqual(out['data']['solid_count_after'], 2)
        self.assertEqual(self.doc.current, 'Original')

    def test_multi_sheet_source_refuses_before_thickening(self):
        self.doc.features[0].bodies.append(Body('SecondSheet', 1))
        out = surface.thicken_surface(self.thicken_args())
        self.assertFalse(out['ok'])
        self.assertEqual(self.doc.calls, [])
        self.assertEqual(self.doc.current, 'Original')

    def test_wrong_native_side_is_partial_failure(self):
        self.doc.wrong_side = True
        out = surface.thicken_surface(self.thicken_args())
        self.assertFalse(out['ok'])
        self.assertIn('ThicknessSide', out['message'])
        self.assertEqual(out['data']['partial_artifacts'], [{'kind': 'feature', 'name': 'Thicken'}])

    def test_configuration_restore_failure_overrides_inner_success(self):
        self.doc.fail_restore = True
        out = surface.offset_surface(self.offset_args())
        self.assertFalse(out['ok'])
        self.assertFalse(out['data']['configuration_restored'])
        self.assertEqual(out['data']['active_configuration'], 'Target')
        self.assertEqual(out['data']['readback']['feature'], 'Offset')

    def test_invalid_parameters_and_wrong_document_do_not_create_features(self):
        for args in [self.offset_args(offset_mm=-1), self.offset_args(reverse=0),
                     self.offset_args(face_refs=['e1']), self.thicken_args(thickness_mm=True)]:
            operation = surface.offset_surface if 'face_refs' in args else surface.thicken_surface
            self.assertFalse(operation(args)['ok'])
        self.guard.side_effect = RuntimeError('wrong document')
        self.assertFalse(surface.offset_surface(self.offset_args())['ok'])
        self.assertEqual(self.doc.calls, [])


if __name__ == '__main__':
    unittest.main()
