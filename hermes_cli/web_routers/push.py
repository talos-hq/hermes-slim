"""Authenticated device enrollment and the authoritative Talos mailbox."""

import base64
import json
from contextlib import contextmanager
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.push_delivery import PushSdkMissing, PushSettings, deliver
from gateway.push_store import PushStore
from hermes_cli.web_routers.webapp import _ticket_principal
from hermes_cli import connector_tokens, push_rate_limit

router = APIRouter()


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(max_length=128)
    auth: str = Field(max_length=64)


class Subscription(BaseModel):
    endpoint: str = Field(max_length=4096)
    keys: SubscriptionKeys


class Enrollment(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    subscription: Subscription


class MailboxMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=16000)
    user_id: str | None = Field(default=None, max_length=256)


def _owner(request):
    if getattr(request.state, 'push_connector', None):
        from hermes_cli.web_routers.webapp import SESSION_TOKEN_OWNER
        identity = SESSION_TOKEN_OWNER
    else:
        identity, _token = _ticket_principal(request)
    return json.dumps([identity['provider'], identity.get('issuer', ''), identity['user_id']]), identity['user_id']


@contextmanager
def _store():
    from hermes_state import SessionDB
    with SessionDB() as db:
        yield PushStore(db)


def _validate_subscription(subscription):
    url = urlsplit(subscription['endpoint'])
    # Remote endpoints must use TLS. Literal loopback HTTP permits local integration tests.
    local = url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1'}
    if ((url.scheme != 'https' and not local) or not url.hostname
            or url.username or url.password or url.fragment):
        raise ValueError('Push endpoint requires HTTPS')
    keys = subscription['keys']
    public = base64.urlsafe_b64decode(keys['p256dh'] + '=' * (-len(keys['p256dh']) % 4))
    auth = base64.urlsafe_b64decode(keys['auth'] + '=' * (-len(keys['auth']) % 4))
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    if len(auth) != 16:
        raise ValueError('Invalid push auth key')


@router.post('/subscribe')
def subscribe(request: Request, body: Enrollment):
    owner, _user = _owner(request)
    _limit_owner(request, 'subscribe', owner)
    subscription = body.subscription.model_dump()
    try:
        _validate_subscription(subscription)
    except ValueError:
        raise HTTPException(422, 'Invalid push subscription') from None
    with _store() as store:
        store.register(owner, body.device_id, subscription)
    return {'registered': True}


@router.delete('/subscribe')
def revoke(request: Request, device_id: str = Query(min_length=1, max_length=256)):
    owner, _user = _owner(request)
    with _store() as store:
        store.revoke(owner, device_id)
    return {'revoked': True}


@router.get('/vapid-public-key')
def public_key(request: Request):
    _owner(request)
    settings = PushSettings.load()
    if not settings.private_key or not settings.subject:
        raise HTTPException(503, 'Push is not configured')
    try:
        key = settings.public_key()
    except PushSdkMissing:
        raise HTTPException(
            503,
            'Web Push extras are not installed; install the optional push dependencies to enable '
            'notification delivery. The mailbox keeps working without them.',
        ) from None
    except (ValueError, TypeError):
        raise HTTPException(503, 'Push is not configured') from None
    return JSONResponse({'public_key': key}, headers={'Cache-Control': 'no-store'})


@router.post('/mailbox')
async def write_mailbox(request: Request, body: MailboxMessage):
    owner, user = _owner(request)
    _limit_owner(request, 'mailbox', owner)
    if body.user_id is not None and body.user_id != user:
        raise HTTPException(403, 'Mailbox recipient must match the authenticated user')
    with _store() as store:
        try:
            admitted = store.admit(owner, body.message_id, body.session_id, body.message,
                                   key=request.headers.get('Idempotency-Key'),
                                   body=json.dumps(await request.json(), sort_keys=True, separators=(',', ':')))
        except ValueError as error:
            code = 'idempotency_conflict' if str(error) == 'idempotency_conflict' else 'message_conflict'
            raise HTTPException(409, {'code': code, 'message': 'Key or message id already used for different content'}) from None
        if admitted:
            # Delivery failure cannot roll back or invalidate the committed mailbox receipt.
            try:
                await deliver(store, owner, body.message_id, PushSettings.load())
            except (ValueError, TypeError):
                pass  # Invalid server key: history remains available; never echo the key/error.
    return {'id': body.message_id, 'created': admitted}


@router.get('/mailbox')
def mailbox(request: Request, after: int = Query(default=0, ge=0),
            limit: int = Query(default=50, ge=1, le=200)):
    owner, _user = _owner(request)
    with _store() as store:
        rows = store.mailbox(owner, after, limit + 1)
        messages = rows[:limit]
    return JSONResponse({'messages': messages, 'has_more': len(rows) > limit, 'next_cursor': messages[-1]['sequence'] if messages else after},
                        headers={'Cache-Control': 'no-store'})


@router.patch('/mailbox/{message_id}/read')
def mark_read(request: Request, message_id: str):
    owner, _user = _owner(request)
    with _store() as store:
        found = store.mark_read(owner, message_id)
    if not found:
        raise HTTPException(404, 'Message not found')
    return {'read': True}


@router.get('/mailbox/{message_id}')
def message(request: Request, message_id: str):
    owner, _user = _owner(request)
    with _store() as store:
        record = store.message(owner, message_id)
    if record is None:
        raise HTTPException(404, {'code': 'message_not_found', 'message': 'Message not found'})
    return JSONResponse(record, headers={'Cache-Control': 'no-store'})


def _limit_owner(request, route, owner):
    connector = getattr(request.state, 'push_connector', None)
    principal = 'connector:' + connector['id'] if connector else owner
    push_rate_limit.check(route, 'owner', principal)


def _push_policy(request):
    path = request.url.path
    suffix = path.removeprefix('/api/push').removeprefix('/v1')
    route = suffix.lstrip('/') if suffix in {'/mailbox', '/subscribe'} and request.method == 'POST' else None
    if route:
        push_rate_limit.check(route, 'ip', request.client.host if request.client else '')
    bearer = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if bearer.startswith(connector_tokens.PREFIX):
        connector = connector_tokens.authenticate(bearer)
        if connector is None:
            raise HTTPException(401, {'code': 'unauthorized', 'message': 'Invalid connector token'})
        if not route or f'{route}:write' not in connector['scopes']:
            raise HTTPException(403, {'code': 'insufficient_scope', 'message': 'Connector scope does not permit this operation'})
        request.state.push_connector = connector
        request.state.token_authenticated = True


class LegacyPushHeaders:
    """Wrap the full app so even authentication and validation errors advertise v1."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        legacy = (path == '/api/push' or path.startswith('/api/push/')) and not (
            path == '/api/push/v1' or path.startswith('/api/push/v1/'))

        async def send_response(message):
            if message['type'] == 'http.response.start' and legacy:
                successor = '/api/push/v1' + path[len('/api/push'):]
                message['headers'] = list(message.get('headers', [])) + [
                    (b'deprecation', b'true'),
                    (b'link', f'<{successor}>; rel="successor-version"'.encode())]
            await send(message)

        if scope['type'] == 'http' and (path == '/api/push' or path.startswith('/api/push/')):
            try:
                _push_policy(Request(scope))
            except HTTPException as error:
                response = JSONResponse({'detail': error.detail}, status_code=error.status_code,
                                        headers=error.headers)
                await response(scope, receive, send_response)
                return
        await self.app(scope, receive, send_response)
