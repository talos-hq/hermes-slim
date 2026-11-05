"""Public v1 response shapes shared by routing and generated OpenAPI."""

from typing import Literal

from pydantic import BaseModel


class PushError(BaseModel):
    code: str
    message: str


class Registered(BaseModel):
    registered: bool


class Revoked(BaseModel):
    revoked: bool


class PublicKey(BaseModel):
    public_key: str


class MailboxReceipt(BaseModel):
    id: str
    created: bool


class Message(BaseModel):
    sequence: int
    id: str
    timestamp: str
    session_id: str
    message: str
    read: bool


class MailboxPage(BaseModel):
    messages: list[Message]
    has_more: bool
    next_cursor: int


class ReadReceipt(BaseModel):
    read: bool


class Delivery(BaseModel):
    attempted_at: str
    result: Literal['sent', 'failed']


class Revocation(BaseModel):
    at: str
    reason: Literal['404', '410']


class Counters(BaseModel):
    sent: int
    failed: int
    revoked: int


class PushStatus(BaseModel):
    subscriptions: int
    last_delivery: Delivery | None
    last_revocation: Revocation | None
    counters: Counters
