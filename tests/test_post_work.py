from pathlib import Path
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'codex_hooks/coverage_audit.py'


class PostWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'tools/solidworks-mcp/solidworks_mcp/ops.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_text("raise RuntimeError('never import')\n@tool('inspect', 'Inspect')\ndef inspect(args): pass\n", encoding='utf-8')
        self.hook('UserPromptSubmit')
        self.context = next((self.root / 'outputs/mcp-coverage-audit').glob('*.json'))

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return str(path)

    def cli(self, *args, event=None, ok=True):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--workspace', str(self.root), *args],
            input=json.dumps(event) if event else None, encoding='utf-8', capture_output=True, timeout=10)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            # Historical post-work receipts retain their recorded version-1 semantics.
            if event and event.get('hook_event_name') == 'UserPromptSubmit':
                for path in (self.root/'outputs/mcp-coverage-audit').glob('*.json'):
                    state = json.loads(path.read_text(encoding='utf-8'))
                    state['version'] = 1
                    path.write_text(json.dumps(state), encoding='utf-8')
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def hook(self, kind, code='', name='functions.exec', identity='ui1', **extra):
        event = {'session_id': 's1', 'turn_id': 't1', 'cwd': str(self.root), 'hook_event_name': kind,
                 'tool_name': name, 'tool_use_id': identity, 'tool_input': {'code': code}, **extra}
        return self.cli('hook', event=event)

    def state(self, context=None):
        return self.cli('status', '--context', str(context or self.context))

    def note(self, **extra):
        note = {'operation': 'Inspect SolidWorks selection', 'reason': 'Visual state inspection',
                'kind': 'inspection', 'route': 'explicit', 'event_ids': ['source1'], **extra}
        return self.cli('fallback-note', '--context', str(self.context), '--note', self.file('note.json', note))

    def receipt(self, findings=None, **extra):
        receipt = {'postwork_digest': self.state()['postwork_digest'], 'job_state': 'completed',
                   'findings': findings if findings is not None else [], **extra}
        return receipt

    def submit(self, receipt, ok=True):
        return self.cli('post-work', '--context', str(self.context), '--receipt', self.file('receipt.json', receipt), ok=ok)

    def one_off(self):
        return {'fallback_id': self.state()['fallbacks'][0]['id'], 'classification': 'one_off',
                'outcome': 'no_change', 'reason': 'Single visual confirmation does not justify an API primitive.'}

    def test_supported_executable_routes_ignore_literals_patches_comments_definitions_and_other_apps(self):
        self.assertEqual(self.hook('PreToolUse', "await tab.getSnapshot();", name='mcp__cua_repl__js'), {})
        confirmed = self.hook('PreToolUse', "await cua.getApp('SolidWorks');", name='mcp__cua_repl__js')
        self.assertEqual(confirmed['hookSpecificOutput']['permissionDecision'], 'deny')
        positive = "await tools.mcp__cua_repl__js({code: \"let app = await cua.getApp('SolidWorks');\"})"
        negatives = [json.dumps(positive), '// ' + positive, '/* ' + positive + ' */',
            'const fixture = {code: ' + json.dumps(positive) + '};',
            'function never() { ' + positive + ' }', 'const never = async () => ' + positive + ';',
            'if (false) ' + positive, 'await tools.apply_patch(' + json.dumps(positive) + ');',
            "await tools.exec_command({cmd: " + json.dumps(positive) + '});',
            "await tools.mcp__cua_repl__js({code: \"await cua.getApp('Excel');\"});",
            "await tools.mcp__node_repl__js({code: \"await sky.click({app:'Excel',text:'SolidWorks'});\"});",
            'const pattern = /await sky.click()/;']
        for i, code in enumerate(negatives):
            self.hook('PostToolUse', code, identity=f'negative{i}')
        self.assertEqual(self.state().get('fallbacks', []), [])
        self.hook('PostToolUse', positive)
        state = self.state()
        self.assertEqual(len(state['fallbacks']), 1)
        self.assertEqual(state['fallbacks'][0]['attribution'], 'confirmed')
        self.assertEqual(state['events'], [])
        sky = "await tools.mcp__node_repl__js({code: \"const {sky} = await import('@oai/sky'); await sky.get_window_state({app:'SolidWorks'});\"});"
        self.hook('PostToolUse', sky, identity='sky1')
        self.hook('PostToolUse', "await tools.mcp__node_repl__js({code:'await sky.click({window:cachedWindow});'});", identity='sky2')
        self.assertEqual([f['attribution'] for f in self.state()['fallbacks']], ['confirmed', 'confirmed', 'candidate'])
        self.hook('PostToolUse', "await cua.getApp('SolidWorks');", name='mcp__cua_repl__js', identity='direct')
        self.assertEqual(len(self.state()['fallbacks']), 4)
        self.hook('PostToolUse', "await tools.mcp__node_repl__js({code: \"await sky.type_text({app:'SolidWorks',text:'PRIVATE_TYPED_TEXT'});\"});", identity='typed',
                  tool_response={'screenshot': 'PRIVATE_PIXELS', 'tree': 'PRIVATE_UI_TREE'})
        persisted = self.context.read_text(encoding='utf-8')
        for private in ('PRIVATE_TYPED_TEXT', 'PRIVATE_PIXELS', 'PRIVATE_UI_TREE'):
            self.assertNotIn(private, persisted)

    def test_explicit_note_semantic_idempotency_combined_stop_and_honest_ui_pending(self):
        first = self.note(timestamp='first')
        self.assertFalse(self.note(timestamp='second', operation=' Inspect  SolidWorks selection ')['changed'])
        self.assertEqual(self.state()['postwork_digest'], first['postwork_digest'])
        self.assertEqual(self.hook('Stop')['decision'], 'block')
        second = self.hook('Stop', stop_hook_active=True)
        self.assertNotIn('decision', second)
        self.assertIn('needs_review', second['systemMessage'])
        self.assertEqual(self.state()['improvement_status'], 'needs_review')
        self.assertFalse((self.root / 'tickets').exists())
        self.assertEqual(self.hook('Stop'), {})
        self.assertFalse(self.note(timestamp='third')['changed'])
        self.assertEqual(self.hook('Stop'), {})
        self.note(operation='Inspect a different selection')
        self.assertEqual(self.state()['improvement_status'], 'pending')
        self.assertEqual(self.hook('Stop')['decision'], 'block')

    def test_defer_main_job_then_remind_once_without_resetting_old_stop_budget(self):
        self.note()
        self.hook('PostToolUse', name='mcp__solidworks__inspect', identity='cad1')
        combined = self.hook('Stop')
        self.assertEqual(combined['decision'], 'block')
        self.assertIn('combined', combined['reason'])
        self.submit(self.receipt(job_state='paused', reason='The CAD task is paused for a higher priority job.'))
        self.assertEqual(self.hook('Stop'), {})
        self.hook('UserPromptSubmit', turn_id='t2')
        remind = self.hook('PostToolUse', name='mcp__solidworks__inspect', identity='cad2', turn_id='t2')
        self.assertIn('deferred', remind['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.hook('PostToolUse', name='mcp__solidworks__inspect', identity='cad2', turn_id='t2'), {})
        self.assertEqual(self.state()['improvement_stop_requests'], 1)
        self.assertEqual(self.state()['improvement_status'], 'deferred')
        self.assertEqual(self.state()['coverage_status'], 'pending')
        stop = self.hook('Stop', turn_id='t2')
        self.assertEqual(stop['decision'], 'block')
        self.assertNotIn(str(self.context), stop['reason'])
        self.hook('Stop', turn_id='t2', stop_hook_active=True)
        self.assertEqual(self.hook('Stop'), {})

    def evidence(self, outcome='fixed'):
        state = self.state()
        cat = self.cli('catalog')
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        relative = str(self.source.relative_to(self.root)).replace('\\', '/')
        hashes = {relative: source_hash}
        live = {'postwork_digest': state['postwork_digest'], 'tool': 'inspect', 'availability': 'available',
                'evidence': 'Connected tool was inspected after reload.'}
        verify = {'postwork_digest': state['postwork_digest'], 'tool': 'inspect', 'route': 'live_tool',
            'status': 'passed', 'source_hashes': hashes, 'capability': 'Inspect selected body',
            'invocation': 'Called the connected inspect tool on a copied test part.',
            'before_behavior': 'Selection was lost.', 'observed_behavior': 'Selection is returned.'}
        finding = {'fallback_id': state['fallbacks'][0]['id'], 'classification': 'bug', 'outcome': outcome,
            'reason': 'Preserve selection inspection through the existing primitive.',
            'benefit': 'Avoid repeated visual checks.', 'evidence': 'Reproduced selection omission on a copied part.',
            'source_check': {'tool': 'inspect', 'catalog_digest': cat['digest'], 'source': relative, 'sha256': source_hash},
            'live_check': self.file('live.json', live), 'verification': self.file('verification.json', verify),
            'changed_sources': [{'path': relative, 'sha256': source_hash, 'before_sha256': '0' * 64}]}
        return finding, live, verify

    def test_reasoned_deferral_suppresses_coverage_only_context_stop(self):
        self.hook('PostToolUse', name='mcp__solidworks__inspect', identity='cad1')
        self.assertEqual(self.state()['coverage_status'], 'pending')
        self.assertEqual(self.state()['improvement_status'], 'not_required')
        deferred = self.receipt(job_state='unfinished', reason='The requested CAD work is still unfinished.')
        self.assertEqual(self.submit(deferred)['status'], 'deferred')
        self.assertEqual(self.state()['improvement_status'], 'deferred')
        self.assertEqual(self.hook('Stop'), {})
        self.assertEqual(self.hook('Stop', stop_hook_active=True), {})
        self.assertEqual(self.state()['coverage_status'], 'pending')
        self.hook('PostToolUse', name='mcp__solidworks__inspect', identity='cad2')
        self.assertEqual(self.state()['improvement_status'], 'not_required')
        self.assertEqual(self.hook('Stop')['decision'], 'block')

    def test_receipt_rejects_invented_ids_stale_digest_hashes_and_false_live_completion(self):
        self.note()
        finding, live, verify = self.evidence()
        for field, value in [('fallback_id', 'invented'), ('verification', str(self.root / 'absent.json'))]:
            invalid = copy.deepcopy(finding)
            invalid[field] = value
            self.submit(self.receipt([invalid]), ok=False)
        invalid = copy.deepcopy(finding)
        invalid['changed_sources'][0]['sha256'] = '1' * 64
        self.submit(self.receipt([invalid]), ok=False)
        invalid = copy.deepcopy(finding)
        invalid['source_check']['sha256'] = '1' * 64
        self.submit(self.receipt([invalid]), ok=False)
        self.submit(self.receipt([finding], postwork_digest='stale'), ok=False)
        for field, value in [('route', 'offline_test'), ('status', 'failed'), ('source_hashes', {}),
                             ('tool', 'inventory'), ('observed_behavior', verify['before_behavior'])]:
            invalid = {**verify, field: value}
            self.file('verification.json', invalid)
            self.submit(self.receipt([finding]), ok=False)
        self.file('verification.json', verify)
        self.file('live.json', {**live, 'availability': 'stale'})
        self.submit(self.receipt([finding]), ok=False)
        pending = {**finding, 'outcome': 'source_changed_pending_live'}
        self.assertEqual(self.submit(self.receipt([pending]))['status'], 'pending_live')
        self.assertEqual(self.state()['improvement_status'], 'pending_live')
        self.file('live.json', live)
        self.assertEqual(self.submit(self.receipt([finding]))['status'], 'reviewed')
        self.assertEqual(self.hook('Stop'), {})

    def test_source_existing_stale_oneoff_unknown_have_separate_honest_outcomes(self):
        self.note()
        finding, live, _ = self.evidence()
        for classification, outcome, availability, status in [
                ('existing', 'covered', 'available', 'reviewed'),
                ('stale_live', 'pending_live', 'stale', 'pending_live'),
                ('one_off', 'no_change', 'available', 'reviewed'),
                ('unknown', 'needs_review', 'available', 'needs_review')]:
            self.file('live.json', {**live, 'availability': availability})
            review = {**finding, 'classification': classification, 'outcome': outcome,
                      'next_step': 'Check whether live selection metadata is complete.'}
            result = self.submit(self.receipt([review]))
            self.assertEqual(result['status'], status)
            state = self.state()
            self.assertEqual(state['coverage_status'], 'not_required')
            self.assertEqual(state['improvement_status'], status)
        wrong = {**finding, 'classification': 'one_off', 'outcome': 'extended'}
        self.submit(self.receipt([wrong]), ok=False)
        self.submit(self.receipt([self.one_off()]))
        self.assertEqual(self.hook('Stop'), {})


if __name__ == '__main__':
    unittest.main()
