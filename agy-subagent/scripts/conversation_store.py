"""Read the current agy SQLite schema without modifying account or conversation data."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

UUID = re.compile(r'^[0-9a-fA-F-]{36}$')


@contextmanager
def connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.execute('PRAGMA query_only=ON')
    try:
        yield connection
    finally:
        connection.close()


def varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError('Incomplete protobuf varint')
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError('Invalid protobuf varint')


def bytes_field(data, number):
    position = 0
    while position < len(data):
        tag, position = varint(data, position)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            _, position = varint(data, position)
        elif wire in (1, 5):
            position += 8 if wire == 1 else 4
        elif wire == 2:
            size, position = varint(data, position)
            end = position + size
            if end > len(data):
                raise ValueError('Incomplete protobuf field')
            if field == number:
                return data[position:end]
            position = end
        else:
            raise ValueError('Unsupported protobuf wire type')
    return None


def response_text(payload, step_format):
    if step_format != 0:
        raise ValueError('Unsupported agy step_format. Update the read-only database adapter.')
    # Observed Step.agent_response (20), AgentResponse.response (1).
    response = bytes_field(payload, 20)
    text = bytes_field(response, 1) if response is not None else None
    if text is None:
        raise ValueError('Unsupported agy agent-response payload. Inspect the terminal.')
    return text.decode('utf-8')


def candidates(root, cwd, conversation_id):
    if conversation_id:
        if not UUID.fullmatch(conversation_id):
            raise ValueError('Invalid conversation ID')
        return [conversation_id]
    database = root / 'conversation_summaries.db'
    if not database.exists():
        return []
    uri = Path(cwd).resolve().as_uri()
    with connect(database) as connection:
        rows = connection.execute(
            'SELECT conversation_id, workspace_uris FROM conversation_summaries WHERE instr(workspace_uris,?)>0',
            (uri,)).fetchall()
    return [cid for cid, workspaces in rows if UUID.fullmatch(cid) and uri in json.loads(workspaces)]


def inspect_turn(root, cwd, marker, conversation_id=None):
    """Correlate an exact submitted marker, then read only its latest completed response."""
    root = Path(root)
    matches = []
    try:
        for cid in candidates(root, cwd, conversation_id):
            path = root / 'conversations' / (cid + '.db')
            if not path.exists():
                continue
            with connect(path) as connection:
                user = connection.execute(
                    'SELECT max(idx) FROM steps WHERE step_type=14 AND instr(step_payload,?)>0',
                    (marker.encode('utf-8'),)).fetchone()[0]
                if user is None:
                    continue
                latest_user = connection.execute('SELECT max(idx) FROM steps WHERE step_type=14').fetchone()[0]
                if latest_user != user:
                    raise ValueError('Another user turn appeared in this conversation. Inspect the terminal.')
                pending = connection.execute('SELECT count(*) FROM steps WHERE idx>? AND status!=3', (user,)).fetchone()[0]
                answer = connection.execute(
                    'SELECT idx,status,step_format,step_payload FROM steps WHERE idx>? AND step_type=15 ORDER BY idx DESC LIMIT 1',
                    (user,)).fetchone()
                turns = connection.execute('SELECT count(*) FROM steps WHERE step_type=14').fetchone()[0]
            with connect(root / 'conversation_summaries.db') as connection:
                summary = connection.execute(
                    'SELECT status,not_fully_idle FROM conversation_summaries WHERE conversation_id=?', (cid,)).fetchone()
            item = {'conversation_id': cid, 'user_step_index': user, 'completed': False}
            if answer and answer[1] == 3 and not pending and summary and summary[0] == 'CASCADE_RUN_STATUS_IDLE' and not summary[1]:
                text = response_text(answer[3], answer[2])
                if text.strip():
                    item.update(completed=True, response=text, response_step_index=answer[0], num_turns=turns)
            matches.append(item)
    except sqlite3.OperationalError as error:
        if 'locked' in str(error).lower() or 'busy' in str(error).lower():
            return None
        raise ValueError('Unsupported or unavailable agy database schema: ' + str(error)) from error
    if len(matches) > 1:
        raise ValueError('Ambiguous conversation marker. Refusing to select an unrelated answer.')
    return matches[0] if matches else None
