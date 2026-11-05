"""Importing the push router must not require the optional Web Push extra.

Regression guard: the router imported ``gateway.push_delivery``, which imported ``py_vapid`` and
``pywebpush`` unconditionally. On an install without that extra the whole router -- and any app that
imports it -- died with ModuleNotFoundError at import time, rather than degrading to "mailbox works,
no wake-ups".
"""

from __future__ import annotations

import asyncio

import pytest

from gateway import push_delivery


def _no_subscriptions_store():
    class _Store:
        def subscriptions(self, owner):  # noqa: ANN001, D401 - minimal stub
            return []

        def revoke(self, owner, device_id, subscription):  # noqa: ANN001
            raise AssertionError("revoke must not be called when signing is unavailable")

    return _Store()


def test_module_exposes_the_sdk_flags():
    assert isinstance(push_delivery.PUSH_SDK_AVAILABLE, bool)
    assert isinstance(push_delivery.PUSH_SDK_MISSING, str)


def test_deliver_is_a_noop_without_the_extra(monkeypatch):
    monkeypatch.setattr(push_delivery, "PUSH_SDK_AVAILABLE", False)
    settings = push_delivery.PushSettings("private", "mailto:ops@example.com")
    # No exception, no attempt to send: the mailbox stays authoritative.
    assert asyncio.run(push_delivery.deliver(_no_subscriptions_store(), "owner", "msg-1", settings)) is None


def test_signing_raises_actionable_error_without_the_extra(monkeypatch):
    monkeypatch.setattr(push_delivery, "PUSH_SDK_AVAILABLE", False)
    monkeypatch.setattr(push_delivery, "PUSH_SDK_MISSING", "py_vapid")
    settings = push_delivery.PushSettings("private", "mailto:ops@example.com")
    with pytest.raises(push_delivery.PushSdkMissing) as excinfo:
        settings.vapid()
    # The message names the missing import so an operator can install the right extra.
    assert "py_vapid" in str(excinfo.value)


def test_deliver_still_skips_when_unconfigured(monkeypatch):
    monkeypatch.setattr(push_delivery, "PUSH_SDK_AVAILABLE", True)
    settings = push_delivery.PushSettings("", "")
    assert asyncio.run(push_delivery.deliver(_no_subscriptions_store(), "owner", "msg-2", settings)) is None
