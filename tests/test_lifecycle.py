"""Exercise the public CLI against a deterministic fake agy process."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'agy-subagent/scripts/agy_subagent.py'
FAKE = r'''#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
prompt = args[args.index('--print') + 1]
conversation = args[args.index('--conversation') + 1] if '--conversation' in args else 'test-conversation'
print(json.dumps({'event': 'init', 'conversation_id': conversation}), flush=True)
if prompt == 'slow':
    time.sleep(20)
if prompt == 'fail':
    print('permission required', file=sys.stderr)
    sys.exit(4)
if prompt == 'malformed':
    print('not json', flush=True)
    sys.exit(0)
response = '' if prompt == 'empty' else prompt
print(json.dumps({'event': 'result', 'result': {'conversation_id': conversation,
      'status': 'SUCCESS', 'response': response, 'args': args}}), flush=True)
if prompt == 'cleanup-stall':
    time.sleep(20)
'''


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'state'
        self.binary = self.base / 'fake agy'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.ids = []

    def tearDown(self):
        for task_id in self.ids:
            self.call('close', task_id, check=False)
        self.temp.cleanup()

    def call(self, *args, check=True):
        process = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.root), *args],
                                 capture_output=True, text=True, timeout=12)
        if check:
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def spawn(self, prompt='hello', *extra):
        state = self.call('spawn', '--cwd', str(self.base), '--agy-bin', str(self.binary),
                          '--prompt', prompt, *extra)
        self.ids.append(state['id'])
        return state['id']

    def done(self, task_id):
        return self.call('wait', task_id, '--timeout', '7')

    def test_completion_resume_and_literal_arguments(self):
        prompt = '中文 `touch SHOULD_NOT_EXIST` $(touch SHOULD_NOT_EXIST)\n--model fake'
        task_id = self.spawn(prompt, '--role', 'worker', '--model', 'chosen', '--effort', 'high')
        self.assertEqual(self.done(task_id)['status'], 'completed')
        output = self.call('result', task_id)['output']['result']
        self.assertEqual(output['response'], prompt)
        self.assertIn('accept-edits', output['args'])
        self.assertNotIn('--dangerously-skip-permissions', output['args'])
        self.assertFalse((self.base / 'SHOULD_NOT_EXIST').exists())
        resumed = self.call('send', task_id, '--prompt', 'follow-up')
        self.assertIsNone(resumed['finished_at'])
        self.assertEqual(self.done(task_id)['turn'], 2)
        result = self.call('result', task_id)['output']['result']
        self.assertIn('--conversation', result['args'])
        self.assertEqual(result['conversation_id'], 'test-conversation')

    def test_timeout_and_interrupt_and_close(self):
        task_id = self.spawn('slow', '--timeout', '0.5')
        self.assertEqual(self.done(task_id)['status'], 'timed_out')
        other = self.spawn('slow')
        # Wait for init so interruption retains a resumable ID.
        deadline = time.monotonic() + 3
        while not self.call('status', other)['conversation_id'] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.call('interrupt', other)['status'], 'interrupted')
        self.call('send', other, '--prompt', 'after interrupt')
        self.assertEqual(self.done(other)['status'], 'completed')
        self.assertEqual(self.call('close', other)['status'], 'closed')
        self.assertIn('closed', self.call('send', other, '--prompt', 'x', check=False)['error'])

    def test_failed_and_empty_outputs(self):
        for prompt in ('fail', 'empty', 'malformed'):
            task_id = self.spawn(prompt)
            self.assertEqual(self.done(task_id)['status'], 'failed')
            self.assertTrue(self.call('result', task_id)['output']['error'])

    def test_concurrent_tasks_and_overlapping_send(self):
        slow = self.spawn('slow')
        fast = self.spawn()
        self.assertEqual(self.done(fast)['status'], 'completed')
        self.assertEqual(self.call('status', slow)['status'], 'running')
        self.assertIn('running', self.call('send', slow, '--prompt', 'x', check=False)['error'])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            futures = [pool.submit(self.call, 'send', fast, '--prompt', 'slow', check=False)
                       for _ in range(2)]
            responses = [f.result() for f in futures]
        self.assertEqual(sum(bool(r.get('error')) for r in responses), 1)
        self.assertEqual(len(self.call('list')), 2)

    def test_cleanup_stall_does_not_stall_result(self):
        task_id = self.spawn('cleanup-stall')
        self.assertEqual(self.done(task_id)['status'], 'completed')

    def test_invalid_ids_and_missing_binary(self):
        self.assertIn('error', self.call('status', '../other', check=False))
        result = self.call('spawn', '--cwd', str(self.base), '--agy-bin', '/missing/agy',
                           '--prompt', 'x', check=False)
        self.assertIn('not found', result['error'])


if __name__ == '__main__':
    unittest.main()
