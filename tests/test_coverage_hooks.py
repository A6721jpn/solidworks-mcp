"""Native hook/CLI protocol tests in disposable projects; no CAD imports."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'codex_hooks/coverage_audit.py'


class CoverageHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        package = self.root / 'tools/solidworks-mcp/solidworks_mcp'
        package.mkdir(parents=True)
        (package/'ops.py').write_text("raise RuntimeError('do not import')\n"
            "@tool('list_faces', 'Read faces')\ndef faces(args): pass\n"
            "@tool('offset_surface', 'Create one surface')\ndef offset(args): pass\n"
            "@tool('run_script', 'Run code')\ndef run(args): pass\n", encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, event=None, ok=True):
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--workspace', str(self.root), *args],
                              input=json.dumps(event) if event else None, text=True, encoding='utf-8',
                              capture_output=True, timeout=10)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # This suite exercises recorded version-1 compatibility explicitly.
            if event and event.get('hook_event_name') == 'UserPromptSubmit':
                for path in (self.root/'outputs/mcp-coverage-audit').glob('*.json'):
                    state = json.loads(path.read_text(encoding='utf-8'))
                    state['version'] = 1
                    path.write_text(json.dumps(state), encoding='utf-8')
            return json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        return proc

    def event(self, kind, identity='call1', **extra):
        return {'session_id': 'session1', 'turn_id': 'turn1', 'cwd': str(self.root),
                'hook_event_name': kind, 'tool_name': 'mcp__solidworks__run_script',
                'tool_use_id': identity, 'tool_input': {'code': "call('list_faces')"}, **extra}

    def file(self, name, data):
        p = self.root/name
        p.write_text(json.dumps(data), encoding='utf-8')
        return str(p)

    def prepare(self, operation=None):
        denied = self.cli('hook', event=self.event('PreToolUse'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        context = next((self.root/'outputs/mcp-coverage-audit').glob('*.json'))
        state = json.loads(context.read_text(encoding='utf-8'))
        state['version'] = 1
        context.write_text(json.dumps(state), encoding='utf-8')
        ops = [operation or {'operation': 'Read faces', 'status': 'covered', 'tool': 'list_faces', 'reason': 'Source tool exists.'}]
        self.cli('preflight', '--context', str(context), '--operations', self.file('operations.json', ops))
        return str(context)

    def test_catalog_uses_source_without_import_and_outside_project_is_noop(self):
        result = self.cli('catalog')
        self.assertEqual({t['name'] for t in result['tools']}, {'list_faces', 'offset_surface', 'run_script'})
        outside = self.event('PreToolUse', cwd=str(Path(self.temp.name)/'elsewhere'))
        self.assertEqual(self.cli('hook', event=outside), {})
        self.assertFalse((self.root/'outputs').exists())

    def test_preflight_allows_declared_gap_and_records_native_evidence_across_processes(self):
        prompt = self.cli('hook', event=self.event('UserPromptSubmit'))
        self.assertIn('preflight', prompt['hookSpecificOutput']['additionalContext'])
        context = self.prepare({'operation': 'Thicken one surface', 'status': 'missing', 'reason': 'No source tool.'})
        misleading = [{'operation': 'Thicken one surface', 'status': 'covered', 'tool': 'run_script', 'reason': 'Can run raw API.'}]
        self.cli('preflight', '--context', context, '--operations', self.file('misleading.json', misleading), ok=False)
        allowed = self.cli('hook', event=self.event('PreToolUse'))
        self.assertNotIn('permissionDecision', allowed['hookSpecificOutput'])
        post = self.event('PostToolUse', tool_input={'code': "call('list_faces')\nfm.FeatureBossThicken(.00005,0,0,False,False,False,True)"})
        self.cli('hook', event=post)
        self.cli('hook', event=post)
        state = self.cli('status', '--context', context)
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(state['events'][0]['native_candidates'], ['FeatureBossThicken'])
        self.assertEqual(state['events'][0]['registered_calls'], ['list_faces', 'run_script'])
        self.assertIn('FeatureBossThicken', state['events'][0]['input_excerpt'])

    def test_finish_covered_creates_no_ticket_and_later_evidence_invalidates_receipt(self):
        context = self.prepare()
        self.cli('hook', event=self.event('PostToolUse'))
        state = self.cli('status', '--context', context)
        review = {'evidence_digest': state['evidence_digest'], 'reviews': [
            {'operation': 'Read faces', 'status': 'covered', 'tool': 'list_faces', 'evidence_ids': ['call1']}]}
        self.cli('finish', '--context', context, '--reviews', self.file('reviews.json', review))
        self.assertEqual(self.cli('hook', event=self.event('Stop')), {})
        self.assertFalse((self.root/'tickets').exists())
        self.cli('hook', event=self.event('PostToolUse', identity='call2'))
        self.assertEqual(self.cli('hook', event=self.event('Stop'))['decision'], 'block')
        self.cli('finish', '--context', context, '--reviews', str(self.root/'reviews.json'), ok=False)

    def test_missing_tickets_deduplicate_and_require_all_native_candidates(self):
        context = self.prepare()
        self.cli('hook', event=self.event('PostToolUse', tool_input={'code': 'body.Name = "New name"'}))
        state = self.cli('status', '--context', context)
        r = {'operation': 'Rename one body', 'status': 'missing', 'proposed_tool': 'rename_body',
             'native_api': ['Name:set'], 'reason': 'Existing feature renaming does not rename arbitrary bodies.',
             'inputs': ['document', 'configuration', 'body name', 'new name'],
             'expected_output': 'The body has the requested name.', 'validation': ['Old name guard', 'Readback'],
             'evidence_ids': ['call1']}
        report = {'evidence_digest': state['evidence_digest'], 'reviews': [r]}
        report_path = self.file('reviews.json', report)
        self.cli('finish', '--context', context, '--reviews', report_path)
        r['operation'] = 'Rename an existing solid body'
        r['native_api'] = ['NAME:SET']
        self.cli('finish', '--context', context, '--reviews', self.file('reviews.json', report))
        tickets = list((self.root/'tickets/solidworks-mcp').glob('*.json'))
        self.assertEqual(len(tickets), 1)
        ticket = json.loads(tickets[0].read_text(encoding='utf-8'))
        self.assertEqual(len(ticket['evidence']), 1)
        self.assertTrue(tickets[0].with_suffix('.md').is_file())

    def test_stop_continuation_is_bounded_and_does_not_claim_a_confirmed_gap(self):
        context = self.prepare()
        self.file('outputs/mcp-coverage-audit/' + '0' * 24 + '.json', ['preflight artifact'])
        self.file('outputs/mcp-coverage-audit/' + '1' * 24 + '.json', {'reviews': []})
        self.cli('hook', event=self.event('PostToolUse', tool_input={'code': 'd.InsertOffsetSurface(0, False)'}))
        first = self.cli('hook', event=self.event('Stop', stop_hook_active=False))
        self.assertEqual(first['decision'], 'block')
        self.assertIn('Do not implement any improvement without a real user selection', first['reason'])
        second = self.cli('hook', event=self.event('Stop', stop_hook_active=True))
        self.assertNotIn('decision', second)
        self.assertIn('needs_review', second['systemMessage'])
        ticket = next((self.root/'tickets/solidworks-mcp').glob('*.json'))
        self.assertEqual(json.loads(ticket.read_text(encoding='utf-8'))['status'], 'needs_review')
        self.assertEqual(self.cli('hook', event=self.event('Stop')), {})
        self.assertEqual(self.cli('status', '--context', context)['review_receipt']['status'], 'needs_review')

    def test_old_fallback_does_not_suppress_a_new_context_review(self):
        older = self.prepare()
        self.cli('hook', event=self.event('PostToolUse', tool_input={'code': 'd.InsertOffsetSurface(0, False)'}))
        self.assertEqual(self.cli('hook', event=self.event('Stop', stop_hook_active=False))['decision'], 'block')
        self.cli('hook', event=self.event('UserPromptSubmit', turn_id='turn2'))
        newer = next(p for p in (self.root/'outputs/mcp-coverage-audit').glob('*.json')
                     if json.loads(p.read_text(encoding='utf-8'))['turn_id'] == 'turn2')
        operations = [{'operation': 'Read faces', 'status': 'covered', 'tool': 'list_faces', 'reason': 'Source tool exists.'}]
        self.cli('preflight', '--context', str(newer), '--operations', self.file('new-operations.json', operations))
        self.cli('hook', event=self.event('PostToolUse', identity='call2', turn_id='turn2',
                 tool_input={'code': 'fm.FeatureBossThicken(.00005,0,0,False,False,False,True)'}))
        outcome = self.cli('hook', event=self.event('Stop', turn_id='turn2', stop_hook_active=False))
        self.assertEqual(outcome.get('decision'), 'block')
        self.assertIn(str(newer.resolve()), outcome['reason'])
        self.assertNotIn(older, outcome['reason'])
        self.assertEqual(self.cli('status', '--context', older)['review_receipt']['status'], 'needs_review')
        self.assertIsNone(self.cli('status', '--context', str(newer))['review_receipt'])
        final = self.cli('hook', event=self.event('Stop', turn_id='turn2', stop_hook_active=True))
        self.assertNotIn('decision', final)
        self.assertEqual(self.cli('status', '--context', str(newer))['review_receipt']['status'], 'needs_review')

    def test_wrapper_does_not_duplicate_nested_mcp_and_context_cannot_escape(self):
        context = self.prepare()
        self.cli('hook', event=self.event('PostToolUse', tool_name='functions.exec',
                 tool_input={'code': "await tools.mcp__solidworks__run_script({code: 'call(\"list_faces\")'})"}))
        self.cli('hook', event=self.event('PostToolUse', identity='nested'))
        self.assertEqual(len(self.cli('status', '--context', context)['events']), 1)
        self.cli('status', '--context', str(Path(self.temp.name)/'outside.json'), ok=False)


if __name__ == '__main__':
    unittest.main()
