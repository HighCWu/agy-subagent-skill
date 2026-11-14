#!/usr/bin/env python3
"""Durable local task handles for Antigravity CLI. Python 3.10+, POSIX."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


@contextmanager
def locked(directory):
    with (directory / 'lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def task_dir(root, task_id):
    # Handles cannot escape the state directory.
    if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
        raise ValueError('Invalid task ID')
    directory = root / task_id
    if not (directory / 'state.json').is_file():
        raise ValueError('Unknown task ID')
    return directory


def snapshot(directory):
    with locked(directory):
        state = read_json(directory / 'state.json')
        if state['status'] == 'running' and state.get('supervisor_pid'):
            try:
                os.kill(state['supervisor_pid'], 0)
            except ProcessLookupError:
                state.update(status='failed', error='Supervisor exited without a final result')
                write_json(directory / 'state.json', state)
        return state


def prompt_from(args):
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt or not prompt.strip():
        raise ValueError('Provide a nonempty --prompt or --prompt-file')
    return prompt


def launch(directory, state, prompt):
    run_id = uuid.uuid4().hex
    run = directory / run_id
    run.mkdir(mode=0o700)
    write_json(run / 'request.json', {'prompt': prompt, 'conversation_id': state.get('conversation_id')})
    state.update(status='running', run_id=run_id, started_at=time.time(),
                 error=None, exit_code=None, supervisor_pid=None, finished_at=None)
    state['turn'] = state.get('turn', 0) + 1
    write_json(directory / 'state.json', state)
    try:
        with (run / 'supervisor.log').open('ab') as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--state-dir', str(directory.parent),
                 '_run', state['id'], run_id], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, close_fds=True)
        state['supervisor_pid'] = process.pid
    except OSError as error:
        state.update(status='failed', error=str(error))
    write_json(directory / 'state.json', state)
    return state


def spawn(args, root):
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('Working directory does not exist')
    binary = shutil.which(args.agy_bin)
    if not binary:
        raise ValueError('agy executable not found. Install and authenticate Antigravity CLI first.')
    prompt = prompt_from(args)
    task_id = uuid.uuid4().hex
    directory = root / task_id
    directory.mkdir(mode=0o700)
    state = dict(id=task_id, name=args.name, role=args.role, cwd=str(cwd),
                 agy_bin=str(Path(binary).resolve()), model=args.model, effort=args.effort,
                 agent=args.agent, project=args.project, add_dir=args.add_dir,
                 mode=args.mode or ('accept-edits' if args.role == 'worker' else 'plan'),
                 sandbox=args.sandbox, skip_permissions=args.skip_permissions,
                 timeout=args.timeout, conversation_id=None)
    with locked(directory):
        return launch(directory, state, prompt)


def build_command(state, request):
    command = [state['agy_bin'], '--print', request['prompt'], '--output-format', 'stream-json',
               '--disable-slash-commands', '--mode', state['mode'], '--print-timeout', '0']
    if request['conversation_id']:
        command += ['--conversation', request['conversation_id']]
    for field in ('model', 'effort', 'agent', 'project'):
        if state[field]:
            command += ['--' + field, state[field]]
    for directory in state['add_dir']:
        command += ['--add-dir', directory]
    if state['sandbox']:
        command.append('--sandbox')
    if state['skip_permissions']:
        command.append('--dangerously-skip-permissions')
    return command


def stop_process(process):
    # Only signal the process group created by this supervisor.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def supervise(root, args):
    directory = task_dir(root, args.id)
    run = directory / args.run_id
    state = snapshot(directory)
    request = read_json(run / 'request.json')
    interrupted = False

    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    result = None
    conversation_id = request['conversation_id']
    status = 'failed'
    error = None
    process = None
    try:
        with (run / 'events.ndjson').open('ab') as events, (run / 'stderr.log').open('ab') as stderr:
            process = subprocess.Popen(build_command(state, request), cwd=state['cwd'],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=stderr, start_new_session=True, close_fds=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = b''
            deadline = time.monotonic() + state['timeout']
            eof = False
            while not eof:
                if interrupted or (run / 'cancel').exists():
                    status = 'interrupted'
                    break
                if time.monotonic() >= deadline:
                    status = 'timed_out'
                    error = 'Turn exceeded the configured timeout'
                    break
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        eof = True
                        break
                    events.write(chunk)
                    events.flush()
                    pending += chunk
                    lines = pending.split(b'\n')
                    pending = lines.pop()
                    for line in lines:
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        found_id = event.get('conversation_id')
                        if found_id and found_id != conversation_id:
                            conversation_id = found_id
                            with locked(directory):
                                current = read_json(directory / 'state.json')
                                current['conversation_id'] = conversation_id
                                write_json(directory / 'state.json', current)
                        if event.get('event') == 'result' and isinstance(event.get('result'), dict):
                            result = event['result']
                if result is not None:
                    # The structured result is the turn boundary, even if CLI cleanup stalls.
                    break
            selector.close()
            if result is not None and status not in ('interrupted', 'timed_out'):
                conversation_id = result.get('conversation_id') or conversation_id
                response = result.get('response')
                if result.get('status') == 'SUCCESS' and isinstance(response, str) and response.strip():
                    status = 'completed'
                else:
                    error = result.get('error') or 'agy returned an unsuccessful or empty result'
            elif status == 'failed':
                error = 'agy exited without a structured result. Inspect events.ndjson and stderr.log.'
    except Exception as exception:
        error = str(exception)
        status = 'failed'
    finally:
        if process is not None:
            stop_process(process)
        write_json(run / 'result.json', {'status': status, 'result': result, 'error': error})
        with locked(directory):
            current = read_json(directory / 'state.json')
            if current['run_id'] == args.run_id:
                current.update(status=status, conversation_id=conversation_id, error=error,
                               finished_at=time.time(), exit_code=process.returncode if process else None)
                write_json(directory / 'state.json', current)


def finite_positive(value):
    import math
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Must be a finite number greater than zero')
    return number


def add_prompt(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prompt')
    group.add_argument('--prompt-file')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get(
        'AGY_SUBAGENT_STATE_DIR', str(Path.home() / '.local/state/agy-subagent')))
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('spawn', help='Start a background task')
    add_prompt(start)
    start.add_argument('--cwd', required=True)
    start.add_argument('--name', default='agy')
    start.add_argument('--role', choices=['default', 'worker', 'explorer', 'reviewer'], default='default')
    start.add_argument('--agy-bin', default='agy')
    start.add_argument('--model')
    start.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    start.add_argument('--agent')
    start.add_argument('--project')
    start.add_argument('--add-dir', action='append', default=[])
    start.add_argument('--mode', choices=['plan', 'accept-edits', 'auto'])
    start.add_argument('--sandbox', action='store_true')
    start.add_argument('--skip-permissions', action='store_true')
    start.add_argument('--timeout', type=finite_positive, default=600)
    commands.add_parser('list')
    commands.add_parser('doctor')
    for name in ('status', 'result', 'interrupt', 'close', 'send', 'wait'):
        command = commands.add_parser(name)
        command.add_argument('id')
        if name == 'send':
            add_prompt(command)
        if name == 'wait':
            command.add_argument('--timeout', type=finite_positive, default=30)
    worker = commands.add_parser('_run', help=argparse.SUPPRESS)
    worker.add_argument('id')
    worker.add_argument('run_id')
    args = parser.parse_args()
    root = Path(args.state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == '_run':
        supervise(root, args)
        return
    if args.command == 'doctor':
        binary = shutil.which('agy')
        emit({'agy': binary, 'python': sys.version.split()[0], 'state_dir': str(root),
              'platform': sys.platform, 'ready': bool(binary),
              'next': 'Check agy --help and agy models. Authentication is checked on the first turn.'})
        return
    if args.command == 'spawn':
        emit(spawn(args, root))
        return
    if args.command == 'list':
        emit([snapshot(p.parent) for p in sorted(root.glob('*/state.json'))])
        return
    directory = task_dir(root, args.id)
    if args.command == 'status':
        emit(snapshot(directory))
    elif args.command == 'wait':
        deadline = time.monotonic() + min(args.timeout, 60)
        state = snapshot(directory)
        while state['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.2)
            state = snapshot(directory)
        emit(state)
    elif args.command == 'result':
        state = snapshot(directory)
        path = directory / state['run_id'] / 'result.json'
        emit({'task': state, 'output': read_json(path) if path.exists() else None,
              'artifacts_dir': str(path.parent)})
    elif args.command == 'send':
        prompt = prompt_from(args)
        with locked(directory):
            state = read_json(directory / 'state.json')
            if state['status'] == 'running':
                raise ValueError('Task is running. Wait, or interrupt it before sending a follow-up.')
            if state['status'] == 'closed':
                raise ValueError('Task is closed. Spawn a new task.')
            if not state.get('conversation_id'):
                raise ValueError('No conversation ID captured. Spawn a new task with explicit context.')
            emit(launch(directory, state, prompt))
    elif args.command in ('interrupt', 'close'):
        with locked(directory):
            state = read_json(directory / 'state.json')
            if state['status'] == 'running':
                (directory / state['run_id'] / 'cancel').touch()
        deadline = time.monotonic() + 6
        state = snapshot(directory)
        while state['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.1)
            state = snapshot(directory)
        if args.command == 'close':
            with locked(directory):
                state = read_json(directory / 'state.json')
                if state['status'] == 'running':
                    raise ValueError('Cancellation pending. Check status before closing again.')
                state['status'] = 'closed'
                write_json(directory / 'state.json', state)
        emit(state)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        emit({'error': str(error)})
        sys.exit(1)
