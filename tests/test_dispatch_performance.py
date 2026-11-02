from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import server


class DispatchPerformanceTests(unittest.TestCase):
    def test_scoped_tools_skip_global_snapshots_and_preserve_diff(self):
        payload = {'ok': True, 'diff': 'local change', 'diff_scope': 'sketch', 'data': {'before': {}, 'after': {}}}
        with patch.dict(server.HANDLERS, {name: Mock(return_value=payload.copy()) for name in server.SCOPED_SKETCH_TOOLS}), \
             patch.object(server, '_safe_light_snapshot') as snapshot, \
             patch.object(server.sw_deform, 'active_native_commands', return_value=[]) as commands, \
             patch.object(server, 'running_app'), patch.object(server, 'blocked_report', return_value=None), \
             patch.object(server.WATCHDOG, 'configure'), patch.object(server.WATCHDOG, 'begin_call'), \
             patch.object(server.WATCHDOG, 'end_call', return_value=[]):
            for name in server.SCOPED_SKETCH_TOOLS:
                response = server._dispatch_locked(name, {})
                self.assertEqual(response['diff'], 'local change')
                self.assertEqual(response['diff_scope'], 'sketch')
                self.assertEqual(set(response['timing_ms']), {'guards', 'before_snapshot', 'handler', 'after_snapshot', 'total'})
                self.assertGreaterEqual(response['timing_ms']['total'], 0)
            snapshot.assert_not_called()
            self.assertEqual(commands.call_count, 2 * len(server.SCOPED_SKETCH_TOOLS))

    def test_script_keeps_global_snapshots(self):
        model = {'doc': {'title': 'test'}}
        with patch.dict(server.HANDLERS, {'run_script': Mock(return_value={'ok': True})}), \
             patch.object(server, 'AUTO_DIFF', True), patch.object(server, '_safe_light_snapshot', return_value=model) as snapshot, \
             patch.object(server, 'diff_snapshots', return_value='global change'), \
             patch.object(server.sw_deform, 'active_native_commands', return_value=[]), \
             patch.object(server, 'running_app'), patch.object(server, 'blocked_report', return_value=None), \
             patch.object(server.WATCHDOG, 'configure'), patch.object(server.WATCHDOG, 'begin_call'), \
             patch.object(server.WATCHDOG, 'end_call', return_value=[]):
            response = server._dispatch_locked('run_script', {'read_only': True})
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual(response['diff'], 'global change')

    def test_active_command_blocks_scoped_handler(self):
        handler = Mock()
        with patch.dict(server.HANDLERS, {'set_dimensions': handler}), \
             patch.object(server.sw_deform, 'active_native_commands', return_value=[{'name': 'Deform'}]), \
             patch.object(server, '_safe_light_snapshot') as snapshot:
            response = server._dispatch_locked('set_dimensions', {})
            self.assertFalse(response['ok'])
            self.assertTrue(response['data']['command_still_open'])
            self.assertIn('timing_ms', response)
            handler.assert_not_called()
            snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
