"""Exercise persistent terminals against an interactive SQLite-writing fake agy."""
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
import json, os, re, signal, sqlite3, sys, time, tty, uuid
from pathlib import Path
assert all(os.isatty(fd) for fd in (0,1,2))
assert '--print' not in sys.argv and '--output-format' not in sys.argv
if os.environ.get('FAKE_IGNORE_TERM'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
root=Path(os.environ['FAKE_AGY_DATA'])
root.mkdir(exist_ok=True)
(root/'conversations').mkdir(exist_ok=True)
cid=sys.argv[sys.argv.index('--conversation')+1] if '--conversation' in sys.argv else str(uuid.uuid4())
summary=sqlite3.connect(root/'conversation_summaries.db')
summary.execute('CREATE TABLE IF NOT EXISTS conversation_summaries(conversation_id TEXT PRIMARY KEY,workspace_uris TEXT,status TEXT,not_fully_idle INTEGER)')
summary.execute('INSERT OR REPLACE INTO conversation_summaries VALUES(?,?,?,0)',(cid,json.dumps([Path.cwd().as_uri()]),'CASCADE_RUN_STATUS_IDLE'))
summary.commit()
db=sqlite3.connect(root/'conversations'/(cid+'.db'))
db.execute('CREATE TABLE IF NOT EXISTS steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,step_format INTEGER,step_payload BLOB)')
def vint(n):
    out=b''
    while n>127: out+=bytes([(n&127)|128]); n>>=7
    return out+bytes([n])
def field(n,b): return vint(n*8+2)+vint(len(b))+b
tty.setraw(0)
if os.environ.get('FAKE_SPLIT_QUERY'):
    for first,last,reply in ((b'\x1b[6',b'n',b'\x1b[1;1R'),
                             (b'\x1b]11;',b'?\x07',b'\x1b]11;rgb:0000/0000/0000\x07')):
        os.write(1,first)
        time.sleep(.15)
        os.write(1,last)
        received=b''
        while reply not in received: received+=os.read(0,100)
os.write(1,b'\x1b[?2004h? for shortcuts\r\n')
raw=b''
while True:
    chunk=os.read(0,65536)
    if not chunk: break
    raw+=chunk
    while b'\x1b[201~\r' in raw:
        content,raw=raw.split(b'\x1b[201~\r',1)
        content=content.split(b'\x1b[200~',1)[-1].decode()
        idx=db.execute('SELECT coalesce(max(idx),-1)+1 FROM steps').fetchone()[0]
        db.execute('INSERT INTO steps VALUES(?,14,3,0,?)',(idx,content.encode()))
        db.commit()
        if '<task>\nslow\n</task>' in content: continue
        answer=json.dumps({'pid':os.getpid(),'input':content,'cid':cid})
        payload=field(20,field(3,b'PRIVATE INTERNAL TEXT')+field(1,answer.encode()))
        db.execute('INSERT INTO steps VALUES(?,15,3,0,?)',(idx+1,payload))
        db.commit()
        os.write(1,b'completed\r\n? for shortcuts\r\n')
'''


class PersistentTerminal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'state'
        self.binary = self.base / 'agy'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.data = self.base / 'data'
        self.env = dict(os.environ, AGY_SUBAGENT_SESSION='pty-tests', FAKE_AGY_DATA=str(self.data))
        self.env.pop('AGY_SUBAGENT_RESTORE_PRINT', None)
        self.ids = []

    def call(self, operation, args=None):
        result = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.root), operation],
                                input=json.dumps(args or {}), capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def spawn(self, message='hello'):
        agent = self.call('spawn_agent', dict(task_name='test', message=message, fork_turns='none',
                          cwd=str(self.base), agy={'bin':str(self.binary), 'data_dir':str(self.data)}))
        self.ids.append(agent['agent_id'])
        return agent['agent_id']

    def done(self, agent):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            state=json.loads((self.root/agent/'state.json').read_text())
            if state['status'] != 'running':
                self.assertEqual(state['status'], 'completed', str(state))
                result=json.loads((self.root/agent/state['run_id']/'result.json').read_text())
                return json.loads(result['result']['response'])
            time.sleep(.1)
        self.fail('Terminal did not finish: '+str(self.call('terminal_status', {'target':agent})))

    def tearDown(self):
        for agent in self.ids:
            self.call('close_agent', {'target':agent})
        self.temp.cleanup()

    def test_persistent_process_queue_and_rewind(self):
        agent=self.spawn()
        first=self.done(agent)
        self.assertNotIn('PRIVATE INTERNAL TEXT', first['input'])
        self.call('followup_task', {'target':agent,'message':'second'})
        second=self.done(agent)
        self.assertEqual(first['pid'],second['pid'])
        self.assertEqual(first['cid'],second['cid'])
        turns=self.call('list_turns',{'target':agent})['turns']
        self.call('rewind_agent',{'target':agent,'before_turn_id':turns[-1]['turn_id']})
        self.call('followup_task',{'target':agent,'message':'alternative'})
        third=self.done(agent)
        self.assertNotEqual(first['cid'],third['cid'])
        self.assertNotEqual(first['pid'],third['pid'])
        self.assertNotIn('second',third['input'])
        self.call('close_agent',{'target':agent})
        self.assertFalse(self.call('terminal_status',{'target':agent})['running'])

    def test_interrupt_and_restart(self):
        agent=self.spawn('slow')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.call('terminal_status',{'target':agent}).get('conversation_id'): break
            time.sleep(.1)
        result=self.call('interrupt_agent',{'target':agent})
        self.assertEqual(result['status'],'interrupted')
        self.assertFalse(self.call('terminal_status',{'target':agent})['running'])
        self.call('followup_task',{'target':agent,'message':'continued'})
        self.done(agent)

    def test_queued_round_stays_in_same_terminal_and_crash_can_resume(self):
        agent=self.spawn()
        self.call('followup_task', {'target':agent,'message':'queued'})
        final=self.done(agent)
        state=json.loads((self.root/agent/'state.json').read_text())
        self.assertEqual(state['turn'],2)
        self.assertIn('queued',final['input'])
        updates=self.call('wait_agent',{'timeout_ms':0})['updates']
        self.assertEqual(len([u for u in updates if u.get('type')=='final_answer']),2)
        os.kill(final['pid'],9)
        self.call('followup_task',{'target':agent,'message':'after crash'})
        resumed=self.done(agent)
        self.assertEqual(final['cid'],resumed['cid'])
        self.assertNotEqual(final['pid'],resumed['pid'])

    def test_close_waits_for_stubborn_terminal_to_exit(self):
        self.env['FAKE_IGNORE_TERM']='1'
        agent=self.spawn()
        result=self.done(agent)
        self.call('close_agent',{'target':agent})
        with self.assertRaises(ProcessLookupError):
            os.kill(result['pid'],0)
        self.call('resume_agent',{'target':agent})
        self.call('followup_task',{'target':agent,'message':'immediate resume'})
        self.done(agent)

    def test_terminal_queries_split_across_reads(self):
        self.env['FAKE_SPLIT_QUERY']='1'
        self.done(self.spawn())

    def test_backend_restoration_is_explicit(self):
        self.assertEqual(self.call('configure_backend',{'backend':'print'}),{'backend':'print','print_enabled':True})
        self.assertEqual(self.call('configure_backend',{'backend':'pty'}),{'backend':'pty','print_enabled':False})
