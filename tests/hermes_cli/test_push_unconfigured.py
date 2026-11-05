"""Mailbox durability is independent of optional push-service configuration."""

from fastapi.testclient import TestClient


def test_unconfigured_push_still_commits_mailbox(tmp_path, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('TALOS_VAPID_PRIVATE_KEY', raising=False)
    monkeypatch.setattr(web_server, '_SESSION_TOKEN', 'browser-token')
    monkeypatch.setattr(web_server.app.state, 'auth_required', False, raising=False)
    client = TestClient(web_server.app, base_url='http://127.0.0.1',
                        headers={'Authorization': 'Bearer browser-token'})
    try:
        assert client.get('/api/push/vapid-public-key').status_code == 503
        body = {'message_id': 'event', 'session_id': 'session', 'message': 'Ready'}
        written = client.post('/api/push/mailbox', json=body)
        assert written.status_code == 200
        assert written.json()['created'] is True
        assert client.post('/api/push/mailbox', json=body).json()['created'] is False
        assert client.get('/api/push/mailbox').json()['messages'][0]['id'] == 'event'
    finally:
        client.close()
