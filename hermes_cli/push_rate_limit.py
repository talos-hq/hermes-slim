"""Process-local sliding buckets, following dashboard_auth.routes' login limiter."""

import math
import threading
from time import monotonic as _clock
from collections import OrderedDict, deque

from fastapi import HTTPException

_attempts = OrderedDict()
_lock = threading.Lock()


def _reset_rate_limit():
    with _lock:
        _attempts.clear()


def _prune_locked(now):
    for key in list(_attempts):
        bucket = _attempts[key]
        while bucket and bucket[0] <= now - key[-1]:
            bucket.popleft()
        if not bucket:
            _attempts.pop(key)


def check(route, kind, principal):
    from hermes_cli.config import load_config
    from hermes_constants import hermes_home_key
    settings = load_config()['push']['rate_limit']
    window = max(1, int(settings['window_seconds']))
    limit = max(1, int(settings[f'{route}_{kind}']))
    capacity = max(1, int(settings['max_buckets']))
    now = _clock()
    key = (hermes_home_key(), route, kind, principal or '_unknown_', window)
    with _lock:
        _prune_locked(now)
        while key not in _attempts and len(_attempts) >= capacity:
            _attempts.popitem(last=False)
        bucket = _attempts.setdefault(key, deque())
        _attempts.move_to_end(key)
        if len(bucket) >= limit:
            retry = max(1, math.ceil(bucket[0] + window - now))
            raise HTTPException(429, {'code': 'rate_limited', 'message': 'Too many requests; try again later'},
                                headers={'Retry-After': str(retry)})
        bucket.append(now)
