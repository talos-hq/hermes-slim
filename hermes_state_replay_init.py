"""Initialize a protected transcript before it can receive conversation data."""

from hermes_dir_acl import make_user_dir
from hermes_state_replay import (
    REPLAY_BINDING_KEY,
    ProtectedReplayError,
    _master_key,
    _open_sidecar,
    _project_value,
    open_unbound_sidecar,
    replay_log_path,
)
from hermes_state_replay_fallback import _read_binding, remember_fallback_binding


def _prepare_sidecar(db_path, session_id):
    from convo_safe import Keybox
    from convo_safe.replay import replay_binding

    log = replay_log_path(db_path, session_id)
    make_user_dir(log.parent, mode=0o700, parents=True, exist_ok=True)
    try:
        binding = _read_binding(log, session_id)
    except FileNotFoundError:
        sidecar = log.with_name(log.name + ".keybox.sqlite")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            source = Keybox(":memory:", _master_key())
            source.checkpoint(source.create_session(), log)
        else:
            raise ProtectedReplayError("Protected session has an unbound sidecar")
        binding = replay_binding(open_unbound_sidecar(log))
        remember_fallback_binding(db_path, session_id, binding)
    return binding, _open_sidecar(db_path, session_id, binding, writable=True)


def create_protected_session(db, session_id, source, *, system_prompt=None, model=None):
    """Create a fresh protected session under the normal SQLite writer lease."""
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    from hermes_state_sessions import _parse_model_config
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction

    if not session_id or not source:
        raise ValueError("Protected session requires a session ID and source")

    def publish(conn):
        row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is not None:
            binding = _parse_model_config(row[0]).get(REPLAY_BINDING_KEY)
            if binding is None:
                raise ProtectedReplayError("Existing session has no protected replay binding")
            _open_sidecar(db.db_path, session_id, binding, writable=True)
            remember_fallback_binding(db.db_path, session_id, binding)
            return
        if conn.execute("SELECT 1 FROM state_meta WHERE key = ?", (RETIRED_PREFIX + session_id,)).fetchone():
            raise ProtectedReplayError("Protected session ID has been retired")
        binding, keybox = _prepare_sidecar(db.db_path, session_id)
        insert_session_row_in_transaction(
            db, conn, session_id=session_id, source=source, model=model,
            model_config={REPLAY_BINDING_KEY: binding},
            system_prompt=_project_value(keybox, system_prompt),
        )

    try:
        db._execute_write(publish, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
    except (ImportError, OSError, ValueError):
        raise ProtectedReplayError("Protected session could not be initialized") from None
    return session_id
