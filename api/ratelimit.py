"""Per-token fixed-window rate limiting for the GraphQL endpoint.

Each token has one counter per minute in the Django cache, so the budget is shared by every web
process and is never mixed between tokens.
"""

import logging
from math import ceil
from time import time

from django.conf import settings
from django.core.cache import cache

WINDOW_SECONDS = 60

logger = logging.getLogger(__name__)


def retry_after(token_id: int) -> int | None:
    """Count one request of a token and say whether it is over the limit.

    Args:
        token_id: Primary key of the API token making the request.

    Returns:
        None while the token is within its limit, else the whole seconds until its window ends,
        rounded up. The limiter fails open: when the cache is unavailable, or a counter is
        evicted between its two steps, a warning is logged and None is returned.
    """
    now = time()
    window = int(now // WINDOW_SECONDS)
    key = f"graphql-rate:{token_id}:{window}"
    try:
        # The key outlives its window, so only eviction can remove it between add and incr.
        cache.add(key, 0, WINDOW_SECONDS * 2)
        count = cache.incr(key)
    except Exception as error:
        logger.warning("Rate limit skipped, the cache failed: %s", type(error).__name__)
        return None
    if count <= settings.GRAPHQL_RATE_LIMIT_PER_MINUTE:
        return None
    return ceil((window + 1) * WINDOW_SECONDS - now)
