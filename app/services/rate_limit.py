from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request


_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def _client_id(request: Request) -> str:
    return request.headers.get("Fly-Client-IP") or (request.client.host if request.client else "unknown")


def check_rate_limit(request: Request, action: str, identity: str, limit: int, window_seconds: int) -> None:
    key = f"{action}:{_client_id(request)}:{identity.casefold()}"
    now = monotonic()
    cutoff = now - window_seconds
    with _lock:
        if len(_attempts) > 10_000:
            stale_keys = [candidate for candidate, values in _attempts.items() if not values or values[-1] < now - 3600]
            for stale_key in stale_keys:
                _attempts.pop(stale_key, None)
        bucket = _attempts[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="Too many attempts. Please try again later.")
        bucket.append(now)


def clear_rate_limit(request: Request, action: str, identity: str) -> None:
    key = f"{action}:{_client_id(request)}:{identity.casefold()}"
    with _lock:
        _attempts.pop(key, None)
