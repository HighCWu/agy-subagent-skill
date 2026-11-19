"""Persistent interactive agy terminals controlled through a private local socket."""
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time

import agy_subagent as runner
import conversation_store

KEYS = {'enter': '\r', 'escape': '\x1b', 'ctrl_c': '\x03', 'ctrl_d': '\x04',
        'up': '\x1b[A', 'down': '\x1b[B', 'tab': '\t'}


def clean_text(text):
    text = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', text, flags=re.S)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return text.replace('\r', '\n')


def paste(text, submit=True):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if any(ord(character) < 32 and character not in '\n\t' for character in text):
        raise ValueError('Terminal text contains control characters. Supply plain text.')
    return b'\x1b[200~' + text.encode('utf-8') + b'\x1b[201~' + (b'\r' if submit else b'')


def send(connection, value):
    connection.sendall(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')


def request(directory, value, timeout=5):
    info = runner.read_json(directory / 'terminal.json')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(info['socket'])
        send(connection, value)
        data = b''
        while b'\n' not in data:
            chunk = connection.recv(65536)
            if not chunk:
                raise ValueError('Terminal controller exited without a response')
            data += chunk
        answer = json.loads(data.split(b'\n', 1)[0])
        if answer.get('error') and 'status' not in answer:
            raise ValueError(answer['error'])
        return answer


def ensure_daemon(directory):
    try:
        request(directory, {'op': 'status'})
        return
    except (OSError, ValueError):
        pass
    with (directory / 'terminal-controller.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_daemon', str(directory)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError('Terminal controller could not start. Inspect terminal-controller.log.')
        try:
            info = runner.read_json(directory / 'terminal.json')
            if info['daemon_pid'] == process.pid:
                request(directory, {'op': 'status'})
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    process.terminate()
    raise ValueError('Terminal controller startup timed out')


def close_terminal(directory):
    try:
        return request(directory, {'op': 'close'})
    except (OSError, ValueError):
        return {'closed': True, 'running': False}


def interactive_command(state):
    command = [state['agy_bin'], '--log-file', str(Path(state['terminal_dir']) / 'agy.log')]
    if state.get('conversation_id'):
        command += ['--conversation', state['conversation_id']]
    for field in ('mode', 'model', 'effort', 'agent', 'project'):
        if state.get(field):
            command += ['--' + field, state[field]]
    for path in state['add_dir']:
        command += ['--add-dir', path]
    if state['sandbox']:
        command.append('--sandbox')
    if state['skip_permissions']:
        command.append('--dangerously-skip-permissions')
    return command


class Terminal:
    def __init__(self, directory):
        self.directory = directory
        self.master = None
        self.process = None
        self.conversation_id = None
        self.ready = False
        self.screen = ''
        self.query_buffer = ''
        self.pending = b''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.active = None
        self.last_db_poll = 0
        self.stop = False

    def start(self, state):
        self.close_child()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        environment = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor',
                           AGY_SUBAGENT_STATE_DIR=str(self.directory.parent))
        if state.get('session_id'):
            environment.update(AGY_SUBAGENT_SESSION=state['session_id'], AGY_SUBAGENT_PARENT_PATH=state['task_name'])
        def child_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        try:
            state = dict(state, terminal_dir=str(self.directory))
            self.process = subprocess.Popen(interactive_command(state), cwd=state['cwd'], env=environment,
                                            stdin=slave, stdout=slave, stderr=slave,
                                            preexec_fn=child_terminal, close_fds=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        os.set_blocking(master, False)
        self.conversation_id = state.get('conversation_id')
        self.ready = False
        self.screen = ''
        self.query_buffer = ''
        self.pending = b''
        self.decoder.reset()

    def close_child(self):
        if self.process is not None:
            runner.stop_process(self.process)
            self.process = None
        if self.master is not None:
            os.close(self.master)
            self.master = None
        self.ready = False
        self.pending = b''

    def status(self):
        tail = clean_text(self.screen)[-12000:]
        trust = tail.rfind('Do you trust the contents of this project?')
        prompt = tail.rfind('for shortcuts')
        needs_input = 'workspace_trust' if trust > prompt else None
        return {'backend': 'pty', 'terminal_pid': self.process.pid if self.process else None,
                'running': self.process is not None and self.process.poll() is None,
                'ready': self.ready, 'conversation_id': self.conversation_id,
                'active_run_id': self.active['run_id'] if self.active else None,
                'needs_input': needs_input, 'screen': tail,
                'transcript': str(self.directory / 'terminal.log')}

    def finish(self, status, result=None, error=None):
        active = self.active
        self.active = None
        if active is None:
            return
        try:
            send(active['connection'], {'status': status, 'result': result, 'error': error,
                                       'conversation_id': self.conversation_id})
        except OSError:
            pass
        active['connection'].close()

    def begin(self, connection, run_id):
        if self.active:
            raise ValueError('Terminal already has an active turn')
        if not re.fullmatch('[0-9a-f]{32}', run_id):
            raise ValueError('Invalid run ID')
        state = runner.read_json(self.directory / 'state.json')
        if state['run_id'] != run_id or state['status'] != 'running':
            raise ValueError('This turn has been superseded')
        run = self.directory / run_id
        turn = runner.read_json(run / 'request.json')
        text = '[agy-subagent-turn ' + run_id + ']\n' + turn['prompt']
        payload = paste(text)
        if self.process is None or self.process.poll() is not None or self.conversation_id != turn['conversation_id']:
            self.start(state)
        self.active = {'connection': connection, 'run_id': run_id, 'run': run, 'state': state,
                       'marker': '[agy-subagent-turn ' + run_id + ']', 'payload': payload,
                       'sent': False, 'started': time.monotonic(), 'stable': None,
                       'stable_since': None, 'cancelled_at': None}

    def read_output(self):
        try:
            data = os.read(self.master, 65536)
        except OSError as error:
            if error.errno == errno.EIO:
                return
            raise
        if not data:
            return
        with (self.directory / 'terminal.log').open('ab') as log:
            log.write(data)
        text = self.decoder.decode(data)
        self.screen = (self.screen + text)[-100000:]
        # PTY reads can split escape sequences at any byte boundary.
        queries = self.query_buffer + text
        consumed = 0
        for match in re.finditer(r'\x1b\[6n|\x1b\](10|11);\?(?:\x07|\x1b\\)', queries):
            if match.group(1):
                number = match.group(1)
                color = 'ffff/ffff/ffff' if number == '10' else '0000/0000/0000'
                self.pending += f'\x1b]{number};rgb:{color}\x07'.encode()
            else:
                self.pending += b'\x1b[1;1R'
            consumed = match.end()
        self.query_buffer = queries[consumed:][-128:]
        if 'for shortcuts' in clean_text(self.screen):
            self.ready = True

    def tick(self):
        active = self.active
        if active is None:
            if self.process is not None and self.process.poll() is not None:
                self.close_child()
            return
        run, state = active['run'], active['state']
        timeout = state.get('timeout')
        elapsed = time.monotonic() - active['started']
        if (run / 'cancel').exists() or (timeout and elapsed >= timeout):
            if active['cancelled_at'] is None:
                self.pending = b'\x03'
                active['cancelled_at'] = time.monotonic()
            elif time.monotonic() - active['cancelled_at'] >= 1:
                # Closing the owned terminal also stops tools that outlive UI cancellation.
                self.close_child()
                self.finish('timed_out' if timeout and elapsed >= timeout else 'interrupted')
            return
        if self.process is None or self.process.poll() is not None:
            self.finish('failed', error='Interactive agy exited. Inspect terminal.log and agy.log.')
            self.close_child()
            return
        if not active['sent'] and self.ready:
            self.pending += active['payload']
            active['sent'] = True
        if not active['sent'] or time.monotonic() - self.last_db_poll < 0.25:
            return
        self.last_db_poll = time.monotonic()
        data_root = Path(state.get('data_dir') or Path.home() / '.gemini/antigravity-cli')
        try:
            result = conversation_store.inspect_turn(data_root, state['cwd'], active['marker'], self.conversation_id)
            if result and result['conversation_id'] != self.conversation_id:
                self.conversation_id = result['conversation_id']
                with runner.locked(self.directory):
                    current = runner.read_json(self.directory / 'state.json')
                    if current['run_id'] == active['run_id']:
                        current['conversation_id'] = self.conversation_id
                        runner.write_json(self.directory / 'state.json', current)
            if result and result['completed']:
                signature = (result['response_step_index'], result['response'])
                if active['stable'] != signature:
                    active.update(stable=signature, stable_since=time.monotonic())
                elif time.monotonic() - active['stable_since'] >= 0.75:
                    self.finish('completed', {'status': 'SUCCESS', 'conversation_id': self.conversation_id,
                                'response': result['response'], 'num_turns': result['num_turns'],
                                'duration_seconds': elapsed, 'backend': 'pty',
                                'response_step_index': result['response_step_index']})
            else:
                active['stable'] = None
        except (ValueError, sqlite3.DatabaseError) as error:
            self.close_child()
            self.finish('failed', error=str(error))



def daemon(directory):
    terminal = Terminal(directory)
    runtime = Path(tempfile.mkdtemp(prefix='agy-pty-'))
    address = runtime / 'control.sock'
    with (directory / 'terminal.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A terminal controller already owns this agent')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(address))
            server.listen(8)
            runner.write_json(directory / 'terminal.json', {'socket': str(address), 'daemon_pid': os.getpid()})
            def stop(signum, frame):
                terminal.stop = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            try:
                while not terminal.stop:
                    readers = [server] + ([terminal.master] if terminal.master is not None else [])
                    writers = [terminal.master] if terminal.master is not None and terminal.pending else []
                    readable, writable, _ = select.select(readers, writers, [], 0.1)
                    if terminal.master is not None and terminal.master in readable:
                        terminal.read_output()
                    if terminal.master is not None and terminal.master in writable:
                        written = os.write(terminal.master, terminal.pending[:4096])
                        terminal.pending = terminal.pending[written:]
                    if server in readable:
                        connection, _ = server.accept()
                        connection.settimeout(5)
                        try:
                            raw = b''
                            while b'\n' not in raw:
                                chunk = connection.recv(65536)
                                if not chunk:
                                    raise ValueError('Missing terminal request')
                                raw += chunk
                            command = json.loads(raw.split(b'\n', 1)[0])
                            operation = command['op']
                            if operation == 'run':
                                terminal.begin(connection, command['run_id'])
                                connection = None
                            elif operation == 'status':
                                send(connection, terminal.status())
                            elif operation == 'input':
                                if not terminal.process or terminal.process.poll() is not None:
                                    raise ValueError('Terminal is not running')
                                terminal.pending += KEYS[command['key']].encode() if 'key' in command else paste(command['text'], command.get('submit', False))
                                send(connection, {'accepted': True})
                            elif operation == 'close':
                                terminal.stop = True
                                terminal.close_child()
                                terminal.finish('interrupted', error='Terminal controller closed')
                                send(connection, {'closed': True})
                            else:
                                raise ValueError('Unknown terminal operation')
                        except (ValueError, OSError, KeyError) as error:
                            if connection:
                                send(connection, {'error': str(error)})
                        finally:
                            if connection:
                                connection.close()
                    terminal.tick()
            finally:
                terminal.close_child()
                terminal.finish('interrupted', error='Terminal controller closed')
                address.unlink(missing_ok=True)
                runtime.rmdir()


def run_turn(directory, run_id):
    ensure_daemon(directory)
    return request(directory, {'op': 'run', 'run_id': run_id}, timeout=None)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '_daemon':
        raise SystemExit('Internal PTY controller entry point')
    daemon(Path(sys.argv[2]))
