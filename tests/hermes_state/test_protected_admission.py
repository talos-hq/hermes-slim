"""The runtime ledger keeps protected input and outcome text redacted."""

import json

import pytest

import hermes_state_runtime as runtime
from gateway.session_ingress_media import ingress_fingerprint, reconcile_native_retry
from gateway.session_results import admission_result
from hermes_state import SessionDB
from hermes_state_replay import ProtectedReplayError


def test_protected_ledger_projects_text_and_keeps_retry_identity(tmp_path, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    db_path = tmp_path / "state.db"
    email = "queue-fixture@example.org"

    with SessionDB(db_path=db_path) as db:
        db.create_protected_session("fixture", "cli")
        epoch = runtime.begin_runtime_epoch(db, instance_id="fixture")
        identity = dict(epoch=epoch, principal_id="fixture", session_id="fixture", request_id="once")
        native = {"text": email}
        payload = {"text": email, "native_text_v1": native}
        native["ingress_sha256"] = ingress_fingerprint(payload)
        first = runtime.admit_session_input(db, **identity, payload=payload)
        assert email not in json.dumps(first)
        assert "[[CONVO_SAFE:" in first["payload"]["text"]
        assert first["payload"]["text"] == first["payload"]["native_text_v1"]["text"]
        assert runtime.admit_session_input(db, **identity, payload=payload) == first
        retry = reconcile_native_retry(
            db, principal_id="fixture", session_id="fixture", request_id="once", payload=payload,
        )
        assert retry == first["payload"]
        assert runtime.admit_session_input(db, **identity, payload=retry) == first
        with pytest.raises(runtime.RuntimeStoreError, match="admission_conflict"):
            runtime.admit_session_input(db, **identity, payload=first["payload"], intent="steer")
        with pytest.raises(runtime.RuntimeStoreError, match="admission_conflict"):
            runtime.admit_session_input(db, **identity, payload={"text": "changed"})
        claimed = runtime.claim_session_input(db, epoch=epoch, session_id="fixture")
        error = "provider-fixture@example.org"
        runtime.settle_session_input(
            db, epoch=epoch, admission_id=claimed["admission_id"], generation=claimed["generation"],
            outcome="failed", result={"result": {"error": error}},
        )
        stored = admission_result(db, claimed["admission_id"])
        assert "[[CONVO_SAFE:" in stored["result"]["error"]
        assert error not in json.dumps(stored)

    with SessionDB(db_path=db_path) as db:
        saved = runtime.get_session_admission(db, admission_id=first["admission_id"])
        assert saved["payload"] == first["payload"]
    assert email.encode() not in db_path.read_bytes()
    assert error.encode() not in db_path.read_bytes()


def test_protected_rehome_requires_a_destination_with_the_queued_mappings(tmp_path, monkeypatch):
    pytest.importorskip("convo_safe.replay")
    from agent.protected_request import protected_child_config, session_request_keybox

    monkeypatch.setenv("HERMES_CONVO_SAFE_MASTER_KEY", "00" * 32)
    email = "move-fixture@example.org"
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_protected_session("source", "test")
        db.create_session("plain", "test")
        epoch = runtime.begin_runtime_epoch(db, instance_id="fixture")
        identity = dict(epoch=epoch, principal_id="messaging:fixture", request_id="once")
        payload = {"text": email}
        queued = runtime.admit_session_input(db, **identity, session_id="source", payload=payload)
        move = dict(epoch=epoch, old_session_id="source", principal_prefix="messaging:")
        with pytest.raises(ProtectedReplayError):
            runtime.rehome_queued_inputs(db, **move, new_session_id="plain")
        assert runtime.get_session_admission(db, admission_id=queued["admission_id"]) == queued

        config = protected_child_config(db, "source", "next", {})
        db.create_session("next", "test", model_config=config)
        assert runtime.rehome_queued_inputs(db, **move, new_session_id="next") == [queued["admission_id"]]
        moved = runtime.admit_session_input(db, **identity, session_id="next", payload=payload)
        assert moved["admission_id"] == queued["admission_id"]
        assert moved["payload"] == queued["payload"]
        box = session_request_keybox(db, "next")
        assert box.restore_strict(box.replay_session, moved["payload"]["text"], 1024) == email
