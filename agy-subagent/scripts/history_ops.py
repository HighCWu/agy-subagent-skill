"""Immutable conversation snapshots and local history branching for agy agents."""
import json
import time
import uuid

import agy_subagent as runner

TARGET = {'type': 'string'}
TURN_ID = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
CONTEXT_FIELDS = {
    'context_mode': {'type': 'string', 'enum': ['full', 'summary', 'none']},
    'context_summary': {'type': 'string'},
}
BRANCH_FIELDS = {
    'target': TARGET,
    'task_name': {'type': 'string', 'pattern': '^[a-z0-9_]+$'},
    'message': {'type': 'string'},
    'model': {'type': 'string'},
    'reasoning_effort': {'type': 'string', 'enum': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']},
    'cwd': {'type': 'string'},
    'agy': {'type': 'object'},
    **CONTEXT_FIELDS,
}
OPERATIONS = {
    'list_turns': {'required': ['target'], 'properties': {
        'target': TARGET, 'include_content': {'type': 'boolean'},
        'include_discarded': {'type': 'boolean'}}},
    'fork_agent': {'required': ['target', 'task_name', 'message'], 'properties': {
        **BRANCH_FIELDS, 'last_turn_id': TURN_ID}},
    'retry_agent': {'required': ['target', 'task_name', 'turn_id'], 'properties': {
        **BRANCH_FIELDS, 'turn_id': TURN_ID}},
    'rewind_agent': {'required': ['target', 'before_turn_id'], 'properties': {
        'target': TARGET, 'before_turn_id': TURN_ID, **CONTEXT_FIELDS}},
}


def save_seed(directory, seed):
    seeds = directory / 'seeds'
    seeds.mkdir(exist_ok=True, mode=0o700)
    seed_id = uuid.uuid4().hex
    runner.write_json(seeds / (seed_id + '.json'), seed)
    return seed_id


def repair_active_history(directory, state):
    """Link turns appended by a supervisor that was already running before upgrade."""
    plan_path = directory / 'history-repair.json'
    while plan_path.exists() or (state.get('run_id') and state['run_id'] != state.get('history_head')):
        if plan_path.exists():
            plan = runner.read_json(plan_path)
        else:
            current = directory / state['run_id'] / 'request.json'
            request = runner.read_json(current)
            if 'history_seed' in request and 'previous_run_id' in request:
                state['history_head'] = state['run_id']
                runner.write_json(directory / 'state.json', state)
                return
            last_time = request.get('started_at', current.stat().st_mtime)
            entries = []
            for path in directory.glob('*/request.json'):
                item = runner.read_json(path)
                started = item.get('started_at', path.stat().st_mtime)
                if ('history_seed' not in item and started <= last_time
                        and ((path.parent / 'result.json').exists() or path == current)):
                    entries.append({'run_id': path.parent.name, 'started_at': started})
            plan = {'seed_id': state['history_seed'], 'previous': state.get('history_head'),
                    'target': state['run_id'], 'turns': sorted(entries, key=lambda item: item['started_at'])}
            runner.write_json(plan_path, plan)
        previous = plan['previous']
        for item in plan['turns']:
            path = directory / item['run_id'] / 'request.json'
            request = runner.read_json(path)
            request.update(previous_run_id=previous, history_seed=plan['seed_id'],
                           started_at=item['started_at'], message=request.get('message', request['prompt']))
            runner.write_json(path, request)
            previous = item['run_id']
        state['history_head'] = plan['target']
        runner.write_json(directory / 'state.json', state)
        plan_path.unlink()


def ensure_history(directory, state):
    """Migrate old records once, under the agent lock. Preserve original prompt text."""
    if state.get('history_version') == 1:
        repair_active_history(directory, state)
        return
    plan_path = directory / 'history-migration.json'
    if plan_path.exists():
        plan = runner.read_json(plan_path)
    else:
        plan = {'seed_id': save_seed(directory, {'context': '', 'turns': []}), 'turns': []}
    known = {item['run_id'] for item in plan['turns']}
    paths = sorted(directory.glob('*/request.json'),
                   key=lambda path: runner.read_json(path).get('started_at', path.stat().st_mtime))
    for path in paths:
        if path.parent.name not in known:
            request = runner.read_json(path)
            plan['turns'].append({'run_id': path.parent.name,
                                  'started_at': request.get('started_at', path.stat().st_mtime)})
    # Persist ordering before touching any request mtime. A failed migration can resume.
    runner.write_json(plan_path, plan)
    previous = None
    for item in plan['turns']:
        path = directory / item['run_id'] / 'request.json'
        request = runner.read_json(path)
        request.update(previous_run_id=previous, history_seed=plan['seed_id'],
                       started_at=item['started_at'], message=request.get('message', request['prompt']))
        runner.write_json(path, request)
        previous = item['run_id']
    state.update(history_version=1, history_seed=plan['seed_id'],
                 history_head=state.get('run_id'), history_revision=0)
    runner.write_json(directory / 'state.json', state)


def record(directory, run_id, state):
    request_path = directory / run_id / 'request.json'
    request = runner.read_json(request_path)
    result_path = request_path.parent / 'result.json'
    output = runner.read_json(result_path) if result_path.exists() else {}
    return {'turn_id': run_id, 'agent_id': state['id'],
            'message': request.get('message', request['prompt']),
            'response': (output.get('result') or {}).get('response') or '',
            'status': output.get('status', state['status'] if state.get('run_id') == run_id else 'failed'),
            'started_at': request.get('started_at', request_path.stat().st_mtime)}


def snapshot(directory, state, through=None):
    """Read an active or archived path, including immutable inherited turn records."""
    ensure_history(directory, state)
    seed_id = state['history_seed']
    head = state.get('history_head')
    if through is not None:
        path = directory / through / 'request.json'
        if path.exists():
            request = runner.read_json(path)
            seed_id, head = request['history_seed'], through
        else:
            for path in sorted((directory / 'seeds').glob('*.json')):
                seed = runner.read_json(path)
                for index, item in enumerate(seed['turns']):
                    if item['turn_id'] == through:
                        return {'context': seed['context'], 'turns': seed['turns'][:index + 1]}
            raise ValueError('Unknown turn_id for this agent')
    seed = runner.read_json(directory / 'seeds' / (seed_id + '.json'))
    turns = []
    visited = set()
    while head:
        if head in visited:
            raise ValueError('History contains a cycle')
        visited.add(head)
        turns.append(record(directory, head, state))
        head = runner.read_json(directory / head / 'request.json').get('previous_run_id')
    return {'context': seed['context'], 'turns': seed['turns'] + list(reversed(turns))}


def render(seed):
    return json.dumps({'initial_context': seed['context'], 'turns': [
        {'user': item['message'], 'assistant': item['response'], 'status': item['status']}
        for item in seed['turns']]}, ensure_ascii=False)


def choose_context(seed, args):
    mode = args.get('context_mode', 'full')
    summary = args.get('context_summary')
    if mode == 'summary' and not summary:
        raise ValueError('context_mode=summary requires context_summary')
    if mode != 'summary' and summary is not None:
        raise ValueError('context_summary requires context_mode=summary')
    if mode == 'summary':
        return {'context': summary, 'turns': []}
    if mode == 'none':
        return {'context': '', 'turns': []}
    return seed


def list_turns(directory, state, args):
    active = snapshot(directory, state)
    active_ids = {item['turn_id'] for item in active['turns']}
    turns = list(active['turns'])
    if args.get('include_discarded'):
        saved = {}
        for path in sorted((directory / 'seeds').glob('*.json')):
            for item in runner.read_json(path)['turns']:
                saved[item['turn_id']] = item
        for path in directory.glob('*/request.json'):
            item = record(directory, path.parent.name, state)
            saved[item['turn_id']] = item
        turns += sorted((item for key, item in saved.items() if key not in active_ids),
                        key=lambda item: item['started_at'])
    positions = {item['turn_id']: index + 1 for index, item in enumerate(active['turns'])}
    result = []
    for item in turns:
        entry = {key: item[key] for key in ('turn_id', 'agent_id', 'status', 'started_at')}
        entry.update(turn=positions.get(item['turn_id']), active=item['turn_id'] in active_ids,
                     message_chars=len(item['message']), response_chars=len(item['response']))
        if args.get('include_content'):
            entry.update(message=item['message'], response=item['response'])
        result.append(entry)
    answer = {'agent_id': state['id'], 'task_name': state['task_name'],
              'history_revision': state.get('history_revision', 0), 'turns': result}
    if args.get('include_content'):
        answer['initial_context'] = active['context']
    return answer


def branch(directory, state, operation, args):
    if operation == 'retry_agent':
        selected = snapshot(directory, state, args['turn_id'])
        original = selected['turns'][-1]
        if original['status'] == 'running':
            raise ValueError('The selected turn is still running. Interrupt it before retrying.')
        selected['turns'] = selected['turns'][:-1]
        message = args.get('message', original['message'])
        source_turn = original['turn_id']
    else:
        selected = snapshot(directory, state, args.get('last_turn_id'))
        if 'last_turn_id' in args:
            if selected['turns'][-1]['status'] != 'completed':
                raise ValueError('last_turn_id must identify a completed turn')
        else:
            while selected['turns'] and selected['turns'][-1]['status'] != 'completed':
                selected['turns'].pop()
        message = args['message']
        source_turn = selected['turns'][-1]['turn_id'] if selected['turns'] else None
    context = choose_context(selected, args)
    options = {key: state[key] for key in ('mode', 'agent', 'project', 'timeout') if state.get(key) is not None}
    options.update(bin=state['agy_bin'], add_dir=state['add_dir'], sandbox=state['sandbox'],
                   skip_permissions=state['skip_permissions'])
    options.update(args.get('agy', {}))
    parameters = {'task_name': args['task_name'], 'message': message,
                  'agent_type': state['role'], 'cwd': args.get('cwd', state['cwd']), 'agy': options}
    for field, old in (('model', 'model'), ('reasoning_effort', 'effort')):
        value = args.get(field, state.get(old))
        if value is not None:
            parameters[field] = value
    lineage = {'operation': operation, 'agent_id': state['id'], 'turn_id': source_turn,
               'context_mode': args.get('context_mode', 'full')}
    # The source lock is released by dispatch before taking the session/name lock.
    return parameters, context, lineage


def dispatch(root, session, operation, args):
    import collaboration_api as api
    directory = api.resolve(root, session, args['target'])
    runner.snapshot(directory)
    event = None
    with runner.locked(directory):
        state = runner.read_json(directory / 'state.json')
        ensure_history(directory, state)
        if operation == 'list_turns':
            return list_turns(directory, state, args)
        if operation in ('fork_agent', 'retry_agent'):
            parameters, seed, lineage = branch(directory, state, operation, args)
        else:
            if state['status'] == 'running':
                raise ValueError('Agent is running. Interrupt it before rewinding.')
            if state['status'] == 'closed':
                raise ValueError('Agent is closed. Resume it before rewinding.')
            selected = snapshot(directory, state, args['before_turn_id'])
            selected['turns'] = selected['turns'][:-1]
            seed = choose_context(selected, args)
            archive_id = uuid.uuid4().hex
            archives = directory / 'rewinds'
            archives.mkdir(exist_ok=True, mode=0o700)
            runner.write_json(archives / (archive_id + '.json'), {'state': state, 'timestamp': time.time()})
            discarded_pending = len(state.get('pending_messages', []))
            state.update(history_seed=save_seed(directory, seed), history_head=None,
                         history_revision=state['history_revision'] + 1, run_id=None,
                         status='ready', conversation_id=None, supervisor_pid=None,
                         error=None, exit_code=None, started_at=None, finished_at=None,
                         pending_messages=[], closing=False)
            runner.write_json(directory / 'state.json', state)
            event = {'type': 'rewound', 'sender': state['task_name'], 'recipient': state['parent'],
                     'agent_id': state['id'], 'history_revision': state['history_revision'],
                     'before_turn_id': args['before_turn_id'], 'retained_turns': len(seed['turns'])}
            answer = dict(api.view(state), context_mode=args.get('context_mode', 'full'),
                          context_chars=len(render(seed)), retained_turns=len(seed['turns']),
                          archive_id=archive_id, discarded_pending_messages=discarded_pending)
    if event:
        api.publish(root, session, event)
        return answer
    answer = api.start_agent(root, session, parameters, seed_snapshot=seed, lineage=lineage, inherited_state=state)
    return dict(answer, context_mode=lineage['context_mode'], context_chars=len(render(seed)),
                replayed_turns=len(seed['turns']), forked_from=lineage)
