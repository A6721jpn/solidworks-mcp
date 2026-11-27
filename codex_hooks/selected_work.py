from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def enabled(state):
    return state.get('version', 1) >= 2


def prompt_digest(prompt):
    return hashlib.sha256(prompt.encode('utf-8')).hexdigest()


def session_path(a, root, session):
    return a.state_root(root) / 'sessions' / (a.digest(session)[:24] + '.json')


def latest_reply(a, root, session):
    path = session_path(a, root, session)
    return a.read_json(path).get('latest_reply') if path.exists() else None


def remember_prompt(a, root, event, context):
    prompt = event.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    path = session_path(a, root, event['session_id'])
    with a.locked(path.with_suffix('.lock')):
        data = a.read_json(path) if path.exists() else {}
        identity = prompt_digest(prompt)
        if identity in data.get('synthetic_prompts', []):
            return False
        data['latest_reply'] = {'context': str(context), 'session_id': event['session_id'],
                                'turn_id': event.get('turn_id') or 'session', 'digest': identity}
        a.write_json(path, data)
    return True


def remember_continuation(a, root, session, reason):
    path = session_path(a, root, session)
    with a.locked(path.with_suffix('.lock')):
        data = a.read_json(path) if path.exists() else {}
        identities = data.get('synthetic_prompts', [])
        identity = prompt_digest(reason)
        data['synthetic_prompts'] = (identities + [identity])[-16:]
        a.write_json(path, data)


def metrics(response):
    total = None
    queue = [(response, 0)]
    while queue:
        value, depth = queue.pop(0)
        if depth > 4:
            continue
        if isinstance(value, dict):
            timing = value.get('timing_ms')
            candidate = timing.get('total') if isinstance(timing, dict) else None
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and math.isfinite(candidate) and 0 <= candidate <= 1e12:
                total = candidate
                break
            queue.extend((v, depth + 1) for k, v in value.items() if k in {'structuredContent', 'content', 'result'})
            if value.get('type') == 'text' and isinstance(value.get('text'), str) and len(value['text']) <= 200000:
                try:
                    queue.append((json.loads(value['text']), depth + 1))
                except ValueError:
                    pass
        elif isinstance(value, list):
            queue.extend((v, depth + 1) for v in value[:16])
    return {'timing_ms_total': total,
            'response_bytes': None if response is None else len(json.dumps(response, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}


def metric_summary(state):
    observations = state.get('observations', []) + state.get('operation_notes', [])
    timings = [o['timing_ms_total'] for o in observations if o.get('timing_ms_total') is not None]
    return {'call_count': len(observations), 'known_timing_ms_total': sum(timings),
            'missing_timing_count': len(observations) - len(timings),
            'response_bytes_total': sum(o['response_bytes'] for o in observations if o.get('response_bytes') is not None),
            'missing_response_bytes_count': sum(o.get('response_bytes') is None for o in observations),
            'units': 'milliseconds and serialized UTF-8 bytes; not tokens'}


def activity(state):
    return bool(state.get('events') or state.get('fallbacks') or state.get('operation_notes')
                or (state.get('preflight') and not state['preflight'].get('validation_of')))


def status(state):
    work = state.get('selected_work', {})
    if work.get('deferral'):
        return 'deferred'
    proposal = work.get('proposal')
    if not proposal:
        return 'needs_review' if work.get('stalled') else 'pending' if activity(state) else 'not_required'
    if not proposal['candidates']:
        return 'no_candidates'
    selection = work.get('selection')
    if selection is None:
        return 'awaiting_selection'
    if not selection['selected_ids']:
        return 'declined'
    tasks = list(work.get('tasks', {}).values())
    if all(t['status'] == 'verified' for t in tasks):
        return 'verified'
    if any(t['status'] in {'progress', 'resume'} for t in tasks):
        return 'stalled' if work.get('stalled') else 'running'
    if any(t['status'] == 'pending_live' for t in tasks):
        return 'pending_live'
    if any(t['status'] == 'blocked' for t in tasks):
        return 'blocked'
    if any(t['status'] == 'paused' for t in tasks):
        return 'paused'
    return 'cancelled'


def operation_note(a, root, path, note):
    if not isinstance(note, dict):
        raise ValueError('Operation note must be an object.')
    item = {k: a.bounded_text(note.get(k), k) for k in ('operation', 'route', 'outcome', 'evidence')}
    item['id'] = 'op-' + a.digest(item)[:24]
    for key in ('timing_ms_total', 'response_bytes'):
        value = note.get(key)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                  or not math.isfinite(value) or not 0 <= value <= 1e12
                                  or (key == 'response_bytes' and not isinstance(value, int))):
            raise ValueError('Optional operation metrics must be bounded nonnegative numbers; bytes must be an integer.')
        item[key] = value
    current = a.read_json(path)
    owner = validation_owner(a, root, current)
    if owner:
        if current['preflight']['catalog_digest'] != a.catalog(root)['digest']:
            raise ValueError('Validation preflight source catalog is stale.')
        with a.locked(owner.with_suffix('.lock')):
            state = a.read_json(owner)
            notes = state.setdefault('validation_notes', [])
            changed = not any(n['id'] == item['id'] for n in notes)
            if changed:
                if len(notes) >= 128:
                    raise ValueError('At most 128 validation notes per selected context.')
                notes.append(item)
                a.write_json(owner, state)
        return {'id': item['id'], 'changed': changed, 'context': str(owner), 'validation': True}
    with a.locked(path.with_suffix('.lock')):
        state = a.read_json(path)
        if not enabled(state):
            raise ValueError('operation-note requires a version-2 context.')
        notes = state.setdefault('operation_notes', [])
        changed = not any(n['id'] == item['id'] for n in notes)
        if changed:
            if len(notes) >= 128:
                raise ValueError('At most 128 operation notes per context.')
            notes.append(item)
            state.setdefault('selected_work', {}).pop('deferral', None)
            a.write_json(path, state)
    return {'id': item['id'], 'changed': changed, 'context': str(path)}


def propose(a, root, path, value):
    if not isinstance(value, dict):
        raise ValueError('Proposal must be an object.')
    with a.locked(path.with_suffix('.lock')):
        state = a.read_json(path)
        if not enabled(state):
            raise ValueError('propose requires a version-2 context; legacy contexts keep post-work.')
        work = state.setdefault('selected_work', {})
        if value.get('job_state') in a.DEFER_STATES:
            if work.get('proposal'):
                raise ValueError('An immutable proposal already exists; pause selected work with advance.')
            work['deferral'] = {'job_state': value['job_state'], 'reason': a.bounded_text(value.get('reason'), 'reason')}
            a.write_json(path, state)
            return {'status': 'deferred', 'context': str(path)}
        if value.get('job_state') != 'completed':
            raise ValueError('Proposal requires job_state=completed or a reasoned deferral.')
        proposal = {k: a.bounded_text(value.get(k), k) for k in ('capability_assessment', 'efficiency_assessment')}
        candidates = value.get('candidates')
        if not isinstance(candidates, list) or len(candidates) > 12:
            raise ValueError('candidates must be an array of at most 12 items.')
        proposal['candidates'] = []
        ids = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError('Candidate must be an object.')
            normalized = {k: a.bounded_text(candidate.get(k), k, 80 if k == 'id' else 1200)
                          for k in ('id', 'title', 'benefit', 'evidence', 'scope', 'acceptance')}
            route = candidate.get('verification_route')
            if route not in {'live_tool', 'hook_cli'} or normalized['id'] in ids:
                raise ValueError('Unique IDs and live_tool or hook_cli verification_route are required.')
            normalized['verification_route'] = route
            if route == 'live_tool':
                normalized['tool'] = a.bounded_text(candidate.get('tool'), 'tool', 64)
            ids.add(normalized['id'])
            proposal['candidates'].append(normalized)
        if not candidates:
            proposal['no_candidate_reason'] = a.bounded_text(value.get('no_candidate_reason'), 'no_candidate_reason')
        existing = work.get('proposal')
        proposal['postwork_digest'] = existing['postwork_digest'] if existing else a.postwork_digest(state)
        reply = latest_reply(a, root, state['session_id']) or {}
        proposal['origin_reply'] = existing.get('origin_reply') if existing else {k: reply.get(k) for k in ('context', 'digest')}
        proposal['id'] = 'proposal-' + a.digest(proposal)[:24]
        if existing and existing != proposal:
            raise ValueError('Proposal is immutable; a conflicting retry is refused.')
        if not existing:
            if not activity(state):
                raise ValueError('Proposal requires recorded CAD activity or an explicit preflight/operation-note.')
            if a.coverage_pending(state):
                raise ValueError('Finish CAD coverage before proposing improvements.')
            work['proposal'] = proposal
            work.pop('deferral', None)
            work.pop('stalled', None)
            a.write_json(path, state)
        numbered = '\n'.join(f"{i}. [{c['id']}] {c['title']} — {c['benefit']} Scope: {c['scope']} Acceptance: {c['acceptance']} Verify: {c['verification_route']}" for i, c in enumerate(candidates, 1))
        return {'status': status(state), 'context': str(path), 'proposal_id': proposal['id'],
                'candidates': proposal['candidates'], 'display': numbered or proposal['no_candidate_reason']}


def require_reply(a, root, state, value):
    reply = latest_reply(a, root, state['session_id'])
    if not reply or value.get('reply_digest') != reply['digest'] or value.get('reply_context') != reply['context']:
        raise ValueError('Selection/control must bind the latest genuine reply_context and reply_digest.')
    other = a.read_json(a.context_path(root, reply['context']))
    if other.get('session_id') != state['session_id']:
        raise ValueError('Another session cannot claim this work.')
    if 'prompt' in value and (not isinstance(value['prompt'], str) or prompt_digest(value['prompt']) != reply['digest']):
        raise ValueError('Supplied prompt does not match the latest genuine reply.')
    return reply


def select(a, root, path, value):
    if not isinstance(value, dict):
        raise ValueError('Selection must be an object.')
    with a.locked(path.with_suffix('.lock')):
        state = a.read_json(path)
        work = state.get('selected_work', {})
        proposal = work.get('proposal')
        if not enabled(state) or not proposal or value.get('proposal_id') != proposal['id']:
            raise ValueError('Selection requires this immutable proposal_id.')
        ids = value.get('selected_ids')
        allowed = {c['id'] for c in proposal['candidates']}
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or not set(ids) <= allowed:
            raise ValueError('selected_ids must be a unique subset of the displayed proposal.')
        if value.get('presented') is not True or value.get('interpreted') is not True:
            raise ValueError('Agent must attest actual proposal presentation and semantic interpretation.')
        normalized = {k: value.get(k) for k in ('proposal_id', 'reply_context', 'reply_digest', 'presented', 'interpreted')}
        normalized['selected_ids'] = sorted(ids)
        existing = work.get('selection')
        if existing:
            if normalized != existing:
                raise ValueError('Conflicting selection retry; the original selection is immutable.')
            return {'status': status(state), 'changed': False, 'context': str(path)}
        require_reply(a, root, state, value)
        if {'context': value['reply_context'], 'digest': value['reply_digest']} == proposal.get('origin_reply'):
            raise ValueError('Selection requires a reply after proposal presentation.')
        work['selection'] = normalized
        work['tasks'] = {i: {'status': 'progress', 'fingerprints': [], 'evidence': 'Selected; implementation pending.'} for i in sorted(ids)}
        work.pop('stalled', None)
        work['no_progress_boundaries'] = 0
        a.write_json(path, state)
    return {'status': status(state), 'changed': True, 'context': str(path)}


def checkpoint(a, root, value):
    if not isinstance(value, dict) or not isinstance(value.get('path'), str):
        raise ValueError('Progress requires checkpoint {path,sha256} or current changed_sources.')
    path = (root / value['path']).resolve()
    if not a.inside(path, root) or not path.is_file() or path.stat().st_size > 2000000:
        raise ValueError('Checkpoint must be a bounded local workspace file.')
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if value.get('sha256') != actual:
        raise ValueError('Checkpoint sha256 is stale.')
    return {str(path.relative_to(root)).replace('\\', '/'): actual}


def verified(a, root, proposal, candidate, value):
    hashes = a.changed_sources(root, value.get('changed_sources'))
    verification = a.local_artifact(root, value.get('verification'))
    route = candidate['verification_route']
    if (verification.get('status') != 'passed' or verification.get('route') != route
            or verification.get('source_hashes') != hashes or verification.get('proposal_id') != proposal['id']
            or verification.get('item_id') != candidate['id']):
        raise ValueError('Verification must match proposal, item, route, and current changed source hashes.')
    for key in ('capability', 'invocation', 'before_behavior', 'observed_behavior'):
        a.bounded_text(verification.get(key), 'verification ' + key)
    if verification['before_behavior'] == verification['observed_behavior']:
        raise ValueError('Verification requires actual changed behavior.')
    if route == 'live_tool':
        current = a.catalog(root)
        tool = a.source_check(root, value.get('source_check'), current)
        live = a.local_artifact(root, value.get('live_check'))
        if (tool != candidate['tool'] or verification.get('tool') != tool
                or live.get('tool') != tool or live.get('availability') != 'available'
                or live.get('proposal_id') != proposal['id'] or live.get('source_hashes') != hashes
                or value['source_check']['source'] not in hashes):
            raise ValueError('Live capability completion requires the named current source and actual available live tool evidence.')
        a.bounded_text(live.get('evidence'), 'live evidence')
    return hashes


def refresh_verified(a, root, state):
    if state.get('selected_work', {}).get('completed_snapshot'):
        return False
    changed = False
    for task in state.get('selected_work', {}).get('tasks', {}).values():
        if task['status'] != 'verified':
            continue
        for relative, expected in task.get('source_hashes', {}).items():
            path = (root / relative).resolve()
            if not a.inside(path, root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                task['status'] = 'progress'
                task['evidence'] = 'Current source changed after verification; revalidate the selected behavior.'
                changed = True
                break
    return changed


def advance(a, root, path, value):
    if not isinstance(value, dict):
        raise ValueError('Update must be an object.')
    with a.locked(path.with_suffix('.lock')):
        state = a.read_json(path)
        refresh_verified(a, root, state)
        work = state.get('selected_work', {})
        proposal = work.get('proposal') or {}
        identity = value.get('item_id')
        if value.get('proposal_id') != proposal.get('id') or identity not in work.get('tasks', {}):
            raise ValueError('Only an item selected from this proposal can advance.')
        outcome = value.get('status')
        if outcome not in {'progress', 'verified', 'pending_live', 'blocked', 'paused', 'cancelled', 'resume'}:
            raise ValueError('Unknown selected-work status.')
        old = work['tasks'][identity]
        if old['status'] in {'paused', 'cancelled'} and outcome not in {old['status'], 'resume'}:
            raise ValueError('Paused/cancelled work requires explicit user-bound resume before further execution.')
        if old['status'] == 'verified':
            if outcome == 'verified' and old.get('update_digest') == a.digest(value):
                return {'status': status(state), 'changed': False}
            raise ValueError('Verified item cannot be overwritten.')
        if outcome in {'paused', 'cancelled'} or (outcome == 'resume' and old['status'] in {'paused', 'cancelled'}):
            if value.get('user_requested') is not True:
                raise ValueError('Pause/cancel/resume control requires explicit user_requested attestation.')
            require_reply(a, root, state, value)
        evidence = a.bounded_text(value.get('evidence'), 'evidence')
        fingerprints = old.get('fingerprints', [])
        hashes = None
        if outcome == 'verified':
            candidate = next(c for c in proposal['candidates'] if c['id'] == identity)
            hashes = verified(a, root, proposal, candidate, value)
        elif outcome == 'progress':
            hashes = a.changed_sources(root, value['changed_sources']) if value.get('changed_sources') else checkpoint(a, root, value.get('checkpoint'))
        token = a.digest(hashes) if hashes else None
        new_progress = token is not None and token not in fingerprints
        if new_progress:
            if len(fingerprints) >= 128:
                raise ValueError('At most 128 distinct progress checkpoints per item.')
            fingerprints = fingerprints + [token]
            work.pop('stalled', None)
            work['no_progress_boundaries'] = 0
        explicit_resume = outcome == 'resume' and value.get('user_requested') is True
        if explicit_resume:
            require_reply(a, root, state, value)
            reply_identity = {k: value[k] for k in ('reply_context', 'reply_digest')}
            if work.get('last_resume_reply') != reply_identity:
                work['last_resume_reply'] = reply_identity
                work.pop('stalled', None)
                work['no_progress_boundaries'] = 0
        record = {'status': outcome, 'evidence': evidence, 'fingerprints': fingerprints,
                  'update_digest': a.digest(value)}
        if hashes:
            record['source_hashes' if outcome == 'verified' else 'checkpoint_hashes'] = hashes
        if outcome == 'verified':
            record['verification'] = value['verification']
        changed = record != old
        work['tasks'][identity] = record
        refresh_verified(a, root, state)
        if work['tasks'] and all(task['status'] == 'verified' for task in work['tasks'].values()):
            work['completed_snapshot'] = {'proposal_id': proposal['id'],
                'source_hashes_by_item': {key: task['source_hashes'] for key, task in work['tasks'].items()}}
        a.write_json(path, state)
    return {'status': status(state), 'item_status': outcome, 'changed': changed, 'progress_changed': new_progress}


def progress_signature(a, state):
    work = state.get('selected_work', {})
    return a.digest({'coverage': (state.get('review_receipt') or {}).get('evidence_digest'),
                     'proposal': (work.get('proposal') or {}).get('id'),
                     'resume_reply': work.get('last_resume_reply'),
                     'tasks': {k: {'fingerprints': v.get('fingerprints', []), 'verified': v['status'] == 'verified'}
                               for k, v in work.get('tasks', {}).items()}})


def boundary(a, state):
    work = state.setdefault('selected_work', {})
    current = status(state)
    if current not in {'pending', 'running', 'stalled', 'needs_review'}:
        return False
    signature = progress_signature(a, state)
    if signature != work.get('last_stop_signature'):
        work['no_progress_boundaries'] = 0
        work.pop('stalled', None)
    else:
        work['no_progress_boundaries'] = work.get('no_progress_boundaries', 0) + 1
    work['last_stop_signature'] = signature
    if work['no_progress_boundaries'] >= 3:
        work['stalled'] = 'Three consecutive Stop boundaries had no changed artifact/source fingerprint or verified phase.'
        return False
    return True


def owned_contexts(a, root, session):
    for path, state in a.audit_contexts(root):
        if enabled(state) and state.get('session_id') == session:
            yield path, state


def reminders(a, root, session):
    return ', '.join(f'{path} ({status(state)})' for path, state in owned_contexts(a, root, session)
                     if status(state) in {'pending', 'needs_review', 'awaiting_selection', 'running', 'stalled', 'pending_live', 'blocked', 'paused', 'deferred'})


def validation_owner(a, root, state):
    value = (state.get('preflight') or {}).get('validation_of')
    if not value:
        return None
    path = a.context_path(root, value)
    owner = a.read_json(path)
    if (owner.get('session_id') != state['session_id'] or not enabled(owner)
            or not owner.get('selected_work', {}).get('selection', {}).get('selected_ids')
            or status(owner) not in {'running', 'stalled', 'pending_live', 'blocked'}):
        raise ValueError('Validation owner must be unfinished selected work in the same session.')
    return path
