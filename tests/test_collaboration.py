"""Verify Codex-shaped parameters and observable collaboration semantics."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from test_lifecycle import SCRIPT

FAKE = r'''#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys, time
args = sys.argv[1:]
prompt = args[args.index('--print') + 1]
if '--input-format' in args:
    prompt = json.loads(sys.stdin.readline())['message']['content']
conversation = args[args.index('--conversation') + 1] if '--conversation' in args else 'conversation-' + str(os.getpid())
print(json.dumps({'event':'init', 'conversation_id':conversation}), flush=True)
match = re.search(r'<task>\n(.*?)\n</task>', prompt, re.S)
message = match.group(1) if match else prompt
if '\n\nMessage from ' in prompt:
    message = prompt.rsplit('\n\nMessage from ', 1)[-1].split('\n', 1)[-1]
if message in ('orphan', 'busy-orphan'):
    code = 'import time; time.sleep(20)'
    if message == 'busy-orphan':
        code = 'import time\nwhile True:\n print(\"descendant noise\", flush=True)\n time.sleep(0.05)'
    child = subprocess.Popen([sys.executable, '-u', '-c', code])
    print(json.dumps({'event':'child_pid', 'pid':child.pid}), flush=True)
    sys.exit(4)
if message == 'cleanup':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if message in ('slow', 'brief'):
    time.sleep(20 if message == 'slow' else 0.6)
print(json.dumps({'event':'result','result':{'status':'SUCCESS', 'response':prompt,
      'conversation_id':conversation, 'args':args, 'parent':os.environ.get('AGY_SUBAGENT_PARENT_PATH')}}), flush=True)
if message == 'cleanup':
    time.sleep(20)
'''


class Collaboration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'state'
        self.binary = self.base / 'agy fake'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.environment = dict(os.environ, AGY_SUBAGENT_SESSION='test-session',
                                AGY_SUBAGENT_PARENT_PATH='/root')
        self.ids = []

    def tearDown(self):
        for task_id in self.ids:
            self.call('close_agent', {'target':task_id}, check=False)
        self.temp.cleanup()

    def call(self, operation, arguments=None, check=True, environment=None):
        process = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.root), operation],
                                 input=json.dumps(arguments or {}), capture_output=True, text=True,
                                 env=environment or self.environment, timeout=12)
        if check:
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def spawn(self, name='investigate', message='hello', **kwargs):
        parameters = dict(task_name=name, message=message, fork_turns='none', cwd=str(self.base),
                          agy={'bin':str(self.binary)})
        parameters.update(kwargs)
        result = self.call('spawn_agent', parameters)
        self.ids.append(result['agent_id'])
        return result

    def complete(self, target):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            state = next(x for x in self.call('list_agents')['agents'] if x['agent_id'] == target)
            if state['status'] != 'running':
                return state
            time.sleep(0.05)
        self.fail('Agent did not finish')

    def result(self, agent_id):
        state = json.loads((self.root / agent_id / 'state.json').read_text())
        return json.loads((self.root / agent_id / state['run_id'] / 'result.json').read_text())['result']

    def test_native_parameters_defaults_and_actual_role_instructions(self):
        agent = self.spawn(agent_type='worker', model='chosen', reasoning_effort='high')
        self.assertEqual(agent['task_name'], '/root/investigate')
        self.complete(agent['agent_id'])
        result = self.result(agent['agent_id'])
        self.assertIn('Preserve edits made by others', result['response'])
        self.assertIn('chosen', result['args'])
        self.assertIn('--effort', result['args'])
        self.assertNotIn('--mode', result['args'])
        self.assertEqual(result['parent'], '/root/investigate')
        events = self.call('wait_agent', {'timeout_ms':1000})
        self.assertEqual(events['updates'][0]['agent_id'], agent['agent_id'])
        self.assertEqual(self.call('wait_agent', {'timeout_ms':0})['updates'], [])

    def test_running_messages_are_queued_and_delivered_without_manual_interrupt(self):
        agent = self.spawn(message='brief')
        receipt = self.call('send_message', {'target':'investigate', 'message':'queued-one'})
        self.assertEqual(receipt['delivery'], 'queued')
        self.call('followup_task', {'target':agent['task_name'], 'message':'queued-two'})
        self.assertEqual(self.complete(agent['agent_id'])['turn'], 2)
        output = self.result(agent['agent_id'])
        self.assertIn('queued-one', output['response'])
        self.assertIn('queued-two', output['response'])
        self.assertIn('--conversation', output['args'])

    def test_idle_send_does_not_start_until_followup(self):
        agent = self.spawn()
        self.complete(agent['agent_id'])
        self.call('send_message', {'target':'investigate', 'message':'idle inbox'})
        listing = self.call('list_agents')['agents'][0]
        self.assertEqual(listing['status'], 'completed')
        self.assertEqual(listing['pending_messages'], 1)
        self.call('followup_task', {'target':'investigate', 'message':'resume now'})
        self.assertEqual(self.complete(agent['agent_id'])['turn'], 2)
        self.assertIn('idle inbox', self.result(agent['agent_id'])['response'])

    def test_interrupt_close_resume_and_pending_queue(self):
        agent = self.spawn(message='slow')
        self.call('send_message', {'target':'investigate', 'message':'keep me'})
        state = self.call('interrupt_agent', {'target':'investigate'})
        self.assertEqual(state['previous_status'], 'running')
        self.assertEqual(state['status'], 'interrupted')
        self.assertEqual(state['pending_messages'], 1)
        self.assertEqual(self.call('close_agent', {'target':'investigate'})['status'], 'closed')
        self.assertIn('error', self.call('followup_task', {'target':'investigate', 'message':'x'}, check=False))
        self.call('resume_agent', {'target':'investigate'})
        self.call('followup_task', {'target':'investigate', 'message':'go'})
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'completed')
        self.assertIn('keep me', self.result(agent['agent_id'])['response'])

    def test_close_running_then_resume_can_execute_again(self):
        agent = self.spawn(message='slow')
        self.assertEqual(self.call('close_agent', {'target':'investigate'})['status'], 'closed')
        resumed = self.call('resume_agent', {'target':'investigate'})
        self.assertNotEqual(resumed['status'], 'running')
        self.call('followup_task', {'target':'investigate', 'message':'resume after direct close'})
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'completed')
        self.assertIn('resume after direct close', self.result(agent['agent_id'])['response'])

    def test_interrupt_during_result_cleanup_preserves_queued_work(self):
        agent = self.spawn(message='cleanup')
        directory = self.root / agent['agent_id']
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            paths = list(directory.glob('*/events.ndjson'))
            if paths and '"event": "result"' in paths[0].read_text():
                break
            time.sleep(0.02)
        else:
            self.fail('Fake agy did not produce its result before cleanup')
        self.call('send_message', {'target':'investigate', 'message':'queued after result'})
        stopped = self.call('interrupt_agent', {'target':'investigate'})
        self.assertNotEqual(stopped['status'], 'running')
        self.assertEqual(stopped['turn'], 1)
        self.assertEqual(stopped['pending_messages'], 1)

    def test_large_context_uses_stdin_and_preserves_content(self):
        context = '长上下文' * 100000
        agent = self.spawn(fork_turns='all', parent_context=context)
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'completed')
        output = self.result(agent['agent_id'])
        self.assertIn(context, output['response'])
        self.assertIn('--input-format', output['args'])
        self.assertEqual(output['args'][output['args'].index('--print') + 1], '')

    def test_exited_leader_with_open_pipe_fails_and_cleans_descendant(self):
        agent = self.spawn(message='orphan')
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'failed')
        directory = self.root / agent['agent_id']
        events = [json.loads(line) for path in directory.glob('*/events.ndjson')
                  for line in path.read_text().splitlines()]
        child_pid = next(item['pid'] for item in events if item['event'] == 'child_pid')
        process = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_pid)],
                                 capture_output=True, text=True, timeout=3)
        # A briefly unreaped zombie has already stopped executing.
        self.assertTrue(not process.stdout.strip() or process.stdout.strip().startswith('Z'))

    def test_exited_leader_with_continuously_writing_descendant_finishes(self):
        agent = self.spawn(message='busy-orphan')
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'failed')

    def test_context_fork_from_file_and_independent_none(self):
        context_file = self.base / 'context.txt'
        context_file.write_text('parent context marker')
        inherited = self.spawn('inherited', fork_turns='all', context_file=str(context_file))
        self.complete(inherited['agent_id'])
        self.assertIn('parent context marker', self.result(inherited['agent_id'])['response'])
        independent = self.spawn('independent')
        self.complete(independent['agent_id'])
        self.assertNotIn('parent context marker', self.result(independent['agent_id'])['response'])

    def test_automatic_current_chat_context_excludes_other_sessions_and_private_runtime(self):
        codex = self.base / 'codex'
        (codex / 'sessions').mkdir(parents=True)
        records = [
            {'type':'response_item', 'payload':{'type':'message', 'role':'user',
              'content':[{'type':'input_text','text':'relevant current chat'}]}},
            {'type':'response_item', 'payload':{'type':'message', 'role':'developer',
              'content':[{'type':'input_text','text':'private runtime marker'}]}},
            {'type':'response_item', 'payload':{'type':'reasoning', 'text':'private reasoning marker'}}]
        (codex / 'sessions' / 'rollout-current-thread.jsonl').write_text(
            '\n'.join(json.dumps(item) for item in records))
        (codex / 'sessions' / 'rollout-another-thread.jsonl').write_text('unrelated chat marker')
        environment = dict(self.environment, CODEX_HOME=str(codex), CODEX_THREAD_ID='current-thread')
        result = self.call('spawn_agent', {'task_name':'auto', 'message':'read context',
                    'cwd':str(self.base), 'agy':{'bin':str(self.binary)}}, environment=environment)
        self.ids.append(result['agent_id'])
        self.complete(result['agent_id'])
        response = self.result(result['agent_id'])['response']
        self.assertEqual(result['context_source'], 'codex-chat-messages')
        self.assertIn('relevant current chat', response)
        self.assertNotIn('private runtime marker', response)
        self.assertNotIn('private reasoning marker', response)
        self.assertNotIn('unrelated chat marker', response)

    def test_nested_paths_filters_and_agent_to_parent_mailbox(self):
        parent = self.spawn('parent')
        self.complete(parent['agent_id'])
        child_env = dict(self.environment, AGY_SUBAGENT_PARENT_PATH='/root/parent')
        child = self.call('spawn_agent', {'task_name':'child', 'message':'explore', 'fork_turns':'all'},
                          environment=child_env)
        self.ids.append(child['agent_id'])
        self.complete(child['agent_id'])
        self.assertEqual(child['task_name'], '/root/parent/child')
        self.assertEqual(child['context_source'], 'parent-agent-history')
        listing = self.call('list_agents', {'path_prefix':'/root/parent'})['agents']
        self.assertEqual(len(listing), 2)
        self.call('send_message', {'target':'/root', 'message':'child report'}, environment=child_env)
        child_updates = self.call('wait_agent', {'timeout_ms':0}, environment=child_env)['updates']
        root_updates = self.call('wait_agent', {'timeout_ms':0})['updates']
        self.assertTrue(any(x.get('sender') == child['task_name'] for x in child_updates))
        self.assertTrue(any(x.get('message') == 'child report' for x in root_updates))
        self.assertFalse(any(x.get('sender') == child['task_name'] for x in root_updates))

    def test_session_isolation_and_duplicate_task_names(self):
        agent = self.spawn()
        self.assertIn('error', self.call('spawn_agent', {'task_name':'investigate', 'message':'x',
            'fork_turns':'none', 'agy':{'bin':str(self.binary)}}, check=False))
        other_environment = dict(self.environment, AGY_SUBAGENT_SESSION='other-session')
        self.assertEqual(self.call('list_agents', environment=other_environment)['agents'], [])
        self.assertIn('error', self.call('send_message', {'target':agent['agent_id'], 'message':'x'},
                                       environment=other_environment, check=False))

    def test_concurrent_followups_not_lost(self):
        agent = self.spawn(message='brief')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            futures = [pool.submit(self.call, 'followup_task', {'target':'investigate','message':f'queued-{i}'})
                       for i in range(5)]
            for future in futures:
                self.assertEqual(future.result()['delivery'], 'queued')
        self.complete(agent['agent_id'])
        response = self.result(agent['agent_id'])['response']
        for i in range(5):
            self.assertIn(f'queued-{i}', response)

    def test_wait_returns_first_finished_agent_while_another_is_running(self):
        slow = self.spawn('slow_agent', message='slow')
        fast = self.spawn('fast_agent')
        updates = self.call('wait_agent', {'timeout_ms':3000})['updates']
        self.assertTrue(any(event.get('agent_id') == fast['agent_id'] for event in updates))
        state = next(item for item in self.call('list_agents')['agents'] if item['agent_id'] == slow['agent_id'])
        self.assertEqual(state['status'], 'running')

    def test_effort_mapping_and_json_file_input(self):
        parameters = dict(task_name='from_file', message='check effort', agent_type='explorer',
                          fork_turns='none', reasoning_effort='ultra', cwd=str(self.base),
                          agy={'bin':str(self.binary)})
        source = self.base / 'arguments.json'
        source.write_text(json.dumps(parameters))
        process = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.root),
                                  'spawn_agent', '--args-file', str(source)],
                                 capture_output=True, text=True, env=self.environment, timeout=5)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        agent = json.loads(process.stdout)
        self.ids.append(agent['agent_id'])
        self.assertEqual(agent['agy_effort'], 'max')
        self.complete(agent['agent_id'])
        output = self.result(agent['agent_id'])
        self.assertEqual(output['args'][output['args'].index('--effort') + 1], 'max')

    def test_wait_timeout_and_json_schema_validation(self):
        start = time.monotonic()
        self.assertTrue(self.call('wait_agent', {'timeout_ms':100})['timed_out'])
        self.assertLess(time.monotonic()-start, 1)
        for operation, args in [('wait_agent', {'timeout_ms':True}),
                                ('spawn_agent', {'task_name':'bad/path','message':'x'}),
                                ('send_message', {'target':'x','prompt':'wrong field'}),
                                ('list_agents', {'unknown':1})]:
            self.assertIn('error', self.call(operation, args, check=False))


if __name__ == '__main__':
    unittest.main()
