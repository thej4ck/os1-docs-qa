"""In-memory sliding-window rate limiter (single-process).

Generalized from the per-email limiter in chat_routes. Keyed by an arbitrary
bucket string so callers scope it (e.g. "ask:<email>", "mcp:<ip>"). Suitable
for the single-process Railway/uvicorn deploy; a multi-process setup would need
a shared store (Redis).
"""

import time
from collections import defaultdict

_hits: dict[str, list[float]] = defaultdict(list)


def allow(key: str, max_hits: int, window_sec: float) -> bool:
    """Return True if the call is within budget for `key`, else False.

    Records the call when allowed (so it counts toward the window)."""
    now = time.time()
    bucket = [t for t in _hits[key] if now - t < window_sec]
    if len(bucket) >= max_hits:
        _hits[key] = bucket
        return False
    bucket.append(now)
    _hits[key] = bucket
    return True
