"""Protected model history must reopen its exact sidecar before returning rows."""

from pathlib import Path

import pytest

import hermes_state_replay
from hermes_state import SessionDB
from hermes_state_replay import ProtectedReplayError, replay_log_path


def test_bound_history_validates_before_returning_without_rewriting(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    marker = "[[CONVO_SAFE:v1:{synthetic}]]"
    binding = {"scope": 1, "lineage": "fixture", "sidecar": "fixture"}
    calls = []

    def verify(path, session_id, saved_binding, messages):
        calls.append((path, session_id, saved_binding, messages))

    monkeypatch.setattr(hermes_state_replay, "verify_protected_replay", verify)
    try:
        db.create_session("saved", "test")
        db.append_message("saved", "user", content=marker)
        assert db.get_messages_as_conversation("saved")[0]["content"] == marker
        assert calls == []
        db.patch_session_model_config("saved", {"replay_binding": binding})
        result = db.get_messages_as_conversation("saved")
        assert calls == [(db.db_path, "saved", binding, result)]
        assert result[0]["content"] == marker
    finally:
        db.close()


def test_missing_key_stops_model_history_but_keeps_analytics(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _name: None)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("saved", "test")
        db.append_message("saved", "user", content="redacted fixture")
        db.patch_session_model_config("saved", {"replay_binding": {"scope": 1}})
        with pytest.raises(ProtectedReplayError, match="could not be verified"):
            db.get_messages_as_conversation("saved")
        with pytest.raises(ProtectedReplayError, match="could not be verified"):
            db.get_resume_conversations("saved")
        assert db.get_messages_as_conversation("saved", include_compacted=True)[0]["content"] == "redacted fixture"
    finally:
        db.close()


def test_sidecar_location_is_profile_local_even_for_path_like_session_ids():
    root = Path("/example/profile")
    log = replay_log_path(root / "state.db", "../../foreign/session")
    assert log.parent == root / "protected-transcripts"
    assert log.suffix == ".jsonl"


def test_unbound_child_cannot_replay_protected_ancestor(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("parent", "test")
        db.append_message("parent", "user", content="protected fixture")
        db.patch_session_model_config("parent", {"replay_binding": {"scope": 1}})
        db.publish_compression_child(
            parent_session_id="parent",
            child_session_id="child",
            source="test",
            messages=[{"role": "user", "content": "summary"}],
            require_compression_lease=False,
        )
        with pytest.raises(ProtectedReplayError, match="lineage has no replay binding"):
            db.get_messages_as_conversation("child", include_ancestors=True)
