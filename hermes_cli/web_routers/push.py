"""Authenticated device enrollment and the authoritative Talos mailbox."""

import base64
import json
from contextlib import contextmanager
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.push_delivery import PushSettings, deliver
from gateway.push_store import PushStore
from hermes_cli.web_routers.webapp import _ticket_principal

router = APIRouter(prefix='/api/push')


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
    except (ValueError, TypeError):
        raise HTTPException(503, 'Push is not configured') from None
    return JSONResponse({'public_key': key}, headers={'Cache-Control': 'no-store'})


@router.post('/mailbox')
async def write_mailbox(request: Request, body: MailboxMessage):
    owner, user = _owner(request)
    if body.user_id is not None and body.user_id != user:
        raise HTTPException(403, 'Mailbox recipient must match the authenticated user')
    with _store() as store:
        try:
            admitted = store.admit(owner, body.message_id, body.session_id, body.message)
        except ValueError:
            raise HTTPException(409, 'Message id already used for different content') from None
        if admitted:
            # Delivery failure cannot roll back or invalidate the committed mailbox receipt.
            try:
                await deliver(store, owner, body.message_id, PushSettings.load())
            except (ValueError, TypeError):
                pass  # Invalid server key: history remains available; never echo the key/error.
    return {'id': body.message_id, 'created': admitted}


@router.get('/mailbox')
def mailbox(request: Request, after: int = Query(default=0, ge=0)):
    owner, _user = _owner(request)
    with _store() as store:
        messages = store.mailbox(owner, after)
    return JSONResponse({'messages': messages, 'next_cursor': messages[-1]['sequence'] if messages else after},
                        headers={'Cache-Control': 'no-store'})


@router.patch('/mailbox/{message_id}/read')
def mark_read(request: Request, message_id: str):
    owner, _user = _owner(request)
    with _store() as store:
        found = store.mark_read(owner, message_id)
    if not found:
        raise HTTPException(404, 'Message not found')
    return {'read': True}
