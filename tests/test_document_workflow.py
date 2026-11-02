"""Regressions from the sheet-drawing workflow; no COM attachment."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import sw_core, sw_file, sw_script


class DocumentWorkflowTests(unittest.TestCase):
    def test_script_can_pass_tool_name_and_name_arguments(self):
        app = object()
        with patch.object(sw_script, 'running_app', return_value=app), \
             patch.object(sw_script, 'active_document', side_effect=RuntimeError('no doc')), \
             patch.dict(sw_script.HANDLERS, {'example': lambda args: args}):
            env = sw_script._build_env()
            self.assertEqual(env['call']('example', name='Plane1', tool_name='data'),
                             {'name': 'Plane1', 'tool_name': 'data'})

    def test_run_script_forwards_plan_data(self):
        with patch.object(sw_script, 'execute', return_value={'ok': True}) as execute:
            sw_script.run_script({'code': 'result=params["plan"]', 'params': {'plan': {'version': 1}}})
        self.assertEqual(execute.call_args.args[2], {'plan': {'version': 1}})

    def test_script_params_cannot_replace_the_dispatcher(self):
        with patch.object(sw_script, 'running_app', return_value=object()), \
             patch.object(sw_script, 'active_document', side_effect=RuntimeError('no doc')):
            with self.assertRaisesRegex(ValueError, 'shadow'):
                sw_script._build_env({'call': 'not a function'})

    def test_nested_dialog_event_is_visible_without_consuming_outer_events(self):
        from solidworks_mcp.sw_watchdog import WATCHDOG
        original = list(WATCHDOG.events)
        def handler(args):
            WATCHDOG.events.append({'title': 'Warning'})
            return {'ok': True}
        try:
            WATCHDOG.events = []
            with patch.object(sw_script, 'running_app', return_value=object()), \
                 patch.object(sw_script, 'active_document', side_effect=RuntimeError('no doc')), \
                 patch('solidworks_mcp.sw_watchdog.blocked_report', return_value=None), \
                 patch.dict(sw_script.HANDLERS, {'dialog_example': handler}):
                out = sw_script._build_env()['call']('dialog_example')
            self.assertEqual(out['dialog'], [{'title': 'Warning'}])
            self.assertEqual(WATCHDOG.events, out['dialog'])
        finally:
            WATCHDOG.events = original

    def test_guard_rejects_different_active_document(self):
        doc = SimpleNamespace(GetPathName=r'C:\work\other.SLDPRT')
        with patch.object(sw_core, 'active_document', return_value=(object(), doc)):
            with self.assertRaisesRegex(RuntimeError, 'Active document mismatch'):
                sw_core.require_document_path(r'C:\work\target.SLDPRT', 1)

    def test_guard_accepts_case_and_slash_variation(self):
        doc = SimpleNamespace(GetPathName=r'C:\WORK\target.sldprt', GetType=1)
        app = object()
        with patch.object(sw_core, 'active_document', return_value=(app, doc)):
            self.assertEqual(sw_core.require_document_path('c:/work/target.SLDPRT', 1), (app, doc))

    def test_unsaved_document_key_requires_empty_path_and_exact_title(self):
        doc = SimpleNamespace(GetPathName='', GetTitle='Draw1 - Sheet1', GetType=3)
        with patch.object(sw_core, 'active_document', return_value=(object(), doc)):
            self.assertIs(sw_core.require_document_path('untitled:Draw1 - Sheet1', 3)[1], doc)
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                sw_core.require_document_path('untitled:Draw2 - Sheet1', 3)

    def test_open_does_not_claim_wrong_active_document_succeeded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'target.SLDPRT'
            path.touch()
            target = SimpleNamespace(GetTitle='target.SLDPRT', GetPathName=str(path), GetType=1, GetSaveFlag=True)
            other = SimpleNamespace(GetTitle='other.SLDPRT', GetPathName=str(path.with_name('other.SLDPRT')),
                                    GetType=1, GetSaveFlag=True)
            app = SimpleNamespace(OpenDoc6=lambda *a: target, ActivateDoc3=lambda *a: target, ActiveDoc=other)
            with patch.object(sw_file, 'running_app', return_value=app):
                out = sw_file.open_document({'path': str(path)})
            self.assertFalse(out['ok'])
            self.assertEqual(out['data']['active_document']['title'], 'other.SLDPRT')

    def test_zero_arg_value_supports_method_and_property(self):
        self.assertEqual(sw_core.value(SimpleNamespace(GetTitle='x'), 'GetTitle'), 'x')
        self.assertEqual(sw_core.value(SimpleNamespace(GetTitle=lambda: 'x'), 'GetTitle'), 'x')

    def test_deferred_package_imports_are_resolvable(self):
        import ast
        import importlib.util
        package = Path(sw_core.__file__).parent
        for source in package.glob('*.py'):
            tree = ast.parse(source.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'sw_core':
                    name = importlib.util.resolve_name('.' * node.level + node.module, 'solidworks_mcp')
                    with self.subTest(file=source.name):
                        self.assertIsNotNone(importlib.util.find_spec(name))


if __name__ == '__main__':
    unittest.main()
