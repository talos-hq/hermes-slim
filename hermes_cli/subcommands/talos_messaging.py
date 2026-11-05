"""``hermes talos-messaging`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_talos_messaging_parser(subparsers, *, cmd: Callable) -> None:
    """Attach ``talos-messaging`` (alias ``messaging``) to *subparsers*."""
    parser = subparsers.add_parser(
        "talos-messaging",
        aliases=["messaging"],
        help="Put Talos Messaging on a phone: mint the install key, print the link, "
             "write the Add-to-Home-Screen instructions",
        description="Talos Messaging is the phone web app for this installation's agents. "
        "This command mints or reuses its key (webapp/messaging-token), prints the install "
        "link that carries it, and can write a self-contained instructions page with "
        "pictures to send to the phone.")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "connector-token"], default=None,
        help="`serve`: carry the phone's chat through the network from this machine "
             "(dials the public relay as the agent)",
    )
    parser.add_argument(
        "--relay", default=None,
        help="Relay base URL for `serve` (default: the address published at "
             "talosbot.ai/talos-messaging/signal.json)")
    parser.add_argument(
        "--gateway", default=None,
        help="Local gateway base URL for `serve` (default: the Webapp's bind)")
    parser.add_argument(
        "--print", action="store_true", dest="print_only",
        help="Print only the install link, for a script or a QR code")
    parser.add_argument(
        "--email", action="store_true",
        help="Also write ~/talos-messaging-setup.html: the link plus the "
             "Add-to-Home-Screen steps with pictures, to send to the phone")
    parser.add_argument(
        "--revoke", action="store_true",
        help="Delete the key; the phone's app will ask for a new link")
    parser.add_argument(
        "--url", default=None,
        help="Base URL the app is served at (default: webapp.public_url, else "
             "http://127.0.0.1:9120)")
    parser.add_argument('connector_action', nargs='?', choices=['create', 'revoke', 'list'])
    parser.add_argument('--name', default='connector', help='Connector label')
    parser.add_argument('--scope', action='append', choices=['mailbox:write', 'subscribe:write'])
    parser.add_argument('--token-id', help='Token id to revoke')
    parser.set_defaults(func=cmd)
