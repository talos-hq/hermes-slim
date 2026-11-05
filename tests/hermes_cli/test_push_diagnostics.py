"""Durable owner diagnostics and stable v1 wire contracts (#110)."""

import pytest

from tests.hermes_cli.test_push import _register, _write

pytest_plugins = ["tests.hermes_cli.test_push"]


@pytest.mark.parametrize("prefix", ["/api/push", "/api/push/v1"])
@pytest.mark.parametrize("method,path,body,status,code", [
    ("GET", "/mailbox/missing", None, 404, "message_not_found"),
    ("PATCH", "/mailbox/missing/read", None, 404, "message_not_found"),
    ("POST", "/mailbox", {}, 422, "validation_error"),
    ("POST", "/subscribe", {}, 422, "validation_error"),
    ("POST", "/mailbox", {"message_id": "m", "session_id": "s", "message": "hi", "user_id": "other"}, 403, "recipient_mismatch"),
])
def test_error_contract(api, prefix, method, path, body, status, code):
    client, _, _ = api
    response = client.request(method, prefix + path, json=body)
    assert response.status_code == status
    assert set(response.json()) == {"code", "message"}
    assert response.json()["code"] == code
    assert isinstance(response.json()["message"], str)


def test_status_delivery_failure_revocation_and_reopen(api, push_server, monkeypatch, tmp_path):
    from hermes_cli.web_routers import push
    from gateway.push_store import PushStore
    from hermes_state import SessionDB

    client, _, _ = api
    subscription, _, statuses, *_ = push_server
    _register(client, subscription)
    for index, status in enumerate([201, 403, 410]):
        statuses.append(status)
        assert _write(client, str(index)).status_code == 200
    response = client.get("/api/push/v1/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["subscriptions"] == 0
    assert payload["counters"] == {"sent": 1, "failed": 2, "revoked": 1}
    assert payload["last_delivery"]["attempted_at"]
    assert payload["last_delivery"]["result"] == "failed"
    assert payload["last_revocation"]["at"]
    assert payload["last_revocation"]["reason"] == "410"
    import json
    from hermes_cli.web_routers.webapp import SESSION_TOKEN_OWNER
    identity = SESSION_TOKEN_OWNER
    owner = json.dumps([identity['provider'], identity.get('issuer', ''), identity['user_id']])
    with SessionDB() as db:
        assert PushStore(db).status(owner) == payload
    monkeypatch.setattr(push, "_ticket_principal", lambda request: (
        {"provider": "other-provider", "user_id": "other", "issuer": ""}, None))
    assert client.get("/api/push/v1/status").json() == {
        "subscriptions": 0, "last_delivery": None, "last_revocation": None,
        "counters": {"sent": 0, "failed": 0, "revoked": 0}}


@pytest.mark.parametrize('prefix', ['/api/push', '/api/push/v1'])
@pytest.mark.parametrize('kind,status,code', [
    ('auth', 401, 'unauthenticated'),
    ('conflict', 409, 'message_conflict'),
    ('idempotency', 409, 'idempotency_conflict'),
    ('subscription', 422, 'validation_error'),
    ('unconfigured', 503, 'push_unconfigured'),
    ('extras', 503, 'push_extras_missing'),
])
def test_remaining_error_contract(api, monkeypatch, prefix, kind, status, code):
    from gateway import push_delivery
    from hermes_cli.web_routers import push
    client, _, _ = api
    method, path, body, headers = 'GET', '/vapid-public-key', None, {}
    if kind == 'auth':
        client.headers.pop('Authorization')
    if kind in ('conflict', 'idempotency'):
        method, path = 'POST', '/mailbox'
        body = {'message_id': 'one', 'session_id': 's', 'message': 'first'}
        headers = {'Idempotency-Key': 'key'} if kind == 'idempotency' else {}
        assert client.post(prefix + path, json=body, headers=headers).status_code == 200
        body['message'] = 'different'
    if kind == 'subscription':
        method, path = 'POST', '/subscribe'
        body = {'device_id': 'phone', 'subscription': {
            'endpoint': 'https://example.com', 'keys': {'p256dh': 'bad', 'auth': 'bad'}}}
    if kind == 'unconfigured':
        monkeypatch.setattr(push.PushSettings, 'load', lambda: push_delivery.PushSettings('', ''))
    if kind == 'extras':
        monkeypatch.setattr(push_delivery, 'PUSH_SDK_AVAILABLE', False)
    response = client.request(method, prefix + path, json=body, headers=headers)
    assert response.status_code == status
    assert set(response.json()) == {'code', 'message'}
    assert response.json()['code'] == code
    assert isinstance(response.json()['message'], str)


def test_retry_counters_and_stale_revocation(tmp_path, monkeypatch):
    import asyncio
    from gateway import push_delivery
    from gateway.push_store import PushStore
    from hermes_state import SessionDB
    path = tmp_path / 'durable.db'
    old = {'endpoint': 'https://old.example', 'keys': {}}
    new = old | {'endpoint': 'https://new.example'}
    statuses = iter([503, 429, 201])
    monkeypatch.setattr(push_delivery, '_send_once', lambda *args: next(statuses))
    with SessionDB(path) as db:
        store = PushStore(db)
        store.register('owner', 'phone', old)
        asyncio.run(push_delivery.deliver(store, 'owner', 'm',
                    push_delivery.PushSettings('key', 'subject'), backoff=0))
        store.register('owner', 'phone', new)
        store.revoke('owner', 'phone', old, reason='410')
        payload = store.status('owner')
        assert payload['subscriptions'] == 1
        assert payload['counters'] == {'sent': 1, 'failed': 2, 'revoked': 0}
        assert payload['last_delivery']['result'] == 'sent'
        assert payload['last_revocation'] is None
    with SessionDB(path) as db:
        assert PushStore(db).status('owner') == payload


@pytest.mark.parametrize('prefix', ['/api/push', '/api/push/v1'])
@pytest.mark.parametrize('kind,status,code', [
    ('revoked', 401, 'unauthenticated'), ('scope', 403, 'insufficient_scope'),
    ('rate', 429, 'rate_limited'),
])
def test_connector_and_limit_errors(api, monkeypatch, prefix, kind, status, code):
    from hermes_cli import connector_tokens, push_rate_limit
    from fastapi import HTTPException
    client, _, _ = api
    if kind == 'rate':
        def limited(*args):
            raise HTTPException(429, {'code': 'rate_limited', 'message': 'Try later.'},
                                headers={'Retry-After': '60'})
        monkeypatch.setattr(push_rate_limit, 'check', limited)
        response = client.post(prefix + '/mailbox', json={})
        assert response.headers['Retry-After'] == '60'
    else:
        record, token = connector_tokens.create('test', ['mailbox:write'])
        if kind == 'revoked':
            connector_tokens.revoke(record['id'])
        client.headers['Authorization'] = 'Bearer ' + token
        response = client.get(prefix + '/status')
    assert response.status_code == status
    assert set(response.json()) == {'code', 'message'}
    assert response.json()['code'] == code


@pytest.mark.parametrize('prefix', ['/api/push', '/api/push/v1'])
def test_unhandled_error_is_generic(api, monkeypatch, prefix):
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers import push

    def unavailable():
        raise RuntimeError('private provider response')
    monkeypatch.setattr(push, '_store', unavailable)
    client = TestClient(api[0].app, base_url='http://127.0.0.1', raise_server_exceptions=False)
    with client:
        response = client.get(prefix + '/status', headers=api[0].headers)
    assert response.status_code == 500
    assert response.json() == {'code': 'internal_error', 'message': 'Request failed.'}
