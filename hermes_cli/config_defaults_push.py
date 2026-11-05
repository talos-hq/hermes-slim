"""Push delivery and API budget defaults; no secrets in config.yaml."""

PUSH_CONFIG = {
    "subject": "",  # gateway/push_delivery.py; VAPID key lives in the secret store
    "rate_limit": {
        "mailbox_owner": 60,
        "mailbox_ip": 300,
        "subscribe_owner": 10,
        "subscribe_ip": 60,
        "window_seconds": 60,
        "max_buckets": 4096,
    },
}
