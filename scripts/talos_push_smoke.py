#!/usr/bin/env python3
"""Probe enrollment and durable mailbox behavior; no device delivery is tested."""

import argparse
import base64
import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


class NoRedirect(HTTPRedirectHandler):
    """Do not forward credentials to redirected origins or login pages."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base, token, method, path, body, timeout):
    data = None if body is None else json.dumps(body).encode()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    if method == 'POST' and path == '/api/push/mailbox':
        headers['Idempotency-Key'] = body['message_id']
    req = Request(base + path, data=data, method=method, headers=headers)
    try:
        response = build_opener(NoRedirect()).open(req, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = None
        return response.code, payload


def run(base, token, timeout=45):
    rows = []
    device = 'smoke-' + uuid.uuid4().hex
    message = {'message_id': device, 'session_id': device, 'message': 'Talos deployment smoke test'}

    def check(label, method, path, body, predicate):
        try:
            code, payload = request(base, token, method, path, body, timeout)
        except (OSError, URLError, ValueError):
            code, payload = 'NO RESPONSE', None
        try:
            passed = code == 200 and predicate(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            passed = False
        rows.append((label, code, passed))
        return passed

    key_ok = check('VAPID public key', 'GET', '/api/push/vapid-public-key', None,
                   lambda p: valid_public_key(p['public_key']))
    if key_ok:
        key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        subscription = {'device_id': device, 'subscription': {
            'endpoint': 'https://example.invalid/talos-smoke/' + device,
            'keys': {'p256dh': base64.urlsafe_b64encode(key).rstrip(b'=').decode(),
                     'auth': base64.urlsafe_b64encode(os.urandom(16)).rstrip(b'=').decode()}}}
        try:
            enrolled = check('Register fake subscription', 'POST', '/api/push/subscribe', subscription,
                             lambda p: p['registered'] is True)
            if enrolled:
                check('Mailbox first write', 'POST', '/api/push/mailbox', message,
                      lambda p: p['created'] is True and p['id'] == device)
                check('Mailbox identical replay', 'POST', '/api/push/mailbox', message,
                      lambda p: p['created'] is False and p['id'] == device)
                verify_mailbox(check, device, message)
        finally:
            check('Revoke subscription', 'DELETE', '/api/push/subscribe?' + urlencode({'device_id': device}),
                  None, lambda p: p['revoked'] is True)
    print('| Check | HTTP code | Result |')
    print('| --- | --- | --- |')
    for label, code, passed in rows:
        print(f'| {label} | {code} | {"PASS" if passed else "FAIL"} |')
    print('A 2xx on a push endpoint is NOT delivery to a device. No phone/provider receipt was tested.')
    print('The unique smoke mailbox message remains in history; subscription cleanup was attempted (see Revoke result).')
    return 0 if rows and all(row[2] for row in rows) else 1


def valid_public_key(value):
    ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
    return True


def verify_mailbox(check, device, message):
    cursor, matches = 0, []
    for page in range(1000):
        def verify(payload):
            nonlocal cursor
            messages = payload['messages']
            matches.extend(row for row in messages if row['id'] == device)
            next_cursor = payload['next_cursor']
            if messages and next_cursor <= cursor:
                return False
            cursor = next_cursor
            return True
        old_cursor = cursor
        if not check(f'Mailbox page {page + 1}', 'GET', f'/api/push/mailbox?after={cursor}', None, verify):
            return
        if cursor == old_cursor:
            check('Exactly one matching message', 'GET', f'/api/push/mailbox?after={cursor}', None,
                  lambda p: len(matches) == 1 and matches[0]['message'] == message['message']
                  and matches[0]['session_id'] == message['session_id'] and not p['messages'])
            return
    check('Mailbox page bound exceeded', 'GET', '/api/push/mailbox', None, lambda p: False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_url')
    parser.add_argument('--token-env', default='TALOS_API_TOKEN', help='Environment variable holding connector token')
    parser.add_argument('--timeout', type=float, default=45, help='Per-request timeout in seconds')
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1', 'localhost'})) or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
        parser.error('base URL must be an HTTPS origin (HTTP allowed only on loopback)')
    token = os.environ.get(args.token_env)
    if not token or args.timeout <= 0:
        parser.error('require a token environment variable and positive timeout')
    return run(args.base_url.rstrip('/'), token, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
