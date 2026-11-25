"""Regression tests for #72680 (retargeted).

The earlier attempt (#73171) snapshotted GatewayRunner._pending_messages, which
on current main has no writers — the live container is the per-agent
``agent._session_messages`` flushed via ``_flush_messages_to_session_db``.
When that flush raises (FTS/SQLite corruption) the in-memory transcript must
be dumped to a recovery snapshot instead of lost.

These tests exercise the real preservation path:
``_finalize_shutdown_agents`` -> flush raises -> ``_preserve_agent_history_on_shutdown``
-> ``flush_agent_history_to_file``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.shutdown_flush import (
    flush_agent_history_to_file,
)


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def test_preserves_agent_history_when_flush_raises(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    history = [{"role": "user", "content": "lost msg"}]
    flush_agent_history_to_file("sess:abc123", history)

    files = list(flush_dir.glob("*.json"))
    assert files, "expected recovery snapshot"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["issue"] == "#72680"
    assert data["session_id"] == "sess:abc123"
    assert data["count"] == 1
    assert data["messages"][0]["content"] == "lost msg"


def test_no_recovery_file_on_empty_history(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    flush_agent_history_to_file("sess:abc123", [])
    assert not list(flush_dir.glob("*.json"))


def test_protected_snapshot_projects_history_when_sqlite_flush_fails(tmp_path, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    from types import SimpleNamespace
    from agent.protected_request import session_request_keybox
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB

    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    address = "in-flight@example.org"

    def fail(_history):
        raise OSError("fixture database unavailable")

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_protected_session("protected", "test")
        agent = SimpleNamespace(session_id="protected", _session_db=db,
                                _session_messages=[{"role": "user", "content": address}],
                                _flush_messages_to_session_db=fail)
        GatewayRunner._flush_agent_transcript_at_shutdown(agent)
        (snapshot,) = flush_dir.glob("*.json")
        raw = snapshot.read_text(encoding="utf-8")
        assert address not in raw
        text = json.loads(raw)["messages"][0]["content"]
        box = session_request_keybox(db, "protected")
        assert box.restore_strict(box.replay_session, text, 1024) == address
        monkeypatch.delenv("HERMES_CONVO_SAFE_MASTER_KEY")
        GatewayRunner._flush_agent_transcript_at_shutdown(agent)
        assert list(flush_dir.glob("*.json")) == [snapshot]


