"""Version-2 workflow protocol against real CLI processes; no CAD or source imports."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'codex_hooks/coverage_audit.py'


class SelectedWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'tools/solidworks-mcp/solidworks_mcp/ops.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_text("@tool('inspect', 'Inspect')\ndef inspect(args): pass\n", encoding='utf-8')
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.hook('UserPromptSubmit', prompt='Read the active CAD model.')
        self.context = next((self.root/'outputs/mcp-coverage-audit').glob('*.json'))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, event=None, ok=True):
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--workspace', str(self.root), *args],
                              input=json.dumps(event) if event else None, encoding='utf-8', capture_output=True, timeout=10)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        return proc.stderr

    def hook(self, kind, **extra):
        return self.cli('hook', event={'hook_event_name': kind, 'session_id': 's1', 'turn_id': 't1',
                        'cwd': str(self.root), 'tool_name': 'mcp__solidworks__inspect', 'tool_use_id': 'cad1',
                        'tool_input': {}, **extra})

    def file(self, name, value):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return str(path)

    def command(self, command, value, context=None, ok=True):
        flag = {'propose': 'proposal', 'select': 'selection', 'advance': 'update',
                'operation-note': 'note', 'post-work': 'receipt', 'finish': 'reviews'}[command]
        return self.cli(command, '--context', str(context or self.context), '--' + flag,
                        self.file(command + '.json', value), ok=ok)

    def state(self):
        return self.cli('status', '--context', str(self.context))

    def cad(self):
        self.preflight(self.context)
        self.hook('PostToolUse', tool_response={'timing_ms': {'total': 12}, 'result': 'model'})
        self.finish()

    def preflight(self, context, validation_of=None):
        args = ['preflight', '--context', str(context), '--operations', self.file('operations.json', [
            {'operation': 'Read model', 'status': 'covered', 'tool': 'inspect', 'reason': 'Atomic source tool.'}])]
        if validation_of:
            args.extend(['--validation-of', str(validation_of)])
        return self.cli(*args)

    def finish(self):
        state = self.state()
        return self.command('finish', {'evidence_digest': state['evidence_digest'], 'reviews': [
            {'operation': 'Read model', 'status': 'covered', 'tool': 'inspect',
             'evidence_ids': [e['id'] for e in state['events']]}]})

    def proposal(self, route='hook_cli'):
        candidate = {'id': 'summary', 'title': 'Bounded summary', 'benefit': 'Avoid repeated reads.',
                     'scope': 'Local audit reporting.', 'evidence': 'Repeated status observations.',
                     'acceptance': 'Fresh CLI returns persisted counts.', 'verification_route': route}
        if route == 'live_tool':
            candidate['tool'] = 'inspect'
        return {'job_state': 'completed', 'capability_assessment': 'Atomic tools cover the CAD job.',
                'efficiency_assessment': 'Repeated reporting can be reduced.', 'candidates': [candidate]}

    def reply(self, turn='t2', prompt='Implement summary.'):
        self.hook('UserPromptSubmit', turn_id=turn, prompt=prompt)
        reply = self.state()['latest_reply']
        return {'reply_context': reply['context'], 'reply_digest': reply['digest']}

    def selected(self, route='hook_cli', second=False):
        self.cad()
        proposal = self.proposal(route)
        if second:
            proposal['candidates'].append({**proposal['candidates'][0], 'id': 'other'})
        self.proposal_result = self.command('propose', proposal)
        self.proposal_id = self.proposal_result['proposal_id']
        selection = {'proposal_id': self.proposal_id, **self.reply(), 'selected_ids': ['summary', 'other'] if second else ['summary'],
                     'presented': True, 'interpreted': True}
        self.command('select', selection)
        return selection

    def update(self, status, **extra):
        return {'proposal_id': self.proposal_id, 'item_id': 'summary', 'status': status,
                'evidence': 'Observed actual selected-work result.', **extra}

    def progress(self, version):
        path = self.root/'checkpoint.txt'
        path.write_text('actual fixture artifact ' + str(version), encoding='utf-8')
        return self.update('progress', checkpoint={'path': 'checkpoint.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    def verification(self, route='hook_cli'):
        self.source.write_text(self.source.read_text(encoding='utf-8') + '\n# changed fixture behavior\n', encoding='utf-8')
        current = hashlib.sha256(self.source.read_bytes()).hexdigest()
        relative = self.source.relative_to(self.root).as_posix()
        hashes = {relative: current}
        verify = {'proposal_id': self.proposal_id, 'item_id': 'summary', 'status': 'passed', 'route': route,
                  'source_hashes': hashes, 'capability': 'Selected summary behavior',
                  'invocation': 'Real disposable CLI protocol check.', 'before_behavior': 'No summary.',
                  'observed_behavior': 'Expected summary returned.', 'tool': 'inspect'}
        update = self.update('verified', changed_sources=[{'path': relative, 'sha256': current, 'before_sha256': self.before}],
                             verification=self.file('verify.json', verify))
        if route == 'live_tool':
            update['source_check'] = {'catalog_digest': self.cli('catalog')['digest'], 'source': relative,
                                      'tool': 'inspect', 'sha256': current}
            update['live_check'] = self.file('live.json', {'proposal_id': self.proposal_id, 'tool': 'inspect',
                                        'availability': 'available', 'source_hashes': hashes,
                                        'evidence': 'Actual named connected fixture invocation after reload.'})
        return update, verify

    def test_all_cad_reflection_metrics_and_explicit_notes_without_ui(self):
        self.assertEqual(self.state()['version'], 2)
        self.cad()
        self.assertEqual(self.state()['improvement_status'], 'pending')
        self.assertEqual(self.hook('Stop')['decision'], 'block')
        self.hook('PostToolUse', tool_use_id='cad2', tool_response={'content': [{'type': 'text', 'text': '{"timing_ms":{"total":3}}'}]})
        self.hook('PostToolUse', tool_use_id='cad2', tool_response={})
        self.hook('PostToolUse', tool_use_id='cad3')
        self.hook('PostToolUse', tool_name='functions.exec', tool_use_id='wrapper',
                  tool_input={'code': 'await tools.mcp__solidworks__inspect({})'})
        metrics = self.state()['metrics']
        self.assertEqual(metrics['call_count'], 3)
        self.assertEqual(metrics['known_timing_ms_total'], 15)
        self.assertEqual(metrics['missing_timing_count'], 1)
        self.assertEqual(metrics['missing_response_bytes_count'], 1)
        self.assertGreater(metrics['response_bytes_total'], 0)
        self.command('post-work', {'job_state': 'completed', 'findings': []}, ok=False)
        self.finish()
        note = {'operation': 'Read opaque script result', 'route': 'opaque_cli', 'outcome': 'completed', 'evidence': 'Read model identity.',
                'timing_ms_total': 2, 'response_bytes': 20}
        self.assertTrue(self.command('operation-note', note)['changed'])
        self.assertFalse(self.command('operation-note', note)['changed'])
        self.assertEqual(self.state()['metrics']['known_timing_ms_total'], 17)
        self.hook('PostToolUse', tool_name='functions.exec', tool_use_id='ui-wrapper',
                  tool_input={'code': 'await tools.mcp__cua_repl__js({code: "await cua.getApp(\'SolidWorks\')"})'},
                  tool_response={'timing_ms': {'total': 5}})
        self.hook('PostToolUse', tool_name='mcp__cua_repl__js', tool_use_id='ui-direct',
                  tool_input={'code': "await cua.getApp('SolidWorks')"}, tool_response={'timing_ms': {'total': 5}})
        self.assertEqual(self.state()['metrics']['known_timing_ms_total'], 22)
        self.assertEqual(self.state()['metrics']['call_count'], 5)
        proposal = self.proposal()
        proposal.update(candidates=[], no_candidate_reason='No worthwhile change after review.')
        self.assertEqual(self.command('propose', proposal)['status'], 'no_candidates')
        self.assertEqual(self.hook('Stop'), {})

    def test_explicit_note_or_preflight_establishes_activity_and_deferral(self):
        self.assertEqual(self.state()['improvement_status'], 'not_required')
        self.command('propose', self.proposal(), ok=False)
        self.command('operation-note', {'operation': 'Read active model', 'route': 'opaque_cli',
                                     'outcome': 'unfinished', 'evidence': 'Script only partially completed.'})
        self.assertEqual(self.state()['improvement_status'], 'pending')
        self.command('propose', {'job_state': 'unfinished', 'reason': 'Main CAD job is not finished.'})
        self.assertEqual(self.hook('Stop'), {})
        result = self.command('propose', self.proposal())
        self.assertEqual(result['status'], 'awaiting_selection')
        self.assertEqual(self.hook('Stop'), {})
        self.assertEqual(self.command('select', {'proposal_id': result['proposal_id'], **self.reply(),
                         'selected_ids': [], 'presented': True, 'interpreted': True})['status'], 'declined')
        self.assertEqual(self.hook('Stop'), {})
        reply = self.reply('t3', 'Inspect a new model.')
        self.preflight(reply['reply_context'])
        other = self.cli('status', '--context', reply['reply_context'])
        self.assertEqual(other['improvement_status'], 'pending')
        self.assertEqual(other['events'], [])

    def test_proposal_binding_latest_genuine_prompt_replay_and_scope(self):
        self.hook('UserPromptSubmit', prompt='全部')
        self.cad()
        continuation = self.hook('Stop')['reason']
        original = self.state()['latest_reply']
        self.hook('UserPromptSubmit', turn_id='synthetic', prompt=continuation)
        self.assertEqual(self.state()['latest_reply'], original)
        value = self.proposal()
        result = self.command('propose', value)
        self.assertEqual(result, self.command('propose', value))
        value['candidates'][0]['scope'] = 'Changed scope.'
        self.command('propose', value, ok=False)
        base = {'proposal_id': result['proposal_id'], 'selected_ids': ['summary'], 'presented': True, 'interpreted': True}
        self.command('select', {**base, 'reply_context': original['context'], 'reply_digest': original['digest']}, ok=False)
        stale = self.reply()
        current = self.reply('t3', '全部')
        self.assertEqual(current['reply_digest'], original['digest'])
        self.assertNotEqual(current['reply_context'], original['context'])
        self.command('select', {**base, **stale}, ok=False)
        self.command('select', {**base, **current, 'presented': False}, ok=False)
        self.command('select', {**base, **current, 'selected_ids': ['unselected']}, ok=False)
        self.command('select', {**base, **current, 'prompt': 'different prompt'}, ok=False)
        self.command('select', {**base, **current})
        self.reply('t4', 'Continue.')
        self.assertFalse(self.command('select', {**base, **current})['changed'])
        self.command('select', {**base, **current, 'selected_ids': []}, ok=False)
        self.proposal_id = result['proposal_id']
        self.command('advance', {**self.update('blocked'), 'item_id': 'unselected'}, ok=False)

    def test_repeated_progress_continues_then_stalls_and_real_checkpoint_resumes(self):
        self.selected()
        for i in range(5):
            update = self.progress(i)
            self.command('advance', update)
            self.assertEqual(self.hook('Stop', stop_hook_active=True)['decision'], 'block')
        self.command('advance', self.update('progress'), ok=False)
        for i in range(3):
            same = {**update, 'evidence': 'New narration ' + str(i), 'timestamp': str(i)}
            self.assertFalse(self.command('advance', same)['progress_changed'])
            boundary = self.hook('Stop', stop_hook_active=True)
            if i < 2:
                self.assertEqual(boundary['decision'], 'block')
        self.assertNotIn('decision', boundary)
        self.assertEqual(self.state()['improvement_status'], 'stalled')
        self.command('advance', self.update('resume'))
        self.assertNotIn('decision', self.hook('Stop'))
        resume = self.update('resume', user_requested=True, **self.reply('t3', 'Resume stalled implementation.'))
        self.command('advance', resume)
        self.assertEqual(self.hook('Stop')['decision'], 'block')
        for _ in range(3):
            self.command('advance', resume)
            last = self.hook('Stop', stop_hook_active=True)
        self.assertNotIn('decision', last)
        self.assertEqual(self.state()['improvement_status'], 'stalled')
        self.command('advance', self.progress(6))
        self.assertEqual(self.hook('Stop', stop_hook_active=True)['decision'], 'block')
        self.assertEqual(self.state()['improvement_status'], 'running')

    def test_owned_reminders_pause_block_pending_live_and_validation_do_not_recurse(self):
        audit = self.context.parent
        for name, value in [('operations.json', ['not an audit context']),
                            ('0' * 24 + '.json', ['not an audit context']),
                            ('1' * 24 + '.json', {'version': 2, 'session_id': 's1', 'turn_id': 't1', 'events': {}})]:
            (audit/name).write_text(json.dumps(value), encoding='utf-8')
        (audit/('2' * 24 + '.json')).write_text('invalid json', encoding='utf-8')
        self.selected()
        validation_context = self.state()['latest_reply']['context']
        denied = self.hook('PreToolUse', turn_id='t2')
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.preflight(validation_context, self.context)
        self.command('operation-note', {'operation': 'Inspect selected validation result', 'route': 'opaque_cli',
                      'outcome': 'completed', 'evidence': 'Copied fixture readback.'}, context=validation_context)
        self.hook('PostToolUse', turn_id='t2', tool_use_id='validation1')
        state = self.state()
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(len(state['validation_events']), 1)
        self.assertEqual(len(state['validation_notes']), 1)
        t2 = json.loads(Path(state['latest_reply']['context']).read_text(encoding='utf-8'))
        self.assertEqual(t2['events'], [])
        self.assertIn(str(self.context), self.hook('SessionStart')['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision', self.hook('Stop', session_id='other'))
        self.command('advance', self.update('pending_live', evidence='Connected fixture tool needs reload.'))
        self.assertEqual(self.hook('Stop'), {})
        self.assertEqual(self.state()['improvement_status'], 'pending_live')
        self.command('advance', self.update('blocked', evidence='Disposable fixture unavailable.'))
        self.assertEqual(self.hook('Stop'), {})
        self.command('advance', self.update('paused'), ok=False)
        control = {'user_requested': True, **self.reply('t3', 'Pause the selected implementation.')}
        self.command('advance', self.update('paused', **control))
        self.assertEqual(self.hook('Stop'), {})
        self.command('advance', self.progress('paused-bypass'), ok=False)
        self.command('advance', self.update('verified'), ok=False)
        self.command('advance', self.update('resume'), ok=False)
        self.command('advance', self.update('resume', user_requested=True, **self.reply('t4', 'Resume implementation.')))
        self.assertEqual(self.hook('Stop')['decision'], 'block')
        new_reply = self.reply('t5', 'Now inspect a different CAD model.')
        self.assertEqual(self.hook('PreToolUse', turn_id='t5')['hookSpecificOutput']['permissionDecision'], 'deny')
        self.preflight(new_reply['reply_context'])
        self.hook('PostToolUse', turn_id='t5', tool_use_id='new-cad')
        other = self.cli('status', '--context', new_reply['reply_context'])
        self.assertEqual(other['improvement_status'], 'pending')
        self.assertEqual(len(other['events']), 1)
        self.assertEqual(len(self.state()['validation_events']), 1)

    def test_cli_verified_requires_matching_current_hashes_and_changed_behavior(self):
        self.selected()
        update, verify = self.verification()
        altered = {**verify, 'route': 'live_tool'}
        update['verification'] = self.file('bad.json', altered)
        self.command('advance', update, ok=False)
        update['verification'] = self.file('verify.json', {**verify, 'observed_behavior': verify['before_behavior']})
        self.command('advance', update, ok=False)
        update['verification'] = self.file('verify.json', verify)
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'\n# changed after verification\n')
        self.command('advance', update, ok=False)
        self.source.write_bytes(original)
        self.assertEqual(self.command('advance', update)['status'], 'verified')
        self.assertFalse(self.command('advance', update)['changed'])
        self.assertEqual(self.hook('Stop'), {})

    def test_later_selected_edit_invalidates_prior_verification_until_revalidated(self):
        self.selected(second=True)
        first, _ = self.verification()
        self.command('advance', first)
        second, verification = self.verification()
        second['item_id'] = 'other'
        verification['item_id'] = 'other'
        second['verification'] = self.file('verify-other.json', verification)
        self.assertEqual(self.command('advance', second)['status'], 'running')
        self.assertEqual(self.state()['selected_work']['tasks']['summary']['status'], 'progress')
        first['changed_sources'] = second['changed_sources']
        verification['item_id'] = 'summary'
        first['verification'] = self.file('verify-summary.json', verification)
        self.assertEqual(self.command('advance', first)['status'], 'verified')
        self.assertEqual(self.hook('Stop'), {})
        self.source.write_bytes(self.source.read_bytes() + b'\n# later separate completed batch\n')
        self.assertTrue(self.state()['selected_work']['completed_snapshot'])
        self.assertEqual(self.state()['improvement_status'], 'verified')
        self.assertEqual(self.hook('Stop'), {})

    def test_live_completion_requires_actual_named_available_tool_and_hashes(self):
        self.selected('live_tool')
        update, verify = self.verification('live_tool')
        live_path = Path(update['live_check'])
        live = json.loads(live_path.read_text(encoding='utf-8'))
        live_path.write_text(json.dumps({**live, 'availability': 'unavailable'}), encoding='utf-8')
        self.command('advance', update, ok=False)
        live_path.write_text(json.dumps(live), encoding='utf-8')
        update['verification'] = self.file('verify.json', {**verify, 'tool': 'wrong_tool'})
        self.command('advance', update, ok=False)
        update['verification'] = self.file('verify.json', verify)
        self.assertEqual(self.command('advance', update)['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
