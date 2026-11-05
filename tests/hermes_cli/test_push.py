"""Talos authenticated mailbox and encrypted Web Push integration contracts."""

import base64
import asyncio
import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from py_vapid import Vapid


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


@pytest.fixture
def push_server():
    received, statuses = [], deque()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append(({key.lower(): value for key, value in self.headers.items()}, body))
            self.send_response(statuses.popleft() if statuses else 201)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    device_key = ec.generate_private_key(ec.SECP256R1())
    public = device_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b'0123456789abcdef'
    subscription = {'endpoint': f'http://127.0.0.1:{server.server_port}/push',
                    'keys': {'p256dh': _b64(public), 'auth': _b64(auth)}}
    try:
        yield subscription, received, statuses, device_key, auth
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.config import atomic_config_write
    from gateway import push_delivery

    vapid = Vapid()
    vapid.generate_keys()
    private = _b64(vapid.private_key.private_bytes(serialization.Encoding.DER,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv('TALOS_VAPID_PRIVATE_KEY', private)
    atomic_config_write(tmp_path / 'config.yaml', {'push': {'subject': 'mailto:push@example.com'}})
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(web_server, '_SESSION_TOKEN', 'browser-token')
    monkeypatch.setattr(web_server.app.state, 'auth_required', False, raising=False)
    original = push_delivery.deliver

    async def fast_delivery(*args):
        await original(*args, backoff=0)

    monkeypatch.setattr('hermes_cli.web_routers.push.deliver', fast_delivery)
    client = TestClient(web_server.app, base_url='http://127.0.0.1')
    try:
        client.headers['Authorization'] = 'Bearer browser-token'
        yield client, vapid, private
    finally:
        client.close()


def _register(client, subscription, device='iphone'):
    response = client.post('/api/push/subscribe', json={'device_id': device, 'subscription': subscription})
    assert response.status_code == 200, response.text


def _write(client, message_id='event-1'):
    return client.post('/api/push/mailbox', json={
        'message_id': message_id, 'session_id': 'session-1', 'message': 'Your report is ready.'})


@pytest.mark.parametrize('statuses,expected,revoked', [
    ([201], 1, False), ([410], 1, True), ([404], 1, True),
    ([503, 502, 201], 3, False), ([503, 503, 503, 201], 3, False),
    ([401, 201], 1, False), ([403, 201], 1, False), ([302, 201], 1, False),
])
def test_encrypted_delivery_retry_policy(api, push_server, statuses, expected, revoked):
    client, vapid, _private = api
    subscription, received, queue, device_key, auth = push_server
    queue.extend(statuses)
    _register(client, subscription)
    response = _write(client)
    assert response.status_code == 200, response.text
    assert len(received) == expected
    headers, encrypted = received[0]
    assert headers['content-encoding'] == 'aes128gcm'
    assert json.loads(http_ece.decrypt(encrypted, private_key=device_key, auth_secret=auth)) == {
        'message_id': 'event-1'}
    assert b'Your report' not in encrypted
    token = headers['authorization'].split('t=', 1)[1].split(',', 1)[0]
    claims = jwt.decode(token, vapid.public_key, algorithms=['ES256'],
                        audience=subscription['endpoint'].rsplit('/push', 1)[0])
    assert claims['sub'] == 'mailto:push@example.com'
    assert _write(client, 'event-2').status_code == 200
    assert len(received) == expected + (0 if revoked else 1)
    messages = client.get('/api/push/mailbox').json()['messages']
    assert len(messages) == 2  # even a rejected or expired push cannot discard history


def test_duplicate_id_and_conflicting_reuse(api, push_server):
    client, _vapid, _private = api
    subscription, received, *_rest = push_server
    _register(client, subscription)
    assert _write(client).json()['created'] is True
    assert _write(client).json()['created'] is False
    response = client.post('/api/push/mailbox', json={
        'message_id': 'event-1', 'session_id': 'other', 'message': 'Different content'})
    assert response.status_code == 409
    assert len(received) == 1


def test_atomic_claim_survives_reopen_and_concurrent_writers(tmp_path):
    from gateway.push_store import PushStore
    from hermes_state import SessionDB

    def claim(_index):
        with SessionDB(tmp_path / 'state.db') as db:
            return PushStore(db).admit('user', 'event', 'session', 'Message')

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    with SessionDB(tmp_path / 'state.db') as db:
        store = PushStore(db)
        assert len(store.mailbox('user')) == 1
        assert store.mailbox('another-user') == []
        assert not store.mark_read('another-user', 'event')


def test_replace_revoke_and_mailbox_read_state(api, push_server):
    client, _vapid, _private = api
    subscription, received, *_rest = push_server
    _register(client, subscription | {'endpoint': subscription['endpoint'] + '/old'})
    _register(client, subscription)
    assert _write(client).status_code == 200
    assert len(received) == 1
    rows = client.get('/api/push/mailbox').json()
    message = rows['messages'][0]
    assert message['id'] == 'event-1' and message['session_id'] == 'session-1'
    assert message['timestamp'] and message['read'] is False
    assert client.patch('/api/push/mailbox/event-1/read').status_code == 200
    assert client.get('/api/push/mailbox').json()['messages'][0]['read'] is True
    assert client.get('/api/push/mailbox', params={'after': rows['next_cursor']}).json()['messages'] == []
    assert client.delete('/api/push/subscribe?device_id=iphone').status_code == 200
    assert _write(client, 'event-2').status_code == 200
    assert len(received) == 1


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/api/push/subscribe', {}), ('DELETE', '/api/push/subscribe?device_id=iphone', None),
    ('GET', '/api/push/vapid-public-key', None), ('GET', '/api/push/mailbox', None),
    ('POST', '/api/push/mailbox', {}), ('PATCH', '/api/push/mailbox/id/read', None),
])
def test_unauthenticated_rejected(api, method, path, body):
    client, _vapid, _private = api
    client.headers.pop('Authorization')
    assert client.request(method, path, json=body).status_code == 401


def test_public_key_contains_only_public_material(api):
    client, vapid, private = api
    response = client.get('/api/push/vapid-public-key')
    assert response.status_code == 200, response.text
    public = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    assert response.json() == {'public_key': _b64(public)}
    assert private not in response.text
    assert 'private' not in response.text
    revealed = client.post('/api/env/reveal', json={'key': 'TALOS_VAPID_PRIVATE_KEY'})
    assert revealed.status_code == 403
    assert private not in revealed.text


def test_cross_user_write_is_rejected(api):
    client, _vapid, _private = api
    response = client.post('/api/push/mailbox', json={
        'message_id': 'id', 'session_id': 'session', 'message': 'Text', 'user_id': 'someone-else'})
    assert response.status_code == 403


def test_webapp_cookie_auth_and_origin_gate(api, tmp_path):
    from types import SimpleNamespace
    from gateway.run_api import GatewayDashboardMount
    from gateway.webapp_listener import WebappMount
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<html>Webapp</html>')
    mount = GatewayDashboardMount(SimpleNamespace(session_authority=None), SimpleNamespace(
        bound_host='0.0.0.0', bound_port=9120, webapp=WebappMount(dist, tmp_path)))
    clear_providers()
    register_provider(StubAuthProvider())

    async def scenario():
        try:
            await mount.ensure()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mount),
                                          base_url='http://hermes.lan:9120') as browser:
                assert (await browser.get('/api/push/mailbox')).status_code == 401
                await browser.get('/auth/login?provider=stub', follow_redirects=True)
                body = {'message_id': 'cookie-event', 'session_id': 'session', 'message': 'Ready'}
                bad = await browser.post('/api/push/mailbox', json=body,
                                         headers={'Origin': 'https://evil.example'})
                assert bad.status_code == 403
                own = {'Origin': 'http://hermes.lan:9120'}
                assert (await browser.post('/api/push/mailbox', json=body, headers=own)).status_code == 200
                rows = (await browser.get('/api/push/mailbox')).json()['messages']
                assert [row['id'] for row in rows] == ['cookie-event']
                assert (await browser.get('/api/push/vapid-public-key')).status_code == 200
        finally:
            await mount.close()

    try:
        asyncio.run(scenario())
    finally:
        clear_providers()


def test_expired_generation_cannot_revoke_replacement(tmp_path):
    from gateway.push_store import PushStore
    from hermes_state import SessionDB
    with SessionDB(tmp_path / 'state.db') as db:
        store = PushStore(db)
        store.register('owner', 'iphone', {'endpoint': 'old'})
        store.register('owner', 'iphone', {'endpoint': 'new'})
        store.revoke('owner', 'iphone', {'endpoint': 'old'})
    with SessionDB(tmp_path / 'state.db') as db:
        store = PushStore(db)
        assert store.subscriptions('owner') == [('iphone', {'endpoint': 'new'})]
        assert store.subscriptions('other') == []
        store.revoke('other', 'iphone')
        assert store.subscriptions('owner')


@pytest.mark.parametrize('prefix', ['/api/push', '/api/push/v1'])
def test_versioned_mailbox_and_alias_headers(api, prefix):
    client, _, _ = api
    for suffix in ['/mailbox', '/mailbox/unknown']:
        response = client.get(prefix + suffix)
        assert response.status_code == (200 if suffix == '/mailbox' else 404)
        if prefix == '/api/push':
            assert response.headers['Deprecation'] == 'true'
            assert response.headers['Link'] == f'</api/push/v1{suffix}>; rel="successor-version"'
        else:
            assert 'Deprecation' not in response.headers


def test_header_idempotency_is_durable_and_owner_scoped(api, monkeypatch):
    from hermes_cli.web_routers import push
    client, _, _ = api
    calls = []
    async def delivery(*args):
        calls.append(args[2])
    monkeypatch.setattr(push, 'deliver', delivery)
    body = {'message_id': 'keyed', 'session_id': 's', 'message': 'hello'}
    headers = {'Idempotency-Key': 'retry-key'}
    first = client.post('/api/push/mailbox', json=body, headers=headers)
    replay = client.post('/api/push/mailbox', json=body, headers=headers)
    assert first.json()['created'] is True
    assert replay.json()['created'] is False
    conflict = client.post('/api/push/mailbox', json=body | {'message_id': 'new'}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['code'] == 'idempotency_conflict'
    assert len(client.get('/api/push/mailbox').json()['messages']) == 1
    assert calls == ['keyed']
    monkeypatch.setattr(push, '_owner', lambda request: ('other-owner', 'other'))
    assert client.post('/api/push/mailbox', json=body, headers=headers).json()['created'] is True


def test_mailbox_limit_cursor_and_owner_fetch(api, monkeypatch):
    from hermes_cli.web_routers import push
    client, _, _ = api
    for index in range(5):
        assert _write(client, f'page-{index}').status_code == 200
    cursor, ids = 0, []
    for count, more in [(2, True), (2, True), (1, False), (0, False)]:
        page = client.get('/api/push/mailbox', params={'after': cursor, 'limit': 2}).json()
        assert len(page['messages']) == count
        assert page['has_more'] is more
        ids.extend(row['id'] for row in page['messages'])
        cursor = page['next_cursor']
    assert ids == [f'page-{index}' for index in range(5)]
    exact = client.get('/api/push/mailbox', params={'limit': 5}).json()
    assert exact['has_more'] is False
    for limit in [0, 201]:
        assert client.get('/api/push/mailbox', params={'limit': limit}).status_code == 422
    own = client.get('/api/push/mailbox/page-0')
    assert own.status_code == 200
    assert own.json()['id'] == 'page-0'
    for owner in [None, 'other-owner']:
        if owner:
            monkeypatch.setattr(push, '_owner', lambda request: (owner, 'other'))
        response = client.get('/api/push/mailbox/' + ('page-0' if owner else 'unknown'))
        assert response.status_code == 404
        assert response.json()['detail']['code'] == 'message_not_found'


@pytest.fixture
def limited_api(api, monkeypatch):
    from hermes_cli import push_rate_limit as limits
    from hermes_cli.config import atomic_config_write, get_hermes_home
    clock = [100.0]
    monkeypatch.setattr(limits, '_clock', lambda: clock[0])
    limits._reset_rate_limit()
    atomic_config_write(get_hermes_home() / 'config.yaml', {'push': {'subject': 'mailto:push@example.com',
        'rate_limit': {'mailbox_owner': 2, 'mailbox_ip': 20, 'subscribe_owner': 2,
                       'subscribe_ip': 20, 'window_seconds': 60, 'max_buckets': 8}}})
    yield api[0], clock
    limits._reset_rate_limit()


@pytest.mark.parametrize('prefix', ['/api/push', '/api/push/v1'])
def test_mailbox_rate_limit_owner_and_window(limited_api, monkeypatch, prefix):
    from hermes_cli.web_routers import push
    client, clock = limited_api
    body = {'message_id': 'limited', 'session_id': 's', 'message': 'hello'}
    for _ in range(2):
        assert client.post(prefix + '/mailbox', json=body).status_code == 200
    response = client.post(prefix + '/mailbox', json=body)
    assert response.status_code == 429
    assert response.json()['detail']['code'] == 'rate_limited'
    assert response.headers['Retry-After'] == '60'
    with monkeypatch.context() as scoped:
        scoped.setattr(push, '_owner', lambda request: ('other-owner', 'other'))
        assert client.post(prefix + '/mailbox', json=body).status_code == 200
    clock[0] += 60
    assert client.post(prefix + '/mailbox', json=body).status_code == 200


@pytest.mark.parametrize('gated', [False, True])
def test_connector_scopes_revocation_and_rotation(api, monkeypatch, gated):
    from hermes_cli import connector_tokens, browser_credential
    client = api[0]
    monkeypatch.setattr(client.app.state, 'auth_required', gated)
    record, token = connector_tokens.create('agent', ['mailbox:write'])
    client.headers['Authorization'] = 'Bearer ' + token
    assert _write(client).status_code == 200
    response = client.post('/api/push/v1/subscribe', json={'device_id': 'phone',
        'subscription': {'endpoint': 'https://example.com', 'keys': {'auth': 'a', 'p256dh': 'b'}}})
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'insufficient_scope'
    connector_tokens.revoke(record['id'])
    assert _write(client).status_code == 401
    client.headers['Authorization'] = 'Bearer unknown-token'
    assert _write(client).status_code == 401
    record, token = connector_tokens.create('agent two', ['mailbox:write', 'subscribe:write'])
    browser_credential.rotate('replacement-browser-token')
    assert next(row for row in connector_tokens.list_tokens() if row['id'] == record['id'])['revoked_at']
    client.headers['Authorization'] = 'Bearer ' + token
    assert _write(client).status_code == 401


def test_subscribe_rate_limit_and_connector(api, limited_api, push_server):
    from hermes_cli import connector_tokens
    client, clock = limited_api
    subscription, *_ = push_server
    _, token = connector_tokens.create('subscriber', ['subscribe:write'])
    client.headers['Authorization'] = 'Bearer ' + token
    body = {'device_id': 'phone', 'subscription': subscription}
    for _ in range(2):
        assert client.post('/api/push/v1/subscribe', json=body).status_code == 200
    assert client.post('/api/push/subscribe', json=body).status_code == 429
    clock[0] += 60
    assert client.post('/api/push/v1/subscribe', json=body).status_code == 200


@pytest.mark.parametrize('route', ['mailbox', 'subscribe'])
def test_push_ip_budget_includes_unauthenticated_attempts(limited_api, route):
    client, clock = limited_api
    client.headers.pop('Authorization')
    for _ in range(20):
        assert client.post('/api/push/v1/' + route, json={}).status_code == 401
    response = client.post('/api/push/' + route, json={})
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '60'
    clock[0] += 60
    assert client.post('/api/push/v1/' + route, json={}).status_code == 401


def test_push_bucket_capacity_and_expiry(limited_api):
    from hermes_cli import push_rate_limit as limits
    _, clock = limited_api
    for i in range(12):
        limits.check('mailbox', 'owner', str(i))
    assert len(limits._attempts) == 8
    clock[0] += 60
    limits.check('mailbox', 'owner', 'fresh')
    assert len(limits._attempts) == 1


def test_connector_cli_metadata_and_hash_storage(api, capsys):
    import argparse
    import hashlib
    from hermes_cli import connector_tokens, talos_messaging
    from hermes_cli.subcommands.talos_messaging import build_talos_messaging_parser
    from hermes_state import SessionDB
    parser = argparse.ArgumentParser()
    build_talos_messaging_parser(parser.add_subparsers(), cmd=talos_messaging.run)
    args = parser.parse_args(['talos-messaging', 'connector-token', 'create', '--name', 'reporter'])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    with SessionDB() as db:
        row = dict(db._read_one('SELECT * FROM push_connector_tokens WHERE id=?', (result['id'],)))
    assert row['digest'] == hashlib.sha256(result['token'].encode()).hexdigest()
    assert result['token'] not in json.dumps(row)
    args = parser.parse_args(['talos-messaging', 'connector-token', 'list'])
    args.func(args)
    listed = capsys.readouterr().out
    assert result['token'] not in listed
    assert json.loads(listed)[0]['name'] == 'reporter'
    args = parser.parse_args(['talos-messaging', 'connector-token', 'revoke', '--token-id', result['id']])
    args.func(args)
    assert connector_tokens.authenticate(result['token']) is None
