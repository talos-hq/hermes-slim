"""Install-local connector grants; only SHA-256 digests enter the profile state DB."""

import hashlib
import json
import secrets
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

SCOPES = frozenset({'mailbox:write', 'subscribe:write'})
PREFIX = 'talos_ct_'
_lock = threading.RLock()


@contextmanager
def _store():
    from hermes_state import SessionDB
    with SessionDB() as db:
        db._write_sql("""CREATE TABLE IF NOT EXISTS push_connector_tokens (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL,
            digest TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)""")
        yield db


def _metadata(row):
    result = dict(row)
    result.pop('digest', None)
    result['scopes'] = json.loads(result['scopes'])
    return result


def create(name, scopes):
    scopes = sorted(set(scopes))
    if not name.strip() or len(name) > 256 or not scopes or not set(scopes) <= SCOPES:
        raise ValueError('A label and valid connector scopes are required')
    token = PREFIX + secrets.token_urlsafe(32)
    record = {'id': secrets.token_hex(16), 'name': name, 'scopes': scopes,
              'created_at': datetime.now(UTC).isoformat(), 'revoked_at': None}
    with _lock, _store() as db:
        db._write_sql('INSERT INTO push_connector_tokens VALUES (?, ?, ?, ?, ?, NULL)',
                      (record['id'], name, json.dumps(scopes), hashlib.sha256(token.encode()).hexdigest(),
                       record['created_at']))
    return record, token


def list_tokens():
    with _lock, _store() as db:
        return [_metadata(row) for row in db._read_all(
            'SELECT id, name, scopes, created_at, revoked_at FROM push_connector_tokens ORDER BY created_at')]


def authenticate(token):
    if not token.startswith(PREFIX) or len(token) != len(PREFIX) + 43:
        return None
    with _lock, _store() as db:
        row = db._read_one('SELECT * FROM push_connector_tokens WHERE digest=? AND revoked_at IS NULL',
                           (hashlib.sha256(token.encode()).hexdigest(),))
        return _metadata(row) if row else None


def revoke(token_id):
    with _lock, _store() as db:
        return db._write_sql('UPDATE push_connector_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                             (datetime.now(UTC).isoformat(), token_id))


def revoke_all():
    with _lock, _store() as db:
        count = db._read_one('SELECT COUNT(*) AS n FROM push_connector_tokens WHERE revoked_at IS NULL')['n']
        db._write_sql('UPDATE push_connector_tokens SET revoked_at=? WHERE revoked_at IS NULL',
                      (datetime.now(UTC).isoformat(),))
        return count


def run(args):
    """Print a secret once on explicit creation; list and revoke expose metadata only."""
    action = args.connector_action
    if action == 'create':
        record, token = create(args.name, args.scope or ['mailbox:write'])
        print(json.dumps(record | {'token': token}))
    elif action == 'list':
        print(json.dumps(list_tokens()))
    elif action == 'revoke':
        if not args.token_id:
            raise ValueError('--token-id is required for revocation')
        revoke(args.token_id)
    else:
        raise ValueError('Choose connector-token create, list or revoke')
    return 0
