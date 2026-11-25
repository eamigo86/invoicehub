"""Per-token fixed-window rate limiting for the GraphQL endpoint.

Each token has one counter per minute in the Django cache, so the budget is shared by every web
process and is never mixed between tokens.
"""

from time import time

from django.conf import settings
from django.core.cache import cache

WINDOW_SECONDS = 60


def retry_after(token_id: int) -> int | None:
    """Count one request of a token and say whether it is over the limit.

    Args:
        token_id: Primary key of the API token making the request.

    Returns:
        None while the token is within its limit, else the seconds until its window ends.
    """
    now = time()
    window = int(now // WINDOW_SECONDS)
    key = f"graphql-rate:{token_id}:{window}"
    # The key outlives its window, so it cannot expire between add and incr.
    cache.add(key, 0, WINDOW_SECONDS * 2)
    if cache.incr(key) <= settings.GRAPHQL_RATE_LIMIT_PER_MINUTE:
        return None
    return int((window + 1) * WINDOW_SECONDS - now) or 1
