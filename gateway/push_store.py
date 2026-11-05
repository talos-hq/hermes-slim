"""Profile-local Talos mailbox and device subscriptions in the gateway state DB."""

import json
from datetime import UTC, datetime


class PushStore:
    """Use SessionDB's transaction and generation guards for all durable mutations."""

    def __init__(self, db):
        self.db = db
        def initialize(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
                owner TEXT NOT NULL, device_id TEXT NOT NULL, subscription TEXT NOT NULL,
                PRIMARY KEY (owner, device_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS push_mailbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                id TEXT NOT NULL, timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
                message TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
                UNIQUE (owner, id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS push_idempotency (
                owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY (owner, key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS push_diagnostics (
                owner TEXT PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                attempted_at TEXT, result TEXT, revoked_at TEXT, reason TEXT)""")
        db._execute_write(initialize)

    def register(self, owner, device_id, subscription):
        self.db._write_sql("""INSERT INTO push_subscriptions VALUES (?, ?, ?)
            ON CONFLICT(owner, device_id) DO UPDATE SET subscription=excluded.subscription""",
            (owner, device_id, json.dumps(subscription, sort_keys=True)))

    def revoke(self, owner, device_id, subscription=None, *, reason=None):
        # A late 410 for an old endpoint must not revoke a replacement subscription.
        sql = "DELETE FROM push_subscriptions WHERE owner=? AND device_id=?"
        params = (owner, device_id)
        if subscription is not None:
            sql += " AND subscription=?"
            params += (json.dumps(subscription, sort_keys=True),)
        def remove(conn):
            removed = conn.execute(sql, params).rowcount
            if removed and reason is not None:
                now = datetime.now(UTC).isoformat()
                conn.execute("""INSERT INTO push_diagnostics (owner, revoked, revoked_at, reason)
                    VALUES (?, 1, ?, ?) ON CONFLICT(owner) DO UPDATE SET
                    revoked=revoked+1, revoked_at=excluded.revoked_at, reason=excluded.reason""",
                    (owner, now, reason))
        self.db._execute_write(remove)

    def subscriptions(self, owner):
        return [(row['device_id'], json.loads(row['subscription'])) for row in
                self.db._read_all("SELECT * FROM push_subscriptions WHERE owner=?", (owner,))]

    def admit(self, owner, message_id, session_id, message, *, key=None, body=None):
        """Commit the mailbox and claim this event's bounded push batch atomically.

        A crash after commit may lose a wake-up, but never loses mailbox history or
        replays an uncertain push. Push services cannot provide exactly-once delivery.
        """
        def insert(conn):
            if key is not None:
                claim = conn.execute("SELECT body FROM push_idempotency WHERE owner=? AND key=?",
                                     (owner, key)).fetchone()
                if claim is not None:
                    if claim['body'] != body:
                        raise ValueError('idempotency_conflict')
                    return False
                conn.execute("INSERT INTO push_idempotency VALUES (?, ?, ?)", (owner, key, body))
            row = conn.execute("SELECT session_id, message FROM push_mailbox WHERE owner=? AND id=?",
                               (owner, message_id)).fetchone()
            if row is not None:
                if (row['session_id'], row['message']) != (session_id, message):
                    raise ValueError("Message id already used for different content")
                return False
            conn.execute("""INSERT INTO push_mailbox
                (owner, id, timestamp, session_id, message) VALUES (?, ?, ?, ?, ?)""",
                (owner, message_id, datetime.now(UTC).isoformat(), session_id, message))
            return True
        return self.db._execute_write(insert)

    def mailbox(self, owner, after=0, limit=50):
        rows = self.db._read_all("""SELECT sequence, id, timestamp, session_id, message, read
            FROM push_mailbox WHERE owner=? AND sequence>? ORDER BY sequence LIMIT ?""", (owner, after, limit))
        return [dict(row) | {'read': bool(row['read'])} for row in rows]

    def mark_read(self, owner, message_id):
        return self.db._write_rowcount("UPDATE push_mailbox SET read=1 WHERE owner=? AND id=?",
                                       (owner, message_id)) > 0

    def message(self, owner, message_id):
        rows = self.db._read_all("""SELECT sequence, id, timestamp, session_id, message, read
            FROM push_mailbox WHERE owner=? AND id=?""", (owner, message_id))
        return dict(rows[0]) | {'read': bool(rows[0]['read'])} if rows else None

    def record_delivery(self, owner, attempted_at, result):
        """Count each network attempt, including retries; retain no provider content."""
        sent = int(result == 'sent')
        self.db._write_sql("""INSERT INTO push_diagnostics
            (owner, sent, failed, attempted_at, result) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner) DO UPDATE SET sent=sent+excluded.sent,
            failed=failed+excluded.failed,
            attempted_at=MAX(COALESCE(attempted_at, excluded.attempted_at), excluded.attempted_at),
            result=CASE WHEN attempted_at IS NULL OR excluded.attempted_at >= attempted_at
                THEN excluded.result ELSE result END""",
            (owner, sent, 1 - sent, attempted_at, result))

    def status(self, owner):
        rows = self.db._read_all("SELECT * FROM push_diagnostics WHERE owner=?", (owner,))
        row = dict(rows[0]) if rows else {}
        count = self.db._read_all(
            "SELECT COUNT(*) AS total FROM push_subscriptions WHERE owner=?", (owner,))[0]['total']
        return {'subscriptions': count,
                'last_delivery': {'attempted_at': row['attempted_at'], 'result': row['result']}
                    if row.get('attempted_at') else None,
                'last_revocation': {'at': row['revoked_at'], 'reason': row['reason']}
                    if row.get('revoked_at') else None,
                'counters': {key: row.get(key, 0) for key in ('sent', 'failed', 'revoked')}}
