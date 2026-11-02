"""Narrow failure/ownership checks. Native geometry is checked by the live fixture."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import server, sw_deform as deform


def request(**updates):
    return {"expected_document_path": "C:/test/part.SLDPRT", "configuration": "Default",
            "body_name": "Body", "curve_pairs": [{"source_sketch": "Source", "target_sketch": "Target"}],
            "shape_accuracy": .75, "fixed_edges": False, "feature_name": "NativeDeform", **updates}


class DeformTests(unittest.TestCase):
    def test_invalid_intent_is_rejected_before_cad(self):
        with patch.object(deform, "require_document_path") as attach:
            for change in ({"shape_accuracy": float("nan")}, {"shape_accuracy": True},
                           {"fixed_edges": 0}, {"curve_pairs": []},
                           {"expected_document_path": "relative.SLDPRT"}):
                self.assertFalse(deform.create_curve_deform(request(**change))["ok"])
            attach.assert_not_called()

    def test_closed_command_can_be_observed_after_switch_but_not_mutated(self):
        owner = deform.Owner(2, 1, "c:/test/a.sldprt", "A - SOLIDWORKS", "34.0")
        with patch.object(deform.win32gui, "IsWindow", return_value=True), \
             patch.object(deform.win32process, "GetWindowThreadProcessId", return_value=(3, 1)), \
             patch.object(deform.win32gui, "GetWindowText", return_value="B - SOLIDWORKS"), \
             patch.object(deform, "_children", return_value=[]), \
             patch.object(deform, "_message") as send:
            panel = deform.DeformPanel(owner)
            self.assertFalse(panel.is_open())
            with self.assertRaisesRegex(RuntimeError, "caption changed"):
                panel.click("cancel")
            send.assert_not_called()

    def transaction(self, configure_error=None, close_error=None, prepare_error=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        model = {"features": [], "bodies": [{"name": "Body", "fault_count": 0, "volume_mm3": 210.0}]}
        panel = Mock()
        panel.is_open.side_effect = [True, False] if not close_error else [True]
        if close_error:
            panel.close.side_effect = close_error
        panel.readback.return_value = {"actual": "native controls"}
        app = SimpleNamespace(RunCommand=Mock(return_value=True))
        doc = SimpleNamespace(ClearSelection2=Mock())
        owner = SimpleNamespace(pid=1, frame=2)
        for name, replacement in {
            "active_native_commands": Mock(return_value=[]),
            "visible_property_managers": Mock(return_value=[]),
            "require_document_path": Mock(return_value=(app, doc)),
            "_prepare": Mock(return_value=(object(), []), side_effect=prepare_error),
            "_model_readback": Mock(return_value=model),
            "_configurations": Mock(return_value=(["Default"], "Default")),
            "DeformPanel": Mock(return_value=panel),
            "_configure": Mock(return_value={"actual": "configured"}, side_effect=configure_error),
            "select_by_id": Mock(return_value=True),
        }.items():
            stack.enter_context(patch.object(deform, name, replacement))
        stack.enter_context(patch.object(deform.Owner, "capture", return_value=owner))
        return app, doc, panel

    def test_configure_failure_cancels_and_reports_native_state(self):
        _, _, panel = self.transaction(configure_error=RuntimeError("wrong source list"))
        payload = deform.create_curve_deform(request())
        self.assertFalse(payload["ok"])
        panel.close.assert_called_once_with(False)
        self.assertFalse(payload["data"]["command_still_open"])
        self.assertTrue(payload["data"]["readback"]["model_unchanged_verified"])
        self.assertEqual(payload["data"]["readback"]["failure_panel"], {"actual": "native controls"})

    def test_failed_cleanup_reports_unknown_without_model_reconciliation(self):
        self.transaction(configure_error=RuntimeError("setup"), close_error=RuntimeError("timeout"))
        payload = deform.create_curve_deform(request())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["data"]["command_still_open"])
        self.assertTrue(payload["data"]["may_have_changes"])
        self.assertEqual(deform._model_readback.call_count, 1)

    def test_name_conflict_does_not_open_or_clear(self):
        app, doc, _ = self.transaction(prepare_error=ValueError("feature already exists"))
        payload = deform.create_curve_deform(request())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["data"]["may_have_changes"])
        app.RunCommand.assert_not_called()
        doc.ClearSelection2.assert_not_called()

    def test_cancel_ignores_mass_roundoff_but_rejects_model_change(self):
        for delta, expected in ((2.842170943040401e-14, True), (0.001, False)):
            with self.subTest(volume_delta=delta):
                _, _, panel = self.transaction()
                panel.is_open.side_effect = None
                panel.is_open.return_value = False
                before = {"features": [], "bodies": [{"name": "Body", "fault_count": 0, "volume_mm3": 210.0}]}
                after = {"features": [], "bodies": [{"name": "Body", "fault_count": 0, "volume_mm3": 210.0 + delta}]}
                deform._model_readback.side_effect = [before, after, after]
                payload = deform.create_curve_deform(request(validate_only=True))
                self.assertEqual(payload["ok"], expected)
                self.assertEqual(payload["data"]["readback"]["model_unchanged_verified"], expected)

    def test_unidentified_recovery_refuses_without_messages(self):
        with patch.object(deform, "active_native_commands", return_value=[]), \
             patch.object(deform, "require_document_path") as attach, patch.object(deform, "_message") as send:
            payload = deform.cancel_solidworks_command({"expected_document_path": "C:/test/part.SLDPRT",
                       "expected_command": "Deform", "expected_pid": 1, "expected_frame": 2})
            self.assertFalse(payload["ok"])
            attach.assert_not_called()
            send.assert_not_called()

    def test_fresh_recovery_checks_full_path_before_native_mutation(self):
        with patch.dict(deform._OWNERS, {}, clear=True), \
             patch.object(deform, "active_native_commands", return_value=[{"pid": 1, "frame": 2, "ambiguous": False}]), \
             patch.object(deform, "require_document_path", side_effect=RuntimeError("Active document mismatch")), \
             patch.object(deform, "inspect_solidworks_ui", return_value={"data": {"frames": []}}), \
             patch.object(deform, "_message") as send:
            payload = deform.cancel_solidworks_command({"expected_document_path": "C:/test/part.SLDPRT",
                       "expected_command": "Deform", "expected_pid": 1, "expected_frame": 2})
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["data"]["stage"], "verifying_document")
            self.assertFalse(payload["data"]["cancellation_attempted"])
            send.assert_not_called()

    def test_inspection_uses_current_caption_after_document_switch(self):
        stale = deform.Owner(2, 1, "c:/test/a.sldprt", "A - SOLIDWORKS", "34.0")
        current_frame = {"pid": 1, "frame": 2, "title": "B - SOLIDWORKS"}
        marker = {"id": 6771, "class": "Button"}
        with patch.dict(deform._OWNERS, {(1, 2): stale}, clear=True), \
             patch.object(deform, "native_frames", return_value=[current_frame]), \
             patch.object(deform, "_children", return_value=[marker]), \
             patch.object(deform.DeformPanel, "readback", autospec=True, return_value={}) as readback, \
             patch.object(deform, "require_document_path") as attach:
            self.assertTrue(deform.inspect_solidworks_ui({})["ok"])
            observed_owner = readback.call_args.args[0].owner
            self.assertEqual(observed_owner.caption, current_frame["title"])
            self.assertEqual(observed_owner.revision, "unverified")
            self.assertEqual(observed_owner.document_path, "")
            self.assertIs(deform._OWNERS[(1, 2)], stale)
            attach.assert_not_called()

    def test_cancellation_failure_preserves_evidence_and_reports_unknown_readback(self):
        args = {"expected_document_path": "C:/test/part.SLDPRT", "expected_command": "Deform",
                "expected_pid": 1, "expected_frame": 2}
        for failure_stage in ("reading_panel", "cancelling", "verifying_closed"):
            with self.subTest(stage=failure_stage):
                panel = Mock()
                panel.is_open.return_value = True
                panel.readback.return_value = {"command": "Deform"}
                if failure_stage == "reading_panel":
                    panel.readback.side_effect = RuntimeError("read timeout")
                elif failure_stage == "cancelling":
                    panel.close.side_effect = RuntimeError("close timeout")
                else:
                    panel.is_open.side_effect = [True, RuntimeError("verification timeout")]
                with patch.object(deform, "active_native_commands", return_value=[{"pid": 1, "frame": 2, "ambiguous": False}]), \
                     patch.object(deform, "require_document_path", return_value=(object(), object())), \
                     patch.object(deform.Owner, "capture", return_value=SimpleNamespace(pid=1, frame=2)), \
                     patch.object(deform, "DeformPanel", return_value=panel), \
                     patch.object(deform, "inspect_solidworks_ui", side_effect=RuntimeError("native timeout")), \
                     patch.object(deform, "_model_readback") as model:
                    payload = server.dispatch("cancel_solidworks_command", args)
                    self.assertFalse(payload["ok"])
                    data = payload["data"]
                    self.assertEqual(data["stage"], failure_stage)
                    self.assertIsNone(data["command_still_open"])
                    self.assertTrue(data["may_have_changes"])
                    self.assertFalse(data["model_unchanged_verified"])
                    self.assertEqual(data["cancellation_attempted"], failure_stage != "reading_panel")
                    self.assertEqual(data["cleanup_errors"][-1]["stage"], "native_reinspection")
                    if failure_stage != "reading_panel":
                        self.assertEqual(data["readback"]["command"], "Deform")
                    else:
                        panel.close.assert_not_called()
                    model.assert_not_called()

    def test_cancellation_failure_uses_fresh_native_open_state(self):
        args = {"expected_document_path": "C:/test/part.SLDPRT", "expected_command": "Deform",
                "expected_pid": 1, "expected_frame": 2}
        panel = Mock()
        panel.readback.return_value = {"command": "Deform"}
        panel.close.side_effect = RuntimeError("timeout")
        for command in ("Deform", None):
            with self.subTest(command=command), \
                 patch.object(deform, "active_native_commands", return_value=[{"pid": 1, "frame": 2, "ambiguous": False}]), \
                 patch.object(deform, "require_document_path", return_value=(object(), object())), \
                 patch.object(deform.Owner, "capture", return_value=SimpleNamespace(pid=1, frame=2)), \
                 patch.object(deform, "DeformPanel", return_value=panel), \
                 patch.object(deform, "inspect_solidworks_ui", return_value={"data": {"frames": [
                     {"pid": 1, "frame": 2, "command": command}]}}):
                data = deform.cancel_solidworks_command(args)["data"]
                self.assertEqual(data["command_still_open"], command == "Deform")
                self.assertFalse(data["model_unchanged_verified"])

    def test_foreground_activation_handles_already_active_without_native_changes(self):
        owner = SimpleNamespace(frame=2, pid=1, check=Mock())
        with patch.object(deform.win32gui, "GetForegroundWindow", return_value=2), \
             patch.object(deform.win32gui, "ShowWindow") as restore, \
             patch.object(deform.win32gui, "SetForegroundWindow") as activate, \
             patch.object(deform.win32process, "AttachThreadInput") as attach:
            self.assertTrue(deform._activate_frame(owner)["foreground"])
            restore.assert_not_called()
            activate.assert_not_called()
            attach.assert_not_called()

    def test_foreground_activation_retries_with_thread_queues_and_always_detaches(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                state = {"foreground": 9}
                def activate():
                    if activate.calls == 0 or not succeeds:
                        activate.calls += 1
                        raise RuntimeError("SetForegroundWindow refused")
                    state["foreground"] = 2
                activate.calls = 0
                owner = SimpleNamespace(frame=2, pid=1, check=Mock())
                with patch.object(deform.win32gui, "GetForegroundWindow", side_effect=lambda: state["foreground"]), \
                     patch.object(deform.win32gui, "ShowWindow"), \
                     patch.object(deform.win32gui, "SetForegroundWindow", side_effect=lambda hwnd: activate()) as set_foreground, \
                     patch.object(deform.win32gui, "BringWindowToTop"), \
                     patch.object(deform.win32gui, "PeekMessage"), \
                     patch.object(deform.win32api, "GetCurrentThreadId", return_value=10), \
                     patch.object(deform.win32process, "GetWindowThreadProcessId", side_effect=lambda hwnd: (20 if hwnd == 2 else 30, 1)), \
                     patch.object(deform.win32process, "AttachThreadInput") as attach:
                    data = deform._activate_frame(owner)
                    self.assertEqual(data["foreground"], succeeds)
                    self.assertEqual(set_foreground.call_count, 2)
                    self.assertEqual([call.args for call in attach.call_args_list],
                                     [(10, 30, True), (10, 20, True), (10, 20, False), (10, 30, False)])
                    self.assertTrue(any("error" in attempt for attempt in data["attempts"]))
                    self.assertEqual(data["cleanup_errors"], [])

    def test_dispatch_refuses_open_panel_before_com_or_snapshot(self):
        with patch.object(deform, "active_native_commands", return_value=[{"command": "Deform"}]), \
             patch.object(server, "running_app") as attach, \
             patch.object(server, "_safe_light_snapshot") as snapshot:
            payload = server.dispatch("boss_extrude", {})
            self.assertFalse(payload["ok"])
            attach.assert_not_called()
            snapshot.assert_not_called()

    def test_ui_inspection_bypasses_com_and_snapshots(self):
        with patch.object(deform, "active_native_commands", return_value=[{"command": "Deform"}]), \
             patch.dict(server.HANDLERS, {"inspect_solidworks_ui": lambda args: {"ok": True}}), \
             patch.object(server, "running_app") as attach, \
             patch.object(server, "_safe_light_snapshot") as snapshot:
            self.assertTrue(server.dispatch("inspect_solidworks_ui", {})["ok"])
            attach.assert_not_called()
            snapshot.assert_not_called()

    def test_creation_refuses_unrelated_panel_before_snapshot(self):
        with patch.object(deform, "active_native_commands", return_value=[]), \
             patch.object(deform, "visible_property_managers", return_value=[{"property_manager": "Fillet"}]), \
             patch.object(server, "running_app") as attach, \
             patch.object(server, "_safe_light_snapshot") as snapshot:
            self.assertFalse(server.dispatch("create_curve_deform", request())["ok"])
            attach.assert_not_called()
            snapshot.assert_not_called()

    def test_left_open_panel_skips_after_snapshot(self):
        with patch.object(deform, "active_native_commands", side_effect=[[], [{"command": "Deform"}]]), \
             patch.dict(server.HANDLERS, {"test_leaves_panel_open": lambda args: {"ok": True}}), \
             patch.object(server, "running_app"), patch.object(server, "WATCHDOG") as watchdog, \
             patch.object(server, "blocked_report", return_value=None), \
             patch.object(server, "_safe_light_snapshot", return_value={"doc": {}}) as snapshot:
            watchdog.end_call.return_value = []
            payload = server.dispatch("test_leaves_panel_open", {})
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["command_still_open"])
            self.assertEqual(snapshot.call_count, 1)


if __name__ == "__main__":
    unittest.main()
