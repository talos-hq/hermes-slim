"""The Talos Messaging key authenticates one thing: the chat socket's ticket.

A phone cannot hold a dashboard credential — that is a browser's, and it opens a
shell, the files and every settings route. The messaging key is therefore its
own header, ``X-Talos-Messaging-Key``, accepted at exactly one route
(``POST /api/webapp/socket-ticket?socket=gateway``) and answering 403 for every
other socket. Revoking it stops the phone without touching a signed-in desktop.
"""

import pytest

from hermes_cli import talos_messaging
from tests.hermes_cli.test_webapp_socket_tickets import serve_webapp

TICKET_ROUTE = "/api/webapp/socket-ticket"
SESSION_TOKEN = "a" * 43


@pytest.fixture
def webapp(tmp_path, monkeypatch):
    served = serve_webapp(tmp_path, monkeypatch)
    served.home = tmp_path / ".hermes"
    served.messaging, _created = talos_messaging.mint_token(served.home)
    return served


def headers(token):
    return {"X-Talos-Messaging-Key": token}


def test_the_key_mints_a_chat_socket_ticket(webapp):
    response = webapp.client.post(f"{TICKET_ROUTE}?socket=gateway", headers=headers(webapp.messaging))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ticket"]
    assert body["ttl_seconds"] > 0
    # A phone reconnects; nothing on the way may cache the ticket.
    assert response.headers["cache-control"] == "no-store"


def test_the_key_opens_no_other_socket(webapp):
    for socket in ("host-terminal", "speak-stream", "transcribe-stream"):
        response = webapp.client.post(f"{TICKET_ROUTE}?socket={socket}", headers=headers(webapp.messaging))
        assert response.status_code == 403, (socket, response.text)


def test_a_wrong_key_is_unauthorized(webapp):
    response = webapp.client.post(f"{TICKET_ROUTE}?socket=gateway", headers=headers("b" * 43))
    assert response.status_code == 401


def test_the_key_is_not_a_dashboard_session_token(webapp):
    """It must not authenticate the dashboard itself — no shell, no files, no settings."""
    guard_route = "/api/actions"
    with_key = webapp.client.post(guard_route, headers=headers(webapp.messaging), json={})
    assert with_key.status_code != 200, with_key.text

    # The same route answers a real session token, so the refusal above is the
    # key's scope rather than a dead route.
    with_token = webapp.client.post(
        guard_route, headers={"X-Hermes-Session-Token": SESSION_TOKEN}, json={}
    )
    assert with_token.status_code != 401, with_token.text


def test_revoking_the_key_stops_the_phone(webapp):
    assert (
        webapp.client.post(f"{TICKET_ROUTE}?socket=gateway", headers=headers(webapp.messaging)).status_code
        == 200
    )
    assert talos_messaging.revoke_token(webapp.home) is True
    response = webapp.client.post(f"{TICKET_ROUTE}?socket=gateway", headers=headers(webapp.messaging))
    assert response.status_code == 401


def test_a_phone_with_no_messaging_header_is_untouched_by_this_path(webapp):
    """The ordinary browser flow still works: a session token mints a chat ticket."""
    response = webapp.client.post(
        f"{TICKET_ROUTE}?socket=gateway", headers={"X-Hermes-Session-Token": SESSION_TOKEN}
    )
    assert response.status_code == 200, response.text
    assert response.json()["ticket"]
