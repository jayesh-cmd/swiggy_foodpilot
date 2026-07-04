"""
foodpilot/core/retry.py

Async exponential backoff retry for transient failures.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY EXPONENTIAL BACKOFF AND NOT SIMPLE RETRY?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If a service is down and 1000 clients all retry immediately,
they create a thundering herd that makes the outage WORSE.
Exponential backoff spreads retries out:
  Attempt 1: wait 0.5s
  Attempt 2: wait 1.0s
  Attempt 3: wait 2.0s  (then give up)

Adding random jitter (±100ms) prevents synchronized spikes when
many users are retrying at the same time.

FROM SWIGGY DOCS (https://mcp.swiggy.com/builders/docs/reference/errors.md):
  "Exponential backoff with jitter. Start at 500ms, double up to 8s,
   cap at 5 retries."

We use 3 retries here (not 5) because:
  1. We have a Groq fallback — Claude failures don't need 5 retries
  2. Each retry delays the SSE stream, hurting the user experience
  3. 3 attempts with 0.5s/1s/2s covers 99% of transient blips

WHAT COUNTS AS "TRANSIENT"?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RETRY:   429 (rate limited), 500, 502, 503, 504 (infra issues)
  NO RETRY: 400 (bad input), 401 (auth), 403 (scope), 404 (not found)
  These are permanent failures — retrying won't help.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from foodpilot.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# HTTP status codes that indicate a transient failure worth retrying
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,   # seconds — matches Swiggy docs
    max_delay: float = 8.0,    # seconds — matches Swiggy docs
    retryable_codes: frozenset[int] = frozenset(TRANSIENT_HTTP_CODES),
    label: str = "operation",
) -> T:
    """
    Call fn() with exponential backoff retry.

    Args:
        fn:              Zero-argument async callable to retry.
        max_attempts:    Total attempts (including the first). Default 3.
        base_delay:      Initial wait after first failure (seconds). Default 0.5.
        max_delay:       Maximum wait between attempts. Default 8.0.
        retryable_codes: HTTP status codes that trigger retry.
                         The callable must raise an exception with a `.status_code`
                         attribute for code-based filtering (Anthropic SDK does this).
                         If the exception has no `.status_code`, it is always retried.
        label:           Human-readable name for log messages.

    Returns:
        The return value of fn() on success.

    Raises:
        The last exception if all attempts fail.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()

        except Exception as exc:
            last_exc = exc

            # Check if this error is worth retrying
            status_code = getattr(exc, "status_code", None)
            if status_code is not None and status_code not in retryable_codes:
                # Permanent error (400, 401, 403, 404) — don't retry
                logger.debug(
                    f"[retry] {label}: non-retryable status {status_code}, giving up immediately",
                )
                raise

            if attempt == max_attempts:
                logger.warning(
                    f"[retry] {label}: all {max_attempts} attempts failed",
                    extra={"error": str(exc), "attempts": max_attempts},
                )
                raise

            # Exponential backoff with jitter: 0.5, 1.0, 2.0, ... capped at 8s
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = random.uniform(0, 0.1)   # ±100ms prevents thundering herd
            wait = delay + jitter

            logger.info(
                f"[retry] {label}: attempt {attempt}/{max_attempts} failed, "
                f"retrying in {wait:.2f}s",
                extra={"error": str(exc), "status_code": status_code},
            )
            await asyncio.sleep(wait)

    # Should never reach here, but makes the type checker happy
    raise last_exc  # type: ignore[misc]
