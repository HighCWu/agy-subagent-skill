"""Exercise historical forks, retries, rewind isolation, and summary-only context."""
import concurrent.futures
import json
import time
import unittest

import test_collaboration as fixtures


class History(unittest.TestCase):
    tearDown = fixtures.Collaboration.tearDown
    call = fixtures.Collaboration.call
    spawn = fixtures.Collaboration.spawn
    complete = fixtures.Collaboration.complete
    result = fixtures.Collaboration.result

    def setUp(self):
        fixtures.Collaboration.setUp(self)
        # Keep answers distinct from inputs so leakage of discarded answers is observable.
        fake = fixtures.FAKE.replace("'response':prompt,", "'response':'ANSWER:' + message, 'received_prompt':prompt,")
        fake = fake.replace("if message in ('orphan', 'busy-orphan'):",
                            "if message.startswith('Message from '):\n    message = message.split('\\n', 1)[-1]\nif message in ('orphan', 'busy-orphan'):")
        self.binary.write_text(fake)

    def history(self, target='investigate', **options):
        return self.call('list_turns', dict(target=target, **options))

    def advance(self, target, message):
        self.call('followup_task', {'target':target, 'message':message})
        self.complete(target)
        return self.history(target)['turns'][-1]['turn_id']

    def three_turns(self):
        agent = self.spawn(message='ORIGINAL_A')
        self.complete(agent['agent_id'])
        first = self.history()['turns'][0]['turn_id']
        second = self.advance(agent['agent_id'], 'ORIGINAL_B')
        third = self.advance(agent['agent_id'], 'FUTURE_SECRET_C')
        return agent, first, second, third

    def fork(self, operation='fork_agent', **options):
        parameters = {'target':'investigate', 'task_name':'alternative', **options}
        result = self.call(operation, parameters)
        self.ids.append(result['agent_id'])
        self.assertEqual(self.complete(result['agent_id'])['status'], 'completed')
        return result

    def test_list_turns_provides_stable_ids_and_optional_content(self):
        agent, first, second, third = self.three_turns()
        history = self.history()
        self.assertEqual([item['turn_id'] for item in history['turns']], [first, second, third])
        self.assertEqual([item['turn'] for item in history['turns']], [1, 2, 3])
        self.assertNotIn('message', history['turns'][0])
        detailed = self.history(include_content=True)
        self.assertEqual(detailed['turns'][0]['message'], 'ORIGINAL_A')
        self.assertEqual(detailed['turns'][0]['response'], 'ANSWER:ORIGINAL_A')
        events = self.call('wait_agent', {'timeout_ms':1000})['updates']
        self.assertTrue(all(item.get('turn_id') in [first, second, third] for item in events))

    def test_fork_at_turn_excludes_future_and_uses_new_conversation(self):
        agent, first, second, third = self.three_turns()
        source_conversation = self.result(agent['agent_id'])['conversation_id']
        branch = self.fork(last_turn_id=first, message='TRY_DIFFERENT_APPROACH',
                           model='alternative-model', reasoning_effort='high')
        result = self.result(branch['agent_id'])
        self.assertIn('ORIGINAL_A', result['received_prompt'])
        self.assertIn('ANSWER:ORIGINAL_A', result['received_prompt'])
        self.assertNotIn('ORIGINAL_B', result['received_prompt'])
        self.assertNotIn('FUTURE_SECRET_C', result['received_prompt'])
        self.assertNotIn('--conversation', result['args'])
        self.assertNotEqual(result['conversation_id'], source_conversation)
        self.assertIn('alternative-model', result['args'])
        self.assertEqual(branch['replayed_turns'], 1)
        self.assertEqual(len(self.history()['turns']), 3)
        self.assertEqual(self.history(branch['agent_id'])['turns'][0]['turn_id'], first)
        self.advance(branch['agent_id'], 'CONTINUE_BRANCH')
        self.assertIn('--conversation', self.result(branch['agent_id'])['args'])

    def test_retry_reuses_input_and_excludes_answer_and_later_turns(self):
        agent, first, second, third = self.three_turns()
        branch = self.fork('retry_agent', turn_id=second)
        prompt = self.result(branch['agent_id'])['received_prompt']
        self.assertIn('ORIGINAL_B', prompt)
        self.assertIn('ANSWER:ORIGINAL_A', prompt)
        self.assertNotIn('ANSWER:ORIGINAL_B', prompt)
        self.assertNotIn('FUTURE_SECRET_C', prompt)
        self.assertEqual(branch['replayed_turns'], 1)

    def test_retry_replacement_message_and_summary_context(self):
        agent, first, second, third = self.three_turns()
        branch = self.fork('retry_agent', turn_id=second, message='REPLACEMENT_INPUT',
                           context_mode='summary', context_summary='ONLY_KEEP_THIS_SUMMARY')
        prompt = self.result(branch['agent_id'])['received_prompt']
        self.assertIn('ONLY_KEEP_THIS_SUMMARY', prompt)
        self.assertIn('REPLACEMENT_INPUT', prompt)
        self.assertNotIn('ORIGINAL_A', prompt)
        self.assertNotIn('ORIGINAL_B', prompt)
        self.assertNotIn('FUTURE_SECRET_C', prompt)
        self.assertEqual(branch['replayed_turns'], 0)

    def test_summary_and_none_exclude_original_parent_context(self):
        agent = self.spawn(message='LARGE_INPUT', fork_turns='all', parent_context='LONG_PARENT_' * 30000)
        self.complete(agent['agent_id'])
        full = self.fork(task_name='full', message='NEXT')
        summary = self.fork(task_name='summary', message='NEXT', context_mode='summary',
                            context_summary='SHORT_SUMMARY')
        empty = self.fork(task_name='empty', message='NEXT', context_mode='none')
        self.assertLess(summary['context_chars'], full['context_chars'] / 100)
        self.assertNotIn('LONG_PARENT_', self.result(summary['agent_id'])['received_prompt'])
        self.assertNotIn('LARGE_INPUT', self.result(empty['agent_id'])['received_prompt'])
        self.assertEqual(summary['replayed_turns'], 0)
        self.assertEqual(empty['replayed_turns'], 0)

    def test_rewind_before_turn_and_continue_preserves_archived_history(self):
        agent, first, second, third = self.three_turns()
        before = self.result(agent['agent_id'])['conversation_id']
        self.call('send_message', {'target':'investigate', 'message':'DISCARDED_QUEUED_WORK'})
        rewound = self.call('rewind_agent', {'target':'investigate','before_turn_id':second})
        self.assertEqual(rewound['status'], 'ready')
        self.assertEqual(rewound['retained_turns'], 1)
        self.assertEqual(rewound['discarded_pending_messages'], 1)
        self.assertIsNone(rewound['conversation_id'])
        self.assertEqual(self.history()['turns'][0]['turn_id'], first)
        self.advance(agent['agent_id'], 'NEW_DIRECTION_D')
        result = self.result(agent['agent_id'])
        self.assertNotEqual(result['conversation_id'], before)
        self.assertNotIn('--conversation', result['args'])
        self.assertIn('ANSWER:ORIGINAL_A', result['received_prompt'])
        self.assertNotIn('ORIGINAL_B', result['received_prompt'])
        self.assertNotIn('FUTURE_SECRET_C', result['received_prompt'])
        self.assertNotIn('DISCARDED_QUEUED_WORK', result['received_prompt'])
        archived = self.history(include_discarded=True)['turns']
        self.assertEqual(len(archived), 4)
        self.assertFalse(next(item for item in archived if item['turn_id'] == second)['active'])
        restored = self.fork(task_name='old_branch', last_turn_id=third, message='RESTORE_OLD_PATH')
        self.assertIn('FUTURE_SECRET_C', self.result(restored['agent_id'])['received_prompt'])

    def test_rewind_first_turn_and_stale_mailbox_events(self):
        agent, first, second, third = self.three_turns()
        self.call('rewind_agent', {'target':'investigate', 'before_turn_id':first})
        self.assertEqual(self.history()['turns'], [])
        events = self.call('wait_agent', {'timeout_ms':1000})['updates']
        self.assertEqual(sum(event.get('superseded', False) for event in events), 3)
        self.assertTrue(any(event['type'] == 'rewound' for event in events))
        # The old result API has a meaningful empty result while the handle is ready.
        import subprocess, sys
        process = subprocess.run([sys.executable, str(fixtures.SCRIPT), '--state-dir',str(self.root),
                                  'result',agent['agent_id']],capture_output=True,text=True,timeout=5)
        self.assertEqual(process.returncode,0, process.stderr)
        self.assertIsNone(json.loads(process.stdout)['output'])
        self.advance(agent['agent_id'], 'FRESH_INPUT')
        self.assertNotIn('ORIGINAL_A', self.result(agent['agent_id'])['received_prompt'])

    def test_nested_context_and_fork_after_rewind_use_active_history(self):
        agent, first, second, third = self.three_turns()
        self.call('rewind_agent', {'target':'investigate','before_turn_id':second})
        child_env = dict(self.environment, AGY_SUBAGENT_PARENT_PATH='/root/investigate')
        child = self.call('spawn_agent',{'task_name':'child','message':'USE_ACTIVE_CONTEXT'}, environment=child_env)
        self.ids.append(child['agent_id'])
        self.complete(child['agent_id'])
        prompt = self.result(child['agent_id'])['received_prompt']
        self.assertIn('ORIGINAL_A', prompt)
        self.assertNotIn('ORIGINAL_B', prompt)
        self.assertNotIn('FUTURE_SECRET_C', prompt)
        branch = self.fork(message='ALTERNATIVE_FROM_ACTIVE')
        self.assertNotIn('ORIGINAL_B', self.result(branch['agent_id'])['received_prompt'])

    def test_source_can_be_running_when_forking_a_completed_turn(self):
        agent = self.spawn(message='EARLY_SUCCESS')
        self.complete(agent['agent_id'])
        first = self.history()['turns'][0]['turn_id']
        self.call('followup_task',{'target':'investigate','message':'slow'})
        branch = self.fork(last_turn_id=first,message='SAFE_PARALLEL_BRANCH')
        self.assertEqual(self.history()['turns'][-1]['status'],'running')
        self.assertNotIn('slow', self.result(branch['agent_id'])['received_prompt'])
        self.assertIn('error',self.call('rewind_agent',{'target':'investigate','before_turn_id':first},check=False))

    def test_rewind_with_summary_restarts_using_only_selected_summary(self):
        agent, first, second, third = self.three_turns()
        self.call('rewind_agent', {'target':'investigate', 'before_turn_id':third,
                                  'context_mode':'summary', 'context_summary':'COMPACT_STATE_ONLY'})
        self.assertEqual(self.history()['turns'], [])
        self.advance(agent['agent_id'], 'CONTINUE_COMPACTED')
        prompt = self.result(agent['agent_id'])['received_prompt']
        self.assertIn('COMPACT_STATE_ONLY', prompt)
        self.assertNotIn('ORIGINAL_A', prompt)
        self.assertNotIn('ORIGINAL_B', prompt)
        self.assertNotIn('FUTURE_SECRET_C', prompt)

    def test_retry_failed_turn_with_replacement(self):
        agent = self.spawn(message='orphan')
        self.assertEqual(self.complete(agent['agent_id'])['status'], 'failed')
        failed = self.history()['turns'][0]['turn_id']
        branch = self.fork('retry_agent', turn_id=failed, message='RECOVERED_ATTEMPT')
        self.assertEqual(branch['replayed_turns'], 0)
        self.assertIn('RECOVERED_ATTEMPT', self.result(branch['agent_id'])['received_prompt'])

    def test_validation_scope_and_summary_requirement(self):
        agent, first, second, third = self.three_turns()
        for operation, args in [
            ('fork_agent',{'target':'investigate','task_name':'bad','message':'x','last_turn_id':'../escape'}),
            ('fork_agent',{'target':'investigate','task_name':'bad','message':'x','context_mode':'summary'}),
            ('fork_agent',{'target':'investigate','task_name':'bad','message':'x','context_summary':'unselected'}),
            ('rewind_agent',{'target':'investigate','before_turn_id':'0'*32}),
        ]:
            self.assertIn('error',self.call(operation,args,check=False))
        other = dict(self.environment,AGY_SUBAGENT_SESSION='unrelated')
        self.assertIn('error',self.call('list_turns',{'target':agent['agent_id']},environment=other,check=False))

    def test_concurrent_branches_and_rewind_keep_independent_snapshots(self):
        agent, first, second, third = self.three_turns()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            jobs = [pool.submit(self.call,'fork_agent',{'target':'investigate','task_name':f'branch_{i}',
                    'message':f'VARIANT_{i}','last_turn_id':second}) for i in range(3)]
            branches = [job.result() for job in jobs]
        self.call('rewind_agent',{'target':'investigate','before_turn_id':first})
        for branch in branches:
            self.ids.append(branch['agent_id'])
            self.complete(branch['agent_id'])
            self.assertIn('ORIGINAL_B',self.result(branch['agent_id'])['received_prompt'])
            self.assertNotIn('FUTURE_SECRET_C',self.result(branch['agent_id'])['received_prompt'])
        self.assertEqual(self.history()['turns'],[])

    def test_interrupted_legacy_migration_retries_without_reordering_history(self):
        import os
        import sys
        from unittest import mock
        sys.path.insert(0, str(fixtures.SCRIPT.parent))
        import history_ops
        agent, first, second, third = self.three_turns()
        directory = self.root / agent['agent_id']
        state = json.loads((directory/'state.json').read_text())
        for key in ['history_version','history_seed','history_head','history_revision']:
            state.pop(key,None)
        (directory/'state.json').write_text(json.dumps(state))
        for index, run_id in enumerate([first, second, third]):
            path = directory/run_id/'request.json'
            request = json.loads(path.read_text())
            for key in ['previous_run_id','history_seed','message','started_at']:
                request.pop(key,None)
            path.write_text(json.dumps(request))
            original_time = time.time() - 30 + index
            os.utime(path,(original_time,original_time))
        real_write = history_ops.runner.write_json
        failed = False
        def interrupted_write(path, value):
            nonlocal failed
            real_write(path,value)
            if path.name == 'request.json' and not failed:
                failed = True
                raise OSError('simulated interrupted migration')
        with mock.patch.object(history_ops.runner,'write_json',side_effect=interrupted_write):
            with history_ops.runner.locked(directory):
                with self.assertRaises(OSError):
                    history_ops.ensure_history(directory,state)
        migrated = self.history()
        self.assertEqual([item['turn_id'] for item in migrated['turns']], [first, second, third])
        branch = self.fork(last_turn_id=first,message='AFTER_MIGRATION_RETRY')
        self.assertNotIn('FUTURE_SECRET_C', self.result(branch['agent_id'])['received_prompt'])
        self.assertNotIn('ORIGINAL_B', self.result(branch['agent_id'])['received_prompt'])

    def test_nested_fork_inherits_source_defaults_instead_of_caller_configuration(self):
        source = self.spawn()
        self.complete(source['agent_id'])
        caller = self.spawn('caller',model='caller-model',
                            agy={'bin':str(self.binary),'mode':'auto','timeout':100})
        self.complete(caller['agent_id'])
        environment = dict(self.environment,AGY_SUBAGENT_PARENT_PATH='/root/caller')
        branch = self.call('fork_agent',{'target':'/root/investigate','task_name':'source_defaults',
                          'message':'FORK_SOURCE_CONFIGURATION'},environment=environment)
        self.ids.append(branch['agent_id'])
        self.complete(branch['agent_id'])
        output = self.result(branch['agent_id'])
        self.assertNotIn('--model',output['args'])
        self.assertNotIn('--mode',output['args'])
        state = json.loads((self.root/branch['agent_id']/'state.json').read_text())
        self.assertIsNone(state['timeout'])

    def test_old_supervisor_turns_are_linked_after_partial_repair(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(fixtures.SCRIPT.parent))
        import history_ops
        agent, first, second, third = self.three_turns()
        directory = self.root/agent['agent_id']
        state = json.loads((directory/'state.json').read_text())
        state['history_head'] = first
        (directory/'state.json').write_text(json.dumps(state))
        for run_id in [second,third]:
            path = directory/run_id/'request.json'
            request = json.loads(path.read_text())
            for key in ['previous_run_id','history_seed','message']:
                request.pop(key,None)
            path.write_text(json.dumps(request))
        real_write = history_ops.runner.write_json
        failed = False
        def interrupted_write(path, value):
            nonlocal failed
            real_write(path,value)
            if path.name == 'request.json' and not failed:
                failed = True
                raise OSError('simulated interrupted hot upgrade repair')
        with mock.patch.object(history_ops.runner,'write_json',side_effect=interrupted_write):
            with history_ops.runner.locked(directory):
                with self.assertRaises(OSError):
                    history_ops.ensure_history(directory,state)
        turns = self.history()['turns']
        self.assertEqual([item['turn_id'] for item in turns],[first,second,third])
        branch = self.fork(last_turn_id=second,message='AFTER_HOT_UPGRADE_REPAIR')
        prompt = self.result(branch['agent_id'])['received_prompt']
        self.assertIn('ORIGINAL_B',prompt)
        self.assertNotIn('FUTURE_SECRET_C',prompt)

    def test_failed_migration_keeps_already_accepted_pending_messages(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(fixtures.SCRIPT.parent))
        import collaboration_api as api
        agent = self.spawn()
        self.complete(agent['agent_id'])
        directory = self.root/agent['agent_id']
        path = directory/'state.json'
        state = json.loads(path.read_text())
        for key in ['history_version','history_seed','history_head','history_revision']:
            state.pop(key,None)
        state['pending_messages'] = [{'message_id':'accepted','sender':'/root','message':'ACCEPTED_KEEP'}]
        path.write_text(json.dumps(state))
        real_write = api.runner.write_json
        failed = False
        def interrupted_write(destination, value):
            nonlocal failed
            real_write(destination,value)
            if destination == path and not failed:
                failed = True
                raise OSError('migration committed but caller interrupted')
        with mock.patch.object(api.runner,'write_json',side_effect=interrupted_write):
            with api.runner.locked(directory):
                with self.assertRaises(OSError):
                    api.launch_pending(directory,state)
        self.assertEqual(json.loads(path.read_text())['pending_messages'][0]['message'],'ACCEPTED_KEEP')
        self.advance(agent['agent_id'],'RECOVER_NEW')
        prompt = self.result(agent['agent_id'])['received_prompt']
        self.assertIn('ACCEPTED_KEEP',prompt)
        self.assertIn('RECOVER_NEW',prompt)

    def test_legacy_record_migration_remains_forkable(self):
        agent, first, second, third = self.three_turns()
        directory = self.root / agent['agent_id']
        state = json.loads((directory/'state.json').read_text())
        for key in ['history_version','history_seed','history_head','history_revision']:
            state.pop(key,None)
        (directory/'state.json').write_text(json.dumps(state))
        for path in directory.glob('*/request.json'):
            request = json.loads(path.read_text())
            for key in ['previous_run_id','history_seed','message']:
                request.pop(key,None)
            path.write_text(json.dumps(request))
        migrated = self.history(include_content=True)
        self.assertEqual(len(migrated['turns']),3)
        self.assertIn('ORIGINAL_A',migrated['turns'][0]['message'])
        branch = self.fork(last_turn_id=first,message='MIGRATED_BRANCH')
        self.assertNotIn('FUTURE_SECRET_C',self.result(branch['agent_id'])['received_prompt'])


if __name__ == '__main__':
    unittest.main()
