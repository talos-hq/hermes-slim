"""The runtime ledger keeps protected input and outcome text redacted."""

import json

import pytest

import hermes_state_runtime as runtime
from hermes_state import SessionDB


def test_protected_ledger_projects_text_and_keeps_retry_identity(tmp_path, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    db_path = tmp_path / "state.db"
    email = "queue-fixture@example.org"

    with SessionDB(db_path=db_path) as db:
        db.create_protected_session("fixture", "cli")
        epoch = runtime.begin_runtime_epoch(db, instance_id="fixture")
        identity = dict(epoch=epoch, principal_id="fixture", session_id="fixture", request_id="once")
        payload = {"text": email, "nested": {"body": email}}
        first = runtime.admit_session_input(db, **identity, payload=payload)
        assert email not in json.dumps(first)
        assert "[[CONVO_SAFE:" in first["payload"]["text"]
        assert first["payload"]["text"] == first["payload"]["nested"]["body"]
        assert runtime.admit_session_input(db, **identity, payload=payload) == first
        with pytest.raises(runtime.RuntimeStoreError, match="admission_conflict"):
            runtime.admit_session_input(db, **identity, payload={"text": "changed"})

    with SessionDB(db_path=db_path) as db:
        saved = runtime.get_session_admission(db, admission_id=first["admission_id"])
        assert saved["payload"] == first["payload"]
    assert email.encode() not in db_path.read_bytes()
