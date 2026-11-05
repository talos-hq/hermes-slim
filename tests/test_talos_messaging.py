"""``hermes talos-messaging``: the key, the link, and the page you email to a phone.

The contract these tests hold:

- the key is minted once, owner-only, and is a 43-character URL-safe token;
- the install link carries it in the **fragment**, never the path or query —
  the fragment is the only part a server, its log and a proxy never see;
- ``--revoke`` removes it and the app then needs a new link;
- the emailable page contains the link and four pictures, and never a second
  copy of the key in prose;
- nothing prints the key except the link itself.
"""

from __future__ import annotations

import stat

import pytest

from hermes_cli import talos_messaging as tm


@pytest.fixture
def home(tmp_path):
    return tmp_path / "hermes"


def test_mint_is_idempotent_and_owner_only(home):
    token, created = tm.mint_token(home)
    assert created is True
    assert len(token) == tm.TOKEN_LENGTH
    assert token.replace("-", "").replace("_", "").isalnum()

    again, created_again = tm.mint_token(home)
    assert (again, created_again) == (token, False)

    mode = stat.S_IMODE(tm.token_path(home).stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_a_malformed_token_file_is_not_trusted(home):
    path = tm.token_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("short\n")
    assert tm.read_token(home) is None
    token, created = tm.mint_token(home)
    assert created is True and len(token) == tm.TOKEN_LENGTH


def test_revoke_removes_the_key(home):
    tm.mint_token(home)
    assert tm.revoke_token(home) is True
    assert tm.read_token(home) is None
    assert tm.revoke_token(home) is False


def test_the_link_puts_the_key_in_the_fragment_only():
    link = tm.install_link("https://clud.bot", "K" * 43)
    assert link == "https://clud.bot/talos/#k=" + "K" * 43
    before_fragment = link.split("#", 1)[0]
    assert "K" * 43 not in before_fragment
    assert "?" not in link


def test_the_link_normalises_a_trailing_slash():
    assert tm.install_link("https://clud.bot/", "K" * 43) == tm.install_link("https://clud.bot", "K" * 43)


def test_the_base_url_defaults_to_loopback_and_honours_an_override():
    assert tm.resolve_base_url("https://example.test") == "https://example.test"
    assert tm.resolve_base_url(None).startswith("http://")


def test_the_email_page_carries_the_link_and_four_pictures(home):
    token, _ = tm.mint_token(home)
    link = tm.install_link("https://clud.bot", token)
    path = tm.write_email(home, link)
    page = path.read_text()

    assert path.name == tm.EMAIL_FILENAME
    assert link in page
    assert page.count("<svg") == 4
    for step in ("Share button", "Add to Home Screen", "Talos Messaging", "home screen"):
        assert step in page
    # The key appears only inside the link (href + the copyable text), never in prose.
    assert page.replace(link, "").count(token) == 0


def test_print_emits_only_the_link(home, capsys):
    class Args:
        print_only = True
        email = False
        revoke = False
        url = "https://clud.bot"

    assert tm.run(Args()) == 0
    out = capsys.readouterr().out.splitlines()
    link = next(line for line in out if line.startswith("https://"))
    assert link.startswith("https://clud.bot/talos/#k=")


def test_the_default_run_explains_the_steps_without_echoing_the_key(home, capsys, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)

    class Args:
        print_only = False
        email = False
        revoke = False
        url = "https://clud.bot"

    assert tm.run(Args()) == 0
    out = capsys.readouterr().out
    token = tm.read_token(home)
    assert token is not None
    assert "Add to Home Screen" in out
    assert "Share" in out
    # The key shows up only inside the install link.
    assert out.count(token) == 1


def test_email_writes_the_page(home, capsys, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)

    class Args:
        print_only = False
        email = True
        revoke = False
        url = "https://clud.bot"

    assert tm.run(Args()) == 0
    path = home / tm.EMAIL_FILENAME
    assert path.exists()
    assert "Instructions written to" in capsys.readouterr().out


def test_revoke_through_the_command(home, capsys, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    tm.mint_token(home)

    class Args:
        print_only = False
        email = False
        revoke = True
        url = None

    assert tm.run(Args()) == 0
    assert tm.read_token(home) is None
    assert "revoked" in capsys.readouterr().out
