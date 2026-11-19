"""Reversible backend selection. Interactive PTY is the default."""
import os

import agy_subagent as runner


def selection(root):
    path = root / 'backend.json'
    config = runner.read_json(path) if path.exists() else {'backend': 'pty', 'print_enabled': False}
    backend = config.get('backend', 'pty')
    # Explicit opt-in keeps legacy tests and a future user-requested restoration possible.
    if os.environ.get('AGY_SUBAGENT_RESTORE_PRINT') == '1':
        backend = 'print'
        config['print_enabled'] = True
    if backend not in ('pty', 'print'):
        raise ValueError('Unknown agy backend')
    if backend == 'print' and not config.get('print_enabled'):
        raise ValueError('Print backend is disabled. Restore it explicitly with configure_backend.')
    return backend


def configure(root, backend):
    runner.write_json(root / 'backend.json', {'backend': backend, 'print_enabled': backend == 'print'})
    return {'backend': backend, 'print_enabled': backend == 'print'}
