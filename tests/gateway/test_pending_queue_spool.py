"""Regression tests for runtime spool-on-drop of the pending transcript queue.

When the per-session pending cap (``_MAX_PENDING_PER_SESSION``) forces the
gateway to evict the oldest queued transcript message during live operation,
the message must be spooled to the on-disk pending spool (the same machinery
``flush_pending_to_file`` uses at shutdown) and replayed on the next
successful transcript flush — not silently discarded (#78182, #82616).
"""
import json
import logging
import asyncio
import threading
import time

import pytest

from gateway import shutdown_flush
from gateway.session import SessionStore


def _make_store(db):
    store = object.__new__(SessionStore)
    store._db = db
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    store._fts_rebuild_last_attempt_at = time.monotonic()
    # These tests exercise the cap-eviction spool path; keep the stalled-session spool (which
    # normally fires first, at the escalation threshold) out of the way.
    store._TRANSCRIPT_APPEND_FAILURE_ESCALATION_THRESHOLD = 10 ** 6
    return store


class BrokenThenHealedDb:
    """append_message fails while ``broken`` is True, then records rows."""

    def __init__(self):
        self.broken = True
        self.rows = []

    def append_message(self, **kwargs):
        if self.broken:
            raise RuntimeError("db unavailable")
        self.rows.append(kwargs)


@pytest.fixture()
def spool_home(tmp_path, monkeypatch):
    """Point the pending spool at an isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: tmp_path, raising=True
    )
    return tmp_path


def _spool_files(home):
    d = home / "pending_messages"
    return sorted(d.glob("pending-*.json")) if d.exists() else []


class TestSpoolOnDrop:
    def test_drop_spool_drain_roundtrip(self, spool_home, caplog, monkeypatch):
        # Small cap so the test stays fast.
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 5)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        n_extra = 3
        with caplog.at_level(logging.WARNING, logger="gateway.session"):
            for i in range(SessionStore._MAX_PENDING_PER_SESSION + n_extra):
                store.append_to_transcript(
                    "sess-1", {"role": "user", "content": f"msg{i}"}
                )

        # The oldest n_extra messages were evicted — and spooled, not lost.
        files = _spool_files(spool_home)
        assert len(files) == n_extra
        payloads = [json.loads(p.read_text()) for p in files]
        assert all(
            p["reason"] == shutdown_flush.TRANSCRIPT_CAP_DROP_REASON
            for p in payloads
        )
        spooled_contents = sorted(
            p["data"]["message"]["content"] for p in payloads
        )
        assert spooled_contents == ["msg0", "msg1", "msg2"]

        # Drop log escalated to WARNING and includes the spool path.
        drop_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "spooled oldest message" in r.getMessage()
        ]
        assert len(drop_warnings) == n_extra
        assert str(spool_home / "pending_messages") in drop_warnings[0].getMessage()

        # DB heals; the next successful flush drains the backlog AND
        # replays the spooled messages in drop order.
        db.broken = False
        store.append_to_transcript(
            "sess-1", {"role": "assistant", "content": "recovered"}
        )

        contents = [r["content"] for r in db.rows]
        # All surviving in-memory messages plus the recovery trigger...
        for i in range(n_extra, SessionStore._MAX_PENDING_PER_SESSION + n_extra):
            assert f"msg{i}" in contents
        assert "recovered" in contents
        # ...and the previously dropped messages, replayed in drop order.
        replayed = [c for c in contents if c in ("msg0", "msg1", "msg2")]
        assert replayed == ["msg0", "msg1", "msg2"]
        # Spool files consumed after successful replay.
        assert _spool_files(spool_home) == []
        # Nothing pending in memory.
        assert "sess-1" not in store._dirty_transcripts

    def test_drain_only_touches_own_session(self, spool_home, monkeypatch):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 3)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        for i in range(SessionStore._MAX_PENDING_PER_SESSION + 1):
            store.append_to_transcript("sess-a", {"role": "user", "content": f"a{i}"})
            store.append_to_transcript("sess-b", {"role": "user", "content": f"b{i}"})

        assert len(_spool_files(spool_home)) == 2  # one drop per session

        db.broken = False
        store.append_to_transcript("sess-a", {"role": "user", "content": "go-a"})

        # Only sess-a's spooled drop was replayed; sess-b's remains on disk.
        remaining = [
            json.loads(p.read_text()) for p in _spool_files(spool_home)
        ]
        assert len(remaining) == 1
        assert remaining[0]["session_key"] == "sess-b"
        a_rows = [r["content"] for r in db.rows if r["session_id"] == "sess-a"]
        assert "a0" in a_rows

    def test_spool_failure_degrades_to_plain_drop(
        self, spool_home, caplog, monkeypatch
    ):
        """If the spool cannot be written, behave exactly like the old
        drop-oldest path: cap enforced, WARNING logged, no crash."""
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 4)

        def _boom():
            raise OSError("disk full")

        monkeypatch.setattr(shutdown_flush, "_get_flush_dir", _boom)

        db = BrokenThenHealedDb()
        store = _make_store(db)

        with caplog.at_level(logging.WARNING, logger="gateway.session"):
            for i in range(SessionStore._MAX_PENDING_PER_SESSION + 5):
                store.append_to_transcript(
                    "sess-x", {"role": "user", "content": f"msg{i}"}
                )

        pending = store._dirty_transcripts.get("sess-x", [])
        assert len(pending) <= SessionStore._MAX_PENDING_PER_SESSION
        assert _spool_files(spool_home) == []
        degraded = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "on-disk spool unavailable" in r.getMessage()
        ]
        assert len(degraded) == 5
        # No spool bookkeeping means recovery must not attempt a drain.
        db.broken = False
        store.append_to_transcript("sess-x", {"role": "user", "content": "fin"})
        assert [r["content"] for r in db.rows][-1] == "fin"

    def test_replay_failure_keeps_spool_files(self, spool_home, monkeypatch):
        """A failed replay must preserve the spool files for a later retry."""
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 3)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        for i in range(SessionStore._MAX_PENDING_PER_SESSION + 2):
            store.append_to_transcript("sess-r", {"role": "user", "content": f"m{i}"})
        assert len(_spool_files(spool_home)) == 2

        # DB heals only for live writes; replayed (spooled) rows still fail.
        class FlakyDb(BrokenThenHealedDb):
            def append_message(self, **kwargs):
                if kwargs["content"] in ("m0", "m1", "m2"):
                    raise RuntimeError("still broken for replays")
                self.rows.append(kwargs)

        flaky = FlakyDb()
        flaky.broken = False
        store._db = flaky
        # This append pushes pending over the cap again (dropping/spooling
        # m2) before the successful flush triggers the drain.
        store.append_to_transcript("sess-r", {"role": "user", "content": "go"})

        # Spool files survive the failed replay for the next attempt.
        assert len(_spool_files(spool_home)) == 3
        assert "sess-r" in getattr(store, "_spooled_drop_sessions", set())


class TestSpoolPrimitives:
    def test_drain_skips_other_reasons(self, spool_home):
        # A shutdown-format flush file must not be consumed by the drain.
        shutdown_flush.flush_pending_to_file({"key1": "hello"}, reason="shutdown")
        assert len(_spool_files(spool_home)) == 1
        replayed, _remaining = shutdown_flush.drain_transcript_spool(
            "key1", lambda m: None
        )
        assert replayed == 0
        assert len(_spool_files(spool_home)) == 1

    def test_roundtrip_order(self, spool_home):
        for i in range(3):
            shutdown_flush.spool_dropped_transcript_message(
                "s", {"role": "user", "content": f"c{i}"}
            )
        seen = []
        replayed, remaining = shutdown_flush.drain_transcript_spool(
            "s", lambda m: seen.append(m["content"])
        )
        assert replayed == 3
        assert remaining == 0
        assert seen == ["c0", "c1", "c2"]
        assert _spool_files(spool_home) == []


def test_failed_diversion_keeps_only_unwritten_records(monkeypatch):
    from hermes_state_errors import StateDbReplacedError

    store = _make_store(None)
    saved = {"role": "user", "content": "already spooled"}
    unsaved = {"role": "assistant", "content": "needs a retry"}
    concurrent = {"role": "user", "content": "arrived during diversion"}
    store._dirty_transcripts["queue"] = [saved, unsaved]

    def spool(_session_id, message):
        return "spooled.json" if message is saved else None

    def divert(_session_id, messages):
        assert messages == [saved, unsaved]
        store._dirty_transcripts["queue"] = [concurrent]
        raise OSError("fixture write failure")

    monkeypatch.setattr("gateway.session_transcript._spool_dropped", spool)
    monkeypatch.setattr("hermes_state.divert_session_transcript_jsonl", divert)
    store._divert_transcript_after_db_replaced("saved", "queue", StateDbReplacedError("fixture"))
    assert store._dirty_transcripts["queue"] == [unsaved, concurrent]


def test_shutdown_pending_and_overflow_bind_redacted_text_to_original_session(spool_home, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    from agent.protected_request import session_request_keybox
    from gateway.shutdown_flush import flush_overflow_to_file, flush_pending_to_file, recover_pending_spool
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    addresses = ("pending-fixture@example.org", "overflow-fixture@example.org")
    with SessionDB(db_path=spool_home / "state.db") as db:
        db.create_protected_session("original", "test")
        db.create_session("other", "test")
        route = "agent:main:telegram:fixture"

        def lookup(_route, **_kwargs):
            return "original", db

        assert flush_pending_to_file({route: addresses[0]}, session_resolver=lookup) == 1
        assert flush_overflow_to_file({route: [addresses[1]]}, session_resolver=lookup) == 1
        for path in _spool_files(spool_home):
            contents = path.read_bytes()
            assert b"[[CONVO_SAFE:" in contents
            assert all(address.encode() not in contents for address in addresses)
        assert recover_pending_spool(db, session_resolver=lambda *_a, **_kw: ("other", db))[0] == 0
        assert len(_spool_files(spool_home)) == 2
        assert recover_pending_spool(db, session_resolver=lookup)[0] == 2
        rows = db.get_messages_as_conversation("original")
        box = session_request_keybox(db, "original")
        assert tuple(box.restore_strict(box.replay_session, row["content"], 1024) for row in rows) == addresses


def test_shutdown_drain_does_not_acknowledge_volatile_protected_input(spool_home, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    from types import SimpleNamespace
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    replies, queue = [], []

    async def reply(_event, _adapter, message):
        replies.append(message)

    with SessionDB(db_path=spool_home / "state.db") as db:
        db.create_protected_session("protected", "test")
        db.create_session("plain", "test")
        runner = SimpleNamespace(
            session_store=SimpleNamespace(resolve_session_id_for_key=lambda key: (key, db)),
            _delivery_adapter_for=lambda _source: object(),
            _queue_during_drain_enabled=lambda _mode: True,
            _queue_or_replace_pending_event=lambda key, _event: queue.append(key),
            _status_action_gerund=lambda: "stopping",
            _send_busy_reply=reply,
            _command_turn_admitted=lambda *_a: False,
            _enqueue_fifo=lambda key, *_a: queue.append(key),
        )
        for key in ("protected", "plain"):
            asyncio.run(GatewayBusySessionMixin._send_busy_drain_notice(
                runner, SimpleNamespace(source=None), key, "queue",
            ))
        for key, expected in (("protected", True), ("plain", False)):
            assert asyncio.run(GatewayBusySessionMixin._queue_command_turn(
                runner, key, object(), object(), object(), ack="queued",
            )) is expected
        protected_followup = SimpleNamespace(internal=False, get_command=lambda: None)
        response = asyncio.run(GatewayInboundMixin._hm_handle_running_session_message(
            runner, protected_followup, None, "protected",
        ))
        assert "unavailable" in response.lower()
    assert queue == ["plain", "plain"]
    assert "queued" not in replies[0].lower()
    assert "queued" in replies[1].lower()
    assert "unavailable" in replies[2].lower()


def test_protected_internal_wake_requires_durable_admission(monkeypatch):
    from types import SimpleNamespace
    from gateway.platforms.event import MessageType
    from gateway.run_busy import GatewayBusySessionMixin

    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    admitted = []

    async def admit(_runner, _adapter, _source, text, _producer, metadata):
        admitted.append((text, metadata))
        return text == "accepted"

    monkeypatch.setattr("gateway.session_automation.admit_route_wake", admit)
    runner = SimpleNamespace(_delivery_adapter_for=lambda _source: object())
    for text, trusted, accepted in (("accepted", True, True), ("refused", True, False), ("untrusted", False, False)):
        event = SimpleNamespace(
            source=None, text=text, message_type=MessageType.TEXT, metadata={"fixture": True},
            allow_gateway_control=trusted, _gateway_accepted=False,
        )
        assert asyncio.run(GatewayBusySessionMixin._handle_protected_busy_wake(runner, event, "route"))
        assert event._gateway_accepted is accepted
    assert admitted == [("accepted", {"fixture": True}), ("refused", {"fixture": True})]


def test_shutdown_uses_resolved_mux_profile_key(spool_home, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from gateway.run import _profile_runtime_scope
    from gateway.shutdown_sealed_spool import seal_pending
    from gateway.shutdown_flush import flush_overflow_to_file, flush_pending_to_file, recover_pending_spool
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "11" * 32)
    named = spool_home / "profiles" / "named"
    named.mkdir(parents=True)
    (named / ".env").write_text(f"HERMES_CONVO_SAFE_MASTER_KEY={'22' * 32}\n")
    address = "named-shutdown@example.org"
    route = "agent:named:telegram:fixture"
    token = set_multiplex_context(True)
    try:
        with SessionDB(db_path=named / "state.db") as db:
            with _profile_runtime_scope(named):
                db.create_protected_session("original", "test")
            def resolver(*_a, **_kw):
                return "original", db
            assert flush_pending_to_file({route: address}, session_resolver=resolver) == 1
            (spool,) = _spool_files(spool_home)
            assert address.encode() not in spool.read_bytes()
            held = "unresolved-shutdown@example.org"
            later = "later-shutdown@example.org"
            assert flush_overflow_to_file({route: [held, later]}, session_resolver=lambda *_a, **_kw: None,
                                         profile_resolver=lambda _key: named) == 2
            sealed = [item for item in _spool_files(spool_home) if item != spool]
            assert len(sealed) == 2
            assert all(b"sealed_v1" in item.read_bytes() and held.encode() not in item.read_bytes() for item in sealed)
            assert recover_pending_spool(db, session_resolver=lambda *_a, **_kw: None)[0] == 0
            assert len(_spool_files(spool_home)) == 3
            other = spool_home / "profiles" / "other"
            other.mkdir()
            (other / ".env").write_text(f"HERMES_CONVO_SAFE_MASTER_KEY={'22' * 32}\n")
            resolved_spool = spool.with_suffix(".held")
            spool.rename(resolved_spool)
            with SessionDB(db_path=other / "state.db") as other_db:
                with _profile_runtime_scope(other):
                    other_db.create_protected_session("original", "test")
                    seal_pending({"text": "different profile"}, {"session_key": route})
                assert recover_pending_spool(db, session_resolver=lambda *_a, **_kw: ("original", other_db))[0] == 0
            resolved_spool.rename(spool)
            assert len(_spool_files(spool_home)) == 3
            first = next(item for item in sealed if json.loads(item.read_bytes())["seq"] == 0)
            original = first.read_bytes()
            changed = json.loads(original)
            changed["data"]["sealed_v1"] = "AAAA"
            first.write_text(json.dumps(changed))
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 0
            assert len(_spool_files(spool_home)) == 3
            changed = json.loads(original)
            changed["seq"] = 999
            first.write_text(json.dumps(changed))
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 0
            changed = json.loads(original)
            changed["session_key"] = "untrusted-route"
            first.write_text(json.dumps(changed))
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 0
            first.write_text("invalid JSON")
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 0
            first.write_bytes(original)
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 3
            first.write_bytes(original)  # crash after commit and before the spool unlink
            duplicate = first.with_name("pending-sealed-duplicate.json")
            duplicate.write_bytes(original)
            assert recover_pending_spool(db, session_resolver=resolver)[0] == 2
            assert not _spool_files(spool_home)
            with _profile_runtime_scope(named):
                from agent.protected_request import session_request_keybox

                box = session_request_keybox(db, "original")
                rows = db.get_messages_as_conversation("original")
                assert tuple(box.restore_strict(box.replay_session, row["content"], 1024) for row in rows) == (
                    address, held, later,
                )
    finally:
        reset_multiplex_context(token)
