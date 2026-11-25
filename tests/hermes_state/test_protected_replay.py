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


def test_protected_sqlite_writes_project_and_keep_tool_pairing(tmp_path, monkeypatch):
    class Projector:
        replay_session = 1

        def redact_model_text(self, scope, value, *, redact_email):
            assert scope == self.replay_session and redact_email
            return value.replace("fixture@example.org", "[email-marker]")

    monkeypatch.setattr(hermes_state_replay, "_open_sidecar", lambda *args, **kwargs: Projector())
    monkeypatch.setattr("hermes_state_replay_fallback.remember_fallback_binding", lambda *_: None)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("saved", "test")
        db.patch_session_model_config("saved", {"replay_binding": {"scope": 1}})
        row_id = db.append_message("saved", "user", content="fixture@example.org")
        assert db.set_user_message_content("saved", row_id, "Updated fixture@example.org") == 1
        db.append_messages_batch("saved", [
            {"role": "assistant", "tool_calls": [{
                "id": "call-id", "function": {"name": "mail", "arguments": '{"to": "fixture@example.org"}'},
            }]},
            {"role": "tool", "tool_call_id": "call-id", "content": "fixture@example.org"},
        ])
        rows = db.get_messages("saved")
        assert rows[0]["content"] == "Updated [email-marker]"
        assert rows[1]["tool_calls"][0]["id"] == rows[2]["tool_call_id"] == "call-id"
        assert "[email-marker]" in rows[1]["tool_calls"][0]["function"]["arguments"]
        assert rows[2]["content"] == "[email-marker]"


def test_unavailable_sidecar_prevents_protected_write(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _name: None)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("saved", "test")
        db.patch_session_model_config("saved", {"replay_binding": {"scope": 1}})
        with pytest.raises(ProtectedReplayError):
            db.append_message("saved", "user", content="private fixture")
        assert db.get_messages("saved") == []


@pytest.mark.parametrize("key_configured", [True, False])
def test_protected_transcript_fallbacks_do_not_write_plaintext(tmp_path, monkeypatch, key_configured):
    from gateway.shutdown_flush import spool_dropped_transcript_message
    from hermes_state import divert_session_transcript_jsonl

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _name: "fixture" if key_configured else None)
    if not key_configured:
        log = replay_log_path(tmp_path / "state.db", "saved")
        log.parent.mkdir()
        log.with_name(log.name + ".keybox.sqlite").touch()

    record = {"role": "user", "content": "private fixture"}
    with pytest.raises(ProtectedReplayError, match="requires a verified fallback"):
        divert_session_transcript_jsonl("saved", [record])
    assert spool_dropped_transcript_message("saved", record) is None
    assert not (tmp_path / "sessions").exists()
    assert not (tmp_path / "pending_messages").exists()


def test_signed_binding_keeps_fallbacks_redacted_without_sqlite(tmp_path, monkeypatch):
    import json

    from gateway.shutdown_flush import spool_dropped_transcript_message
    from hermes_state import divert_session_transcript_jsonl
    from hermes_state_replay_fallback import remember_fallback_binding

    class Projector:
        replay_session = 1

        def redact_model_text(self, _scope, value, *, redact_email):
            assert redact_email
            return value.replace("fixture@example.org", "[email-marker]")

    path = tmp_path / "state.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", path)
    monkeypatch.setattr("hermes_state_replay_fallback._master_key", lambda: bytes(32))
    binding = {"scope": 1, "lineage": "fixture-lineage", "sidecar": "fixture-sidecar"}

    def open_bound(db_path, session_id, expected, *, writable):
        assert (db_path, session_id, expected, writable) == (path, "saved", binding, True)
        return Projector()

    monkeypatch.setattr("hermes_state_replay_fallback._open_sidecar", open_bound)
    log = replay_log_path(path, "saved")
    log.parent.mkdir()
    remember_fallback_binding(path, "saved", binding)

    record = {"role": "user", "content": "Message fixture@example.org"}
    assert divert_session_transcript_jsonl("saved", [record]) == log
    assert json.loads(log.read_text())["content"] == "Message [email-marker]"
    spool = spool_dropped_transcript_message("saved", record)
    assert json.loads(spool.read_text())["data"]["message"]["content"] == "Message [email-marker]"
    assert not path.exists()
    assert record["content"] == "Message fixture@example.org"

    manifest = log.with_suffix(".binding.json")
    envelope = json.loads(manifest.read_text())
    envelope["binding"]["sidecar"] = "wrong"
    manifest.write_text(json.dumps(envelope))
    with pytest.raises(ProtectedReplayError, match="could not be verified"):
        divert_session_transcript_jsonl("saved", [record])
    assert len(log.read_text().splitlines()) == 1


def test_initialize_protected_session_commits_binding_and_projects_system_prompt(tmp_path, monkeypatch):
    from hermes_state_replay_init import create_protected_session

    binding = {"scope": 2, "lineage": "fixture-lineage", "sidecar": "fixture-sidecar"}

    class Projector:
        replay_session = 2

        def redact_model_text(self, _scope, value, *, redact_email):
            assert redact_email
            return value.replace("fixture@example.org", "[email-marker]")

    monkeypatch.setattr("hermes_state_replay_init._prepare_sidecar", lambda *_: (binding, Projector()))
    monkeypatch.setattr("hermes_state_replay_init._open_sidecar", lambda *_, **__: Projector())
    monkeypatch.setattr("hermes_state_replay_init.remember_fallback_binding", lambda *_: None)
    monkeypatch.setattr("hermes_state_replay._open_sidecar", lambda *_, **__: Projector())
    monkeypatch.setattr("hermes_state_replay_fallback.remember_fallback_binding", lambda *_: None)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        assert db.create_protected_session("new", "test", system_prompt="Contact fixture@example.org.") == "new"
        assert db.get_session_model_config_value("new", "replay_binding") == binding
        assert db.get_session("new")["system_prompt"] == "Contact [email-marker]."
        db.update_system_prompt("new", "Updated fixture@example.org.")
        assert db.get_session("new")["system_prompt"] == "Updated [email-marker]."
        assert create_protected_session(db, "new", "test") == "new"
        db.create_session("existing", "test")
        with pytest.raises(ProtectedReplayError, match="Existing session"):
            db.create_protected_session("existing", "test")
