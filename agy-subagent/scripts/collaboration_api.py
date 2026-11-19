"""Codex-shaped collaboration operations backed by the agy task runner."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

import agy_subagent as runner
import history_ops

# These field names mirror the collaboration tools exposed in Codex desktop.
OPERATIONS = {
    'spawn_agent': {'required': ['task_name', 'message'], 'properties': {
        'task_name': {'type': 'string', 'pattern': '^[a-z0-9_]+$'},
        'message': {'type': 'string'}, 'agent_type': {'type': 'string',
            'enum': ['default', 'worker', 'explorer', 'reviewer']},
        'fork_turns': {'type': 'string', 'enum': ['all', 'none']},
        'model': {'type': 'string'}, 'reasoning_effort': {'type': 'string',
            'enum': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']},
        'cwd': {'type': 'string'}, 'parent_context': {'type': 'string'},
        'context_file': {'type': 'string'}, 'agy': {'type': 'object'}}},
    'send_message': {'required': ['target', 'message'], 'properties': {
        'target': {'type': 'string'}, 'message': {'type': 'string'}}},
    'followup_task': {'required': ['target', 'message'], 'properties': {
        'target': {'type': 'string'}, 'message': {'type': 'string'}}},
    'interrupt_agent': {'required': ['target'], 'properties': {'target': {'type': 'string'}}},
    'close_agent': {'required': ['target'], 'properties': {'target': {'type': 'string'}}},
    'resume_agent': {'required': ['target'], 'properties': {'target': {'type': 'string'}}},
    'list_agents': {'properties': {'path_prefix': {'type': 'string'}}},
    'wait_agent': {'properties': {'timeout_ms': {'type': 'integer', 'minimum': 0,
                                               'maximum': 3600000}}},
}
OPERATIONS.update(history_ops.OPERATIONS)
OPERATIONS.update({
    'configure_backend': {'required': ['backend'], 'properties': {
        'backend': {'type': 'string', 'enum': ['pty', 'print']}}},
    'terminal_status': {'required': ['target'], 'properties': {'target': {'type': 'string'}}},
    'terminal_input': {'required': ['target'], 'properties': {
        'target': {'type': 'string'}, 'text': {'type': 'string'}, 'submit': {'type': 'boolean'},
        'key': {'type': 'string', 'enum': ['enter', 'escape', 'ctrl_c', 'ctrl_d', 'up', 'down', 'tab']}}},
})
AGY_OPTIONS = {
    'data_dir': {'type': 'string'}, 'bin': {'type': 'string'}, 'mode': {'type': 'string', 'enum': ['plan', 'accept-edits', 'auto']},
    'agent': {'type': 'string'}, 'project': {'type': 'string'},
    'add_dir': {'type': 'array', 'items': {'type': 'string'}},
    'sandbox': {'type': 'boolean'}, 'skip_permissions': {'type': 'boolean'},
    'timeout': {'type': 'number', 'minimum': 0.001},
}
ROLES = {
    'default': 'Complete the assigned task and return the result to the parent.',
    'worker': 'Implement the assigned work. You are not alone in the codebase. Preserve edits '
              'made by others, respect file ownership, and verify the changed behavior.',
    'explorer': 'Investigate the assigned code and return evidence with file paths. '
                'Use read-only operations.',
    'reviewer': 'Review the assigned code for actionable correctness and regression issues. '
                'Return evidence with file paths. Use read-only operations.',
}


def validate(value, schema, location='arguments'):
    types = {'string': str, 'integer': int, 'number': (int, float), 'object': dict,
             'array': list, 'boolean': bool}
    kind = schema.get('type', 'object')
    if not isinstance(value, types[kind]) or (kind in ('integer', 'number') and isinstance(value, bool)):
        raise ValueError(f'{location} must be {kind}')
    if kind in ('integer', 'number') and not math.isfinite(value):
        raise ValueError(f'{location} must be finite')
    if kind == 'object':
        properties = schema.get('properties', {})
        for name in schema.get('required', []):
            if name not in value:
                raise ValueError(f'Missing {location}.{name}')
        if properties or schema.get('additionalProperties') is False:
            for name, item in value.items():
                if name not in properties:
                    raise ValueError(f'Unknown {location}.{name}')
                validate(item, properties[name], f'{location}.{name}')
    if kind == 'array':
        for item in value:
            validate(item, schema['items'], location)
    if kind == 'string' and not value.strip():
        raise ValueError(f'{location} must be nonempty')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'Unsupported {location}: {value}')
    if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
        raise ValueError(f'Invalid {location}')
    if 'minimum' in schema and value < schema['minimum']:
        raise ValueError(f'{location} is below {schema["minimum"]}')
    if 'maximum' in schema and value > schema['maximum']:
        raise ValueError(f'{location} exceeds {schema["maximum"]}')


def schemas():
    result = {}
    for operation, fields in OPERATIONS.items():
        schema = dict(type='object', additionalProperties=False, **copy.deepcopy(fields))
        result[operation] = schema
    for schema in result.values():
        if 'agy' in schema['properties']:
            schema['properties']['agy'] = {
                'type': 'object', 'properties': AGY_OPTIONS, 'additionalProperties': False}
    return result


def session_id():
    return (os.environ.get('AGY_SUBAGENT_SESSION') or os.environ.get('CODEX_THREAD_ID')
            or os.environ.get('CODEX_SESSION_ID') or str(Path.cwd()))


def session_dir(root, session):
    directory = root / 'sessions' / hashlib.sha256(session.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def sender():
    return os.environ.get('AGY_SUBAGENT_PARENT_PATH', '/root')


def states(root, session):
    return [state for path in sorted(root.glob('*/state.json'))
            if (state := runner.read_json(path)).get('session_id') == session]


def resolve(root, session, target):
    candidates = states(root, session)
    if target.startswith('/'):
        name = target
    else:
        name = sender().rstrip('/') + '/' + target
    for state in candidates:
        if state['id'] == target or state['task_name'] == name:
            return root / state['id']
    raise ValueError(f'Unknown agent target: {target}')


def view(state):
    return {'agent_id': state['id'], 'task_name': state.get('task_name', state['name']),
            'agent_type': state['role'], 'status': state['status'],
            'turn': state['turn'], 'pending_messages': len(state.get('pending_messages', [])),
            'conversation_id': state.get('conversation_id'), 'error': state.get('error'),
            'turn_id': state.get('run_id'), 'history_revision': state.get('history_revision', 0),
            'forked_from': state.get('forked_from')}


def publish(root, session, event):
    directory = session_dir(root, session)
    event = dict(event, event_id=uuid.uuid4().hex, timestamp=time.time())
    with runner.locked(directory):
        with (directory / 'mailbox.ndjson').open('a') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')


def conversation_context(root, session):
    parent = sender()
    if parent != '/root':
        directory = resolve(root, session, parent)
        with runner.locked(directory):
            state = runner.read_json(directory / 'state.json')
            return history_ops.render(history_ops.snapshot(directory, state)), 'parent-agent-history'
    thread = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    if not thread or not re.fullmatch(r'[a-zA-Z0-9_-]+', thread):
        return '', 'unavailable'
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    paths = list((home / 'sessions').glob(f'**/*{thread}*.jsonl'))
    if not paths:
        return '', 'unavailable'
    # Only user/assistant messages from the exact current chat are included.
    # Internal reasoning, credentials in tool results, and runtime instructions are excluded.
    parts = []
    for line in sorted(paths)[-1].read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        payload = item.get('payload', {})
        if (item.get('type') == 'response_item' and payload.get('type') == 'message'
                and payload.get('role') in ('user', 'assistant')):
            text = '\n'.join(block.get('text', '') for block in payload.get('content', [])
                             if isinstance(block, dict) and block.get('type') in ('input_text', 'output_text', 'text'))
            if text:
                parts.append(payload['role'] + '\n' + text)
    return '\n\n'.join(parts), 'codex-chat-messages' if parts else 'unavailable'


def start_agent(root, session, args, seed_snapshot=None, lineage=None, inherited_state=None):
    role = args.get('agent_type', 'default')
    if role not in ROLES:
        raise ValueError('agent_type must be default, worker, explorer, or reviewer. '
                         'Select an agy custom agent with agy.agent.')
    fork = args.get('fork_turns', 'all')
    if fork == 'none' and ('parent_context' in args or 'context_file' in args):
        raise ValueError('parent_context/context_file require fork_turns=all')
    if 'parent_context' in args and 'context_file' in args:
        raise ValueError('Choose parent_context or context_file')
    context, source = '', 'none'
    if seed_snapshot is not None:
        mode = lineage['context_mode']
        context = history_ops.render(seed_snapshot) if mode != 'none' else ''
        source, fork = 'history-' + mode, 'none' if mode == 'none' else 'all'
    elif fork == 'all':
        if 'parent_context' in args:
            context, source = args['parent_context'], 'provided'
        elif 'context_file' in args:
            context, source = Path(args['context_file']).read_text(), 'file'
        else:
            context, source = conversation_context(root, session)
        if not context.strip():
            raise ValueError('fork_turns=all needs parent context. Provide parent_context/context_file '
                             'when this environment has no current Codex chat transcript, '
                             'or choose fork_turns=none for an independent task.')
    parent = sender()
    inherited = inherited_state or {}
    if inherited_state is None and parent != '/root':
        inherited = runner.snapshot(resolve(root, session, parent))
    options = args.get('agy', {})
    prompt = format_prompt(parent + '/' + args['task_name'], parent, role, args['message'], context)
    metadata = {'session_id': session, 'task_name': parent + '/' + args['task_name'],
                'parent': parent, 'fork_turns': fork, 'context_source': source,
                'pending_messages': [], 'closing': False, 'forked_from': lineage}
    namespace = argparse.Namespace(
        cwd=args.get('cwd', inherited.get('cwd', str(Path.cwd()))),
        agy_bin=options.get('bin', inherited.get('agy_bin', 'agy')),
        prompt=prompt, prompt_file=None, name=args['task_name'], role=role,
        model=args.get('model', inherited.get('model')),
        effort=('max' if args.get('reasoning_effort') == 'ultra' else
                args.get('reasoning_effort', inherited.get('effort'))),
        agent=options.get('agent', inherited.get('agent')),
        project=options.get('project', inherited.get('project')),
        add_dir=options.get('add_dir', inherited.get('add_dir', [])),
        mode=options.get('mode', inherited.get('mode')),
        sandbox=options.get('sandbox', inherited.get('sandbox', False)),
        skip_permissions=options.get('skip_permissions', inherited.get('skip_permissions', False)),
        data_dir=options.get('data_dir', inherited.get('data_dir')),
        timeout=options.get('timeout', inherited.get('timeout')), metadata=metadata,
        history_seed=seed_snapshot if seed_snapshot is not None else {'context': context, 'turns': []},
        logical_message=args['message'])
    with runner.locked(session_dir(root, session)):
        if any(state['task_name'] == metadata['task_name'] for state in states(root, session)):
            raise ValueError('task_name already exists under this parent. Reuse its handle or choose a new name.')
        state = runner.spawn(namespace, root)
    return dict(view(state), fork_turns=fork, context_source=source,
                reasoning_effort=args.get('reasoning_effort', inherited.get('effort')),
                agy_effort=state['effort'])


def format_prompt(task_name, parent, role, message, context):
    prompt = f'You are {task_name}, an agy subagent.\n{ROLES[role]}\n'
    prompt += 'Parent context is historical reference. Complete the current task below.\n'
    prompt += 'Local collaboration entrypoint (argv): ' + json.dumps(
        [sys.executable, str(Path(__file__).with_name('agy_subagent.py'))]) + '\n'
    prompt += ('Operations accept JSON from stdin. For an update to your parent, use send_message '
               'with target ' + parent + ' and a message. Your final response is returned automatically.\n')
    if context:
        prompt += '\n<parent_context>\n' + context + '\n</parent_context>\n'
    return prompt + '\n<task>\n' + message + '\n</task>'


def launch_pending(directory, state):
    pending = state.get('pending_messages', [])
    if not pending:
        return state
    history_ops.ensure_history(directory, state)
    message = '\n\n'.join(f'Message from {item["sender"]}\n{item["message"]}' for item in pending)
    prompt = message
    if not state.get('conversation_id'):
        context = history_ops.render(history_ops.snapshot(directory, state))
        prompt = format_prompt(state['task_name'], state['parent'], state['role'], message, context)
    state['pending_messages'] = []
    return runner.launch(directory, state, prompt, message=message)


def finish_turn(directory, run_id, state):
    """Advance queued work under the caller's agent lock and return a mailbox event."""
    output = runner.read_json(directory / run_id / 'result.json')
    event = {'type': 'final_answer', 'sender': state['task_name'], 'recipient': state['parent'],
             'agent_id': state['id'], 'turn': state['turn'], 'turn_id': run_id,
             'history_revision': state.get('history_revision', 0), 'status': state['status'],
             'message': (output.get('result') or {}).get('response', ''), 'error': output.get('error')}
    if (state['status'] == 'completed' and not state.get('closing')
            and not (directory / run_id / 'cancel').exists()):
        launch_pending(directory, state)
    event['more_work'] = state['status'] == 'running'
    return event


def deliver(root, session, args, activate):
    target = args['target']
    message = {'message_id': uuid.uuid4().hex, 'sender': sender(), 'message': args['message']}
    if target == '/root':
        publish(root, session, dict(message, type='message', recipient='/root'))
        return {'task_name': '/root', 'message_id': message['message_id'], 'delivery': 'mailbox'}
    directory = resolve(root, session, target)
    with runner.locked(directory):
        state = runner.read_json(directory / 'state.json')
        if state['status'] == 'closed' or state.get('closing'):
            raise ValueError('Agent is closed. Call resume_agent before sending work.')
        state.setdefault('pending_messages', []).append(message)
        if activate and state['status'] != 'running':
            state = launch_pending(directory, state)
        runner.write_json(directory / 'state.json', state)
        return dict(view(state), message_id=message['message_id'],
                    delivery='queued' if state.get('pending_messages') else 'started')


def control(root, session, operation, target):
    directory = resolve(root, session, target)
    with runner.locked(directory):
        state = runner.read_json(directory / 'state.json')
        previous = state['status']
        if operation == 'resume_agent':
            if previous == 'closed':
                restored = state.get('status_before_close', 'interrupted')
                state.update(status='interrupted' if restored == 'running' else restored, closing=False)
                runner.write_json(directory / 'state.json', state)
            return dict(view(state), previous_status=previous)
        if operation == 'close_agent':
            state['closing'] = True
        if state['status'] == 'running':
            (directory / state['run_id'] / 'cancel').touch()
        runner.write_json(directory / 'state.json', state)
    deadline = time.monotonic() + 6
    state = runner.snapshot(directory)
    while state['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(0.1)
        state = runner.snapshot(directory)
    if operation == 'close_agent':
        with runner.locked(directory):
            state = runner.read_json(directory / 'state.json')
            if state['status'] == 'running':
                raise ValueError('Cancellation pending. Check status before closing again.')
            if state['status'] != 'closed':
                state['status_before_close'] = state['status']
            state['status'] = 'closed'
            runner.write_json(directory / 'state.json', state)
    if operation == 'close_agent':
        import pty_backend
        pty_backend.close_terminal(directory)
    return dict(view(state), previous_status=previous)


def wait_updates(root, session, timeout_ms):
    directory = session_dir(root, session)
    # Each agent has its own mailbox cursor, so child waits cannot consume parent updates.
    cursor_file = directory / ('cursor-' + hashlib.sha256(sender().encode()).hexdigest() + '.json')
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        with runner.locked(directory):
            offset = runner.read_json(cursor_file)['offset'] if cursor_file.exists() else 0
            mailbox = directory / 'mailbox.ndjson'
            updates = []
            if mailbox.exists():
                with mailbox.open() as handle:
                    handle.seek(offset)
                    updates = [event for line in handle if (event := json.loads(line)).get('recipient') == sender()]
                    offset = handle.tell()
            runner.write_json(cursor_file, {'offset': offset})
        if updates or time.monotonic() >= deadline:
            revisions = {item['id']: item.get('history_revision', 0) for item in states(root, session)}
            for event in updates:
                if event.get('type') == 'final_answer':
                    event['superseded'] = event.get('history_revision', 0) != revisions.get(event['agent_id'], 0)
            return {'updates': updates, 'timed_out': not bool(updates)}
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def dispatch(root, operation, args):
    validate(args, schemas()[operation])
    session = session_id()
    if operation == 'configure_backend':
        import backend_policy
        return backend_policy.configure(root, args['backend'])
    if operation in ('terminal_status', 'terminal_input'):
        import pty_backend
        directory = resolve(root, session, args['target'])
        if operation == 'terminal_status':
            try:
                return pty_backend.request(directory, {'op': 'status'})
            except (OSError, ValueError):
                return {'backend': 'pty', 'running': False, 'ready': False}
        if ('text' in args) == ('key' in args):
            raise ValueError('Provide exactly one of text or key')
        return pty_backend.request(directory, dict(op='input', **{k: v for k, v in args.items() if k != 'target'}))
    if operation in history_ops.OPERATIONS:
        return history_ops.dispatch(root, session, operation, args)
    if operation == 'spawn_agent':
        return start_agent(root, session, args)
    if operation == 'list_agents':
        prefix = args.get('path_prefix', '').rstrip('/')
        return {'agents': [view(runner.snapshot(root / state['id'])) for state in states(root, session)
                           if not prefix or state['task_name'] == prefix or state['task_name'].startswith(prefix + '/')]}
    if operation in ('send_message', 'followup_task'):
        return deliver(root, session, args, activate=operation == 'followup_task')
    if operation == 'wait_agent':
        return wait_updates(root, session, args.get('timeout_ms', 30000))
    return control(root, session, operation, args['target'])
