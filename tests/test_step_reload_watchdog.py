from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from uuid import uuid4
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import sw_file, sw_watchdog


@contextmanager
def input_file(suffix, contents=b''):
    path = Path(__file__).resolve().parents[3] / 'scratch' / ('step-reload-test-' + uuid4().hex + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def part(path, *, dirty=False, title=None):
    return SimpleNamespace(GetPathName=str(path), GetTitle=title or Path(path).name,
                           GetSaveFlag=dirty, GetType=1)


class DocumentImportAndReloadTests(unittest.TestCase):
    def test_step_import_checks_collision_before_loading(self):
        with input_file('.step', b"#1=PRODUCT('part-123','Part',(#2));") as path:
            existing = part(path.parent / 'part-123.step.SLDPRT', dirty=True)
            app = SimpleNamespace(GetDocuments=lambda: [existing], LoadFile4=Mock())
            with patch.object(sw_file, 'running_app', return_value=app):
                result = sw_file.open_document({'path': str(path)})
            self.assertFalse(result['ok'])
            self.assertEqual(result['data']['conflicting_documents'], [existing.GetTitle])
            app.LoadFile4.assert_not_called()

    def test_step_import_refuses_unknown_identities_with_open_documents(self):
        with input_file('.step', b'not a recognizable STEP product record') as path:
            app = SimpleNamespace(GetDocuments=lambda: [part(path.parent / 'other.SLDPRT')], LoadFile4=Mock())
            with patch.object(sw_file, 'running_app', return_value=app):
                result = sw_file.open_document({'path': str(path)})
            self.assertFalse(result['ok'])
            app.LoadFile4.assert_not_called()

    def test_step_x2_name_detects_japanese_open_document_collision(self):
        with input_file('.step', rb"#1=PRODUCT('id-123','\X2\677F30D030CD\X0\',(#2));") as path:
            existing = part(path.parent / '板バネ.step.SLDPRT', dirty=True)
            app = SimpleNamespace(GetDocuments=lambda: [existing], LoadFile4=Mock())
            with patch.object(sw_file, 'running_app', return_value=app):
                outcome = sw_file.open_document({'path': str(path)})
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['data']['conflicting_documents'], [existing.GetTitle])
            app.LoadFile4.assert_not_called()

    def test_step_japanese_collision_accepts_compatibility_widths(self):
        with input_file('.step', rb"#1=PRODUCT('id-123','\X2\677F30D030CD\X0\',(#2));") as path:
            existing = part(path.parent / '板ﾊﾞﾈ.step.SLDPRT', dirty=True)
            app = SimpleNamespace(GetDocuments=lambda: [existing], LoadFile4=Mock())
            with patch.object(sw_file, 'running_app', return_value=app):
                outcome = sw_file.open_document({'path': str(path)})
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['data']['conflicting_documents'], [existing.GetTitle])
            app.LoadFile4.assert_not_called()

    def test_step_x4_name_allows_distinct_open_document(self):
        with input_file('.step', rb"#1=PRODUCT('id-123','\X4\000090E8000054C1\X0\',(#2));") as path:
            existing = part(path.parent / '別物.SLDPRT')
            imported = part(path.parent / 'imported.SLDPRT')
            app = SimpleNamespace(GetDocuments=lambda: [existing], GetImportFileData=lambda path: object(),
                                  LoadFile4=Mock(return_value=imported), ActiveDoc=imported,
                                  IsSame=lambda left, right: 1 if left is right else 2)
            with patch.object(sw_file, 'running_app', return_value=app):
                outcome = sw_file.open_document({'path': str(path)})
            self.assertTrue(outcome['ok'])
            app.LoadFile4.assert_called_once()

    def test_step_malformed_or_unsupported_escape_is_refused(self):
        for encoded in (b'\\X2\\006\\X0\\', b'\\X4\\0000D800\\X0\\', b'\\S\\A'):
            with self.subTest(encoded=encoded), input_file('.step', b"#1=PRODUCT('id','" + encoded + b"',(#2));") as path:
                app = SimpleNamespace(GetDocuments=lambda: [part(path.parent / 'other.SLDPRT')], LoadFile4=Mock())
                with patch.object(sw_file, 'running_app', return_value=app):
                    outcome = sw_file.open_document({'path': str(path)})
                self.assertFalse(outcome['ok'])
                app.LoadFile4.assert_not_called()

    def test_step_import_uses_returned_document_identity(self):
        with input_file('.step', b"#1=PRODUCT('part-123','Part',(#2));") as path:
            imported = part(path.parent / 'imported.SLDPRT')
            app = SimpleNamespace(GetDocuments=lambda: [part(path.parent / 'other.SLDPRT')],
                                  GetImportFileData=lambda path: object(),
                                  LoadFile4=Mock(return_value=imported), ActiveDoc=imported,
                                  IsSame=lambda left, right: 1 if left is right else 2)
            with patch.object(sw_file, 'running_app', return_value=app):
                result = sw_file.open_document({'path': str(path)})
            self.assertTrue(result['ok'])
            self.assertEqual(result['data']['source_path'], str(path.resolve()))
            self.assertEqual(result['data']['document']['title'], 'imported.SLDPRT')
            app.LoadFile4.assert_called_once()

    def test_dirty_reload_is_refused_without_calling_reload(self):
        with input_file('.SLDPRT') as path:
            doc = part(path, dirty=True)
            doc.Extension = SimpleNamespace(ReloadOrReplace=Mock())
            with patch.object(sw_file, 'running_app', return_value=SimpleNamespace(ActiveDoc=doc)):
                result = sw_file.open_document({'path': str(path), 'reload': True})
            self.assertFalse(result['ok'])
            doc.Extension.ReloadOrReplace.assert_not_called()

    def test_clean_reload_preserves_read_only_and_never_discards(self):
        with input_file('.SLDPRT') as path:
            doc = part(path)
            doc.IsOpenedReadOnly = lambda: True
            reload = Mock(return_value=0)
            doc.Extension = SimpleNamespace(ReloadOrReplace=reload)
            with patch.object(sw_file, 'running_app', return_value=SimpleNamespace(ActiveDoc=doc)):
                result = sw_file.open_document({'path': str(path), 'reload': True})
            self.assertTrue(result['ok'])
            reload.assert_called_once_with(True, '', False, True)

    def test_reload_refuses_to_touch_a_different_active_document(self):
        with input_file('.SLDPRT') as path:
            doc = part(path.with_name('other.SLDPRT'))
            doc.Extension = SimpleNamespace(ReloadOrReplace=Mock())
            with patch.object(sw_file, 'running_app', return_value=SimpleNamespace(ActiveDoc=doc)):
                result = sw_file.open_document({'path': str(path), 'reload': True})
            self.assertFalse(result['ok'])
            doc.Extension.ReloadOrReplace.assert_not_called()

    def test_reload_reacquires_requested_part_when_solidworks_activates_virtual_copy(self):
        with input_file('.SLDPRT') as path:
            original = part(path)
            original.IsOpenedReadOnly = lambda: False
            reloaded = part(path)
            virtual = part(path.parent / 'virtual-copy.SLDPRT')
            app = SimpleNamespace(ActiveDoc=original, GetDocuments=lambda: [reloaded, virtual],
                                  IsSame=lambda a, b: 1 if a is b else 2)

            def reload_part(*args):
                app.ActiveDoc = virtual
                return 0

            def activate(title, preferences, option, errors):
                app.ActiveDoc = reloaded
                return reloaded

            original.Extension = SimpleNamespace(ReloadOrReplace=Mock(side_effect=reload_part))
            app.ActivateDoc3 = Mock(side_effect=activate)
            with patch.object(sw_file, 'running_app', return_value=app):
                outcome = sw_file.open_document({'path': str(path), 'reload': True})
            self.assertTrue(outcome['ok'])
            self.assertTrue(outcome['data']['reloaded'])
            self.assertIs(app.ActiveDoc, reloaded)
            app.ActivateDoc3.assert_called_once()
            self.assertEqual(app.ActivateDoc3.call_args.args[:3], (reloaded.GetTitle, False, 1))

    def test_reload_reports_partial_success_if_exact_part_cannot_be_activated(self):
        with input_file('.SLDPRT') as path:
            original = part(path)
            original.IsOpenedReadOnly = lambda: False
            reloaded = part(path)
            virtual = part(path.parent / 'virtual-copy.SLDPRT')
            app = SimpleNamespace(ActiveDoc=original, GetDocuments=lambda: [reloaded, virtual],
                                  IsSame=lambda a, b: 1 if a is b else 2,
                                  ActivateDoc3=lambda *args: virtual)

            def reload_part(*args):
                app.ActiveDoc = virtual
                return 0

            original.Extension = SimpleNamespace(ReloadOrReplace=reload_part)
            with patch.object(sw_file, 'running_app', return_value=app):
                outcome = sw_file.open_document({'path': str(path), 'reload': True})
            self.assertFalse(outcome['ok'])
            self.assertTrue(outcome['data']['reloaded'])
            self.assertFalse(outcome['data']['activated'])
            self.assertIs(app.ActiveDoc, virtual)


class ProgressDialogTests(unittest.TestCase):
    def test_open_progress_is_not_closed_and_warning_gets_own_grace_period(self):
        watchdog = sw_watchdog.DialogWatchdog()
        watchdog._active.set()
        progress = {'hwnd': 1, 'title': 'Open Progress...', 'text': 'ｷｬﾝｾﾙ | ﾓﾃﾞﾙの読み取り'}
        warning = {'hwnd': 2, 'title': 'Warning', 'text': 'Confirm'}
        watchdog.is_blocked = lambda: True
        watchdog.modal_windows = Mock(side_effect=[[progress], [progress, warning], [progress, warning]])
        watchdog.dismiss = Mock()
        sleeps = []

        def tick(_):
            sleeps.append(1)
            if len(sleeps) == 3:
                watchdog._stop.set()

        with patch.object(sw_watchdog, '_TIMEOUT_S', 8), patch.object(sw_watchdog, '_AUTO_DISMISS', True), \
             patch.object(sw_watchdog.time, 'monotonic', side_effect=[0.0, 9.0, 18.0]), \
             patch.object(sw_watchdog.time, 'sleep', side_effect=tick):
            watchdog._loop()
        watchdog.dismiss.assert_called_once_with(2, 'close')
        self.assertEqual([event['title'] for event in watchdog.events], ['Warning'])
        self.assertEqual(watchdog.events[0]['waited_s'], 9.0)

    def test_unlabelled_file_open_progress_is_not_a_dialog(self):
        self.assertTrue(sw_watchdog._is_open_progress({'title': '', 'text': '28/52 のﾌｧｲﾙを開いています | 経過時間: 24 秒'}))
        self.assertFalse(sw_watchdog._is_open_progress({'title': 'Warning', 'text': 'ファイルを開いています'}))


if __name__ == '__main__':
    unittest.main()
