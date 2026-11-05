"""Exercise the smoke CLI over HTTP, including replay and cleanup failures."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts import talos_push_smoke as smoke
from scripts import talos_vapid_keygen as keygen


@pytest.mark.parametrize('fault', [None, 'auth', 'duplicate', 'revoke', 'redirect', 'malformed', 'write'])
def test_smoke_against_stub(fault, monkeypatch, capsys):
    messages, devices, requests = [], set(), []
    public = keygen.encode(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            requests.append((self.command, self.path))
            status, body = 200, {}
            if fault == 'redirect':
                status = 302
            elif fault == 'auth' or self.headers.get('Authorization') != 'Bearer stub-secret':
                status = 401
            elif self.path.endswith('/vapid-public-key'):
                body = {'public_key': public} if fault != 'malformed' else {'public_key': 'bad'}
            elif self.command == 'POST':
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/subscribe'):
                    smoke.valid_public_key(payload['subscription']['keys']['p256dh'])
                    devices.add(payload['device_id'])
                    body = {'registered': True}
                else:
                    assert self.headers['Idempotency-Key'] == payload['message_id']
                    if fault == 'write':
                        status = 500
                    created = not messages or fault == 'duplicate'
                    if created:
                        messages.append({'id': payload['message_id'], 'sequence': len(messages) + 1,
                                         'session_id': payload['session_id'], 'message': payload['message']})
                    body = {'id': payload['message_id'], 'created': created}
            elif self.command == 'DELETE':
                if fault == 'revoke':
                    status = 503
                else:
                    devices.clear()
                body = {'revoked': True}
            else:
                after = int(self.path.split('after=')[1])
                page = [row for row in messages if row['sequence'] > after]
                body = {'messages': page, 'next_cursor': page[-1]['sequence'] if page else after}
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/redirect-target')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        do_GET = do_POST = do_DELETE = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv('TALOS_API_TOKEN', 'stub-secret')
    try:
        result = smoke.main([f'http://127.0.0.1:{server.server_port}'])
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    output = capsys.readouterr().out
    assert result == (0 if fault is None else 1)
    assert 'NOT delivery to a device' in output
    assert 'stub-secret' not in output
    if fault in {'auth', 'redirect', 'malformed'}:
        assert len(requests) == 1
        assert str({'auth': 401, 'redirect': 302, 'malformed': 200}[fault]) in output
    elif fault == 'revoke':
        assert '| 503 | FAIL |' in output
    else:
        assert not devices
        assert len(messages) == (2 if fault == 'duplicate' else 1)
        assert requests[-1][0] == 'DELETE'


def test_keygen_requires_destination_and_never_prints_private(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        keygen.main([])
    assert error.value.code == 2
    path = tmp_path / 'vapid.key'
    assert keygen.main(['--private-key-out', str(path)]) == 0
    private = path.read_text(encoding='ascii').strip()
    assert private not in capsys.readouterr().out
    assert len(private) == 43
    with pytest.raises(SystemExit):
        keygen.main(['--private-key-out', str(path)])
    assert path.read_text(encoding='ascii').strip() == private


@pytest.mark.platforms("posix")
def test_keygen_env_destination_mode_and_compatible_key(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'key'
    monkeypatch.setenv('TALOS_VAPID_KEY_OUT', str(path))
    assert keygen.main([]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    private = path.read_text(encoding='ascii').strip()
    import base64

    scalar = int.from_bytes(base64.urlsafe_b64decode(private + '='), 'big')
    key = ec.derive_private_key(scalar, ec.SECP256R1())
    public = keygen.encode(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    assert public in capsys.readouterr().out
