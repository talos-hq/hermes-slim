#!/usr/bin/env python3
"""Generate server-only VAPID material without printing the private key."""

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-key-out', type=Path,
                        help='New private key file (or set TALOS_VAPID_KEY_OUT); never overwritten')
    args = parser.parse_args(argv)
    destination = args.private_key_out or os.environ.get('TALOS_VAPID_KEY_OUT')
    if not destination:
        parser.error('require --private-key-out PATH or TALOS_VAPID_KEY_OUT')
    key = ec.generate_private_key(ec.SECP256R1())
    private = encode(key.private_numbers().private_value.to_bytes(32, 'big'))
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(private + '\n')
    except OSError:
        parser.exit(1, 'Cannot create private key file; choose a new writable path.\n')
    public = encode(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    print(f'Public VAPID key: {public}')
    print('Private key saved with mode 0600. Keep chmod 600 on this file and backups.')
    print('Load its contents into server secret TALOS_VAPID_PRIVATE_KEY; set push.subject in config.yaml.')
    print('Never commit the file or paste its contents into shell commands or browser bundles.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
