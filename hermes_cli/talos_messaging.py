"""``hermes talos-messaging`` — put Talos Messaging on a phone.

Talos Messaging is the phone PWA in this repository (``apps/talos-messaging``):
a Telegram-shaped chat with this installation's agents, added to the home
screen. This command is the only thing standing between "the app exists" and
"the app is on my phone":

1. **mint or reuse the key** — ``webapp/messaging-token``, owner-only, 43
   URL-safe characters, the same shape as the Webapp session token, but its own
   file so revoking it never signs a desktop out;
2. **build the install link** — ``<base>/talos/#k=<token>``. The key rides in the
   **fragment**, so no server, access log or proxy ever sees it;
3. **say what to do with it**, and with ``--email`` write one self-contained
   HTML page — the link plus the Add-to-Home-Screen steps with pictures — to
   send to the phone. That page is deliberately separate from the app: you read
   it *before* the app has ever opened.

The key is printed only by ``--print`` (or in the install link it builds).
Nothing here writes it to a log, and the email carries the link, not a copy of
the key in prose.
"""

from __future__ import annotations

import html
import os
import secrets
import stat
from pathlib import Path

from hermes_cli.cli_output import print_info, print_success

#: Where the key lives, relative to the hermes home.
TOKEN_PATH = Path("webapp") / "messaging-token"
#: The gateway's default Webapp bind (gateway/webapp_listener.py).
DEFAULT_BASE_URL = "http://127.0.0.1:9120"
#: The path the PWA is served from (apps/talos-messaging, base: "/talos/").
APP_PATH = "/talos/"
#: secrets.token_urlsafe(32) is 43 characters; refuse anything else on read.
TOKEN_LENGTH = 43
EMAIL_FILENAME = "talos-messaging-setup.html"


def token_path(home: Path) -> Path:
    return Path(home) / TOKEN_PATH


def read_token(home: Path) -> str | None:
    """The stored key, or None when there is none or it is malformed."""
    path = token_path(home)
    try:
        value = path.read_text().strip()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    if len(value) != TOKEN_LENGTH or not all(char.isalnum() or char in "-_" for char in value):
        return None
    return value


def mint_token(home: Path) -> tuple[str, bool]:
    """Return the key, minting it once. ``(token, created)``."""
    existing = read_token(home)
    if existing is not None:
        return existing, False
    token = secrets.token_urlsafe(32)
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 in one step: a token written then chmodded is readable
    # for as long as the gap lasts.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return token, True


def revoke_token(home: Path) -> bool:
    """Delete the key. Returns whether one existed."""
    path = token_path(home)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def install_link(base_url: str, token: str) -> str:
    """``<base>/talos/#k=<token>`` — the key in the fragment, never the query."""
    base = base_url.rstrip("/")
    return f"{base}{APP_PATH}#k={token}"


def resolve_base_url(explicit: str | None = None) -> str:
    """``--url``, else the Webapp's public URL, else the loopback default."""
    if explicit:
        return explicit
    try:
        from hermes_cli.config import load_config

        webapp = (load_config() or {}).get("webapp") or {}
        configured = str(webapp.get("public_url") or "").strip()
        if configured:
            return configured
        host = str(webapp.get("host") or "").strip()
        port = webapp.get("port")
        if host in {"127.0.0.1", "::1", "localhost"} and port:
            return f"http://{host}:{port}"
    except Exception:  # a broken config must not stop the command
        pass
    return DEFAULT_BASE_URL


# ── the instructions page ────────────────────────────────────────────────────

def _share_glyph() -> str:
    return """
<svg viewBox="0 0 120 120" role="img" aria-label="Safari share button">
  <rect x="6" y="18" width="108" height="94" rx="16" fill="none" stroke="#C89B63" stroke-width="3"/>
  <path d="M60 12v46" stroke="#E4C89A" stroke-width="4" stroke-linecap="round"/>
  <path d="M44 26l16-14 16 14" fill="none" stroke="#E4C89A" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M34 62l-8 10v22h68V72l-8-10" fill="none" stroke="#C89B63" stroke-width="3" stroke-linecap="round"/>
</svg>"""


def _add_to_home() -> str:
    return """
<svg viewBox="0 0 210 150" role="img" aria-label="Share sheet with Add to Home Screen highlighted">
  <rect x="4" y="4" width="202" height="142" rx="18" fill="#10141a" stroke="#514435" stroke-width="2"/>
  <rect x="16" y="96" width="178" height="38" rx="12" fill="#211c16" stroke="#E4C89A" stroke-width="2"/>
  <rect x="30" y="106" width="18" height="18" rx="4" fill="none" stroke="#E4C89A" stroke-width="2"/>
  <path d="M36 115h6M39 112v6" stroke="#E4C89A" stroke-width="2" stroke-linecap="round"/>
  <text x="58" y="120" font-family="system-ui" font-size="13" fill="#EDE6D9">Add to Home Screen</text>
  <rect x="16" y="18" width="178" height="26" rx="8" fill="#16191e" stroke="#33373d" stroke-width="1.5"/>
  <rect x="16" y="52" width="178" height="26" rx="8" fill="#16191e" stroke="#33373d" stroke-width="1.5"/>
</svg>"""


def _confirm_dialog() -> str:
    return """
<svg viewBox="0 0 210 130" role="img" aria-label="Name field with Talos Messaging and the Add button">
  <rect x="4" y="4" width="202" height="122" rx="18" fill="#10141a" stroke="#514435" stroke-width="2"/>
  <text x="18" y="30" font-family="system-ui" font-size="12" fill="#6B6F75">Add to Home Screen</text>
  <rect x="18" y="42" width="174" height="30" rx="8" fill="#16191e" stroke="#E4C89A" stroke-width="2"/>
  <text x="30" y="62" font-family="system-ui" font-size="13" fill="#EDE6D9">Talos Messaging</text>
  <rect x="130" y="86" width="62" height="28" rx="14" fill="#C89B63"/>
  <text x="147" y="105" font-family="system-ui" font-size="13" fill="#0B0D10">Add</text>
</svg>"""


def _home_icon() -> str:
    return """
<svg viewBox="0 0 210 130" role="img" aria-label="The Talos Messaging icon on the home screen">
  <rect x="4" y="4" width="202" height="122" rx="18" fill="#0B0D10" stroke="#514435" stroke-width="2"/>
  <rect x="28" y="26" width="52" height="52" rx="13" fill="#10141a" stroke="#C89B63" stroke-width="2"/>
  <path d="M44 40h20v7h-6.5v18h-7V47H44z" fill="#E4C89A"/>
  <rect x="98" y="26" width="52" height="52" rx="13" fill="#14181e" stroke="#33373d" stroke-width="1.5"/>
  <rect x="28" y="90" width="52" height="12" rx="4" fill="#232a31"/>
  <text x="28" y="118" font-family="system-ui" font-size="10" fill="#6B6F75">Talos</text>
</svg>"""


STEPS: tuple[tuple[str, str], ...] = (
    ("Open the link on the iPhone", "Safari opens Talos Messaging. It will look like a web page for now."),
    ("Tap the Share button", "The square with the arrow, in the toolbar at the bottom."),
    ("Choose Add to Home Screen", "Scroll the share sheet until you see it."),
    ("Tap Add", "The name is already Talos Messaging. It now opens like an app, with no address bar."),
)


def instructions_html(link: str) -> str:
    """The emailable page: the link, then the four pictures and their steps."""
    escaped = html.escape(link, quote=True)
    pictures = (_share_glyph(), _add_to_home(), _confirm_dialog(), _home_icon())
    steps = "\n".join(
        f"""
      <li>
        <h3>{index}. {html.escape(title)}</h3>
        <figure>{picture}<figcaption>{html.escape(body)}</figcaption></figure>
      </li>"""
        for index, ((title, body), picture) in enumerate(zip(STEPS, pictures, strict=True), start=1)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add Talos Messaging to your iPhone</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 28px 20px 56px; background: #0B0D10; color: #EDE6D9;
         font: 17px/1.55 -apple-system, system-ui, sans-serif; }}
  h1 {{ font-size: 30px; line-height: 1.15; margin: 0 0 6px; }}
  h2 {{ font-size: 20px; margin: 34px 0 10px; color: #E4C89A; }}
  p {{ color: #c6c4c0; }}
  .eyebrow {{ letter-spacing: 2px; text-transform: uppercase; font-size: 12px; color: #C89B63; margin: 0 0 14px; }}
  a.install {{ display: block; margin: 18px 0; padding: 16px; border-radius: 10px;
               border: 1px solid #C89B63; color: #E4C89A; word-break: break-all; text-decoration: none;
               font-size: 15px; }}
  ol {{ list-style: none; padding: 0; margin: 0; }}
  li {{ margin: 0 0 26px; padding: 16px; border: 1px solid #514435; border-radius: 12px; background: #10141a; }}
  h3 {{ font-size: 17px; margin: 0 0 12px; }}
  figure {{ margin: 0; }}
  figure svg {{ width: 100%; max-width: 260px; height: auto; display: block; margin: 0 auto 10px; }}
  figcaption {{ color: #c6c4c0; font-size: 15px; }}
  code {{ background: #16191e; border-radius: 5px; padding: 2px 6px; }}
  .note {{ border-left: 2px solid #C89B63; padding-left: 14px; color: #b5b7ba; font-size: 15px; }}
</style>
</head>
<body>
  <p class="eyebrow">Talos Messaging</p>
  <h1>Chat with your agents from your iPhone</h1>
  <p>Open this link <strong>on the iPhone</strong> — it signs you in and needs no password:</p>
  <a class="install" href="{escaped}">{escaped}</a>
  <p class="note">The link is the key. Anyone who has it can talk to your agents, so send it to
  yourself, not to a group chat. If it leaks, run <code>hermes talos-messaging --revoke</code> and
  send yourself a new one.</p>
  <h2>Add it to the home screen</h2>
  <ol>{steps}
  </ol>
  <p class="note">Once it is on the home screen it keeps its own history on the phone, so switching
  apps or losing signal does not lose the conversation.</p>
</body>
</html>
"""


def write_email(home: Path, link: str) -> Path:
    """Write the emailable instructions page and return its path."""
    path = Path(home) / EMAIL_FILENAME
    path.write_text(instructions_html(link), encoding="utf-8")
    return path


def run(args) -> int:
    """``hermes talos-messaging``: mint, print, email, or revoke."""
    from hermes_cli.config import get_hermes_home

    home = get_hermes_home()
    if getattr(args, "revoke", False):
        if revoke_token(home):
            print_success("Talos Messaging key revoked. The app on the phone will ask for a new link.")
        else:
            print_info("No Talos Messaging key was stored; nothing to revoke.")
        return 0

    token, created = mint_token(home)
    link = install_link(resolve_base_url(getattr(args, "url", None)), token)

    if getattr(args, "print_only", False):
        # A script or a QR code wants the link and nothing else on stdout.
        print(link)
        return 0

    if created:
        print_success(f"Talos Messaging key created at {token_path(home)}")
    else:
        print_info(f"Talos Messaging key: {token_path(home)}")

    print()
    print_info("Install link (open it on the iPhone):")
    print(link)
    print()
    print_info("Steps: open the link on the phone, tap Share, choose Add to Home Screen, tap Add.")
    print_info("The link stays signed in; it is the key, so keep the email to yourself.")

    if getattr(args, "email", False):
        path = write_email(home, link)
        print()
        print_success(f"Instructions written to {path}")
        print_info("Attach or send that file to the iPhone, then open it there and follow the pictures.")
    else:
        print_info("Add --email to write an instructions page with pictures to send to the phone.")

    return 0
