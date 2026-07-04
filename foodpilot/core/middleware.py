"""
foodpilot/core/middleware.py

Request-level middleware.

RequestIDMiddleware:
  Attaches a UUID to every request so logs for the same request can be
  correlated across modules (auth, chat, swiggy tool calls).

RateLimitMiddleware:
  Sliding-window per-user rate limit. Prevents a single user from flooding
  the AI endpoints and consuming expensive Claude API credits.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY IN-MEMORY RATE LIMITING (not Redis)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For a single-instance app (this project), in-memory is:
  - Zero dependencies / zero latency
  - Accurate (no race conditions across processes)
  - Sufficient for protecting Claude API costs

For multi-instance production → swap to Redis-backed (M9+ territory).

SLIDING WINDOW ALGORITHM:
  Per token, we keep a deque of request timestamps.
  On each request:
    1. Drop timestamps older than 60 seconds
    2. If count >= limit → return 429 with Retry-After header
    3. Else → append current time, allow
"""
import time
import uuid
from collections import deque
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from foodpilot.core.logging import get_logger

logger = get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Injects a unique request_id into:
    - request.state.request_id  (available to all route handlers)
    - X-Request-ID response header (visible to API consumers)
    - structured log entries for the request lifecycle
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        path = request.url.path

        if path != "/auth/swiggy/health":
            logger.info(
                "Request started",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                },
            )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        if path != "/auth/swiggy/health":
            logger.info(
                "Request completed",
                extra={
                    "request_id": request_id,
                    "status_code": response.status_code,
                },
            )

        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter per authenticated session.

    Keyed on the first 16 characters of the Bearer token (never the full token).
    Excluded paths: /health, /docs, /openapi.json, /redoc.

    On limit exceeded → HTTP 429 + Retry-After header.
    """

    _EXCLUDED_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")

    def __init__(self, app, requests_per_minute: int = 60) -> None:
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_seconds = 60
        self._buckets: dict[str, deque] = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Skip rate limiting for monitoring and docs endpoints
        if any(path.startswith(p) for p in self._EXCLUDED_PREFIXES):
            return await call_next(request)

        # Only rate-limit authenticated requests
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return await call_next(request)

        # Key = first 16 chars of token (not full token — never log full tokens)
        token_key = auth_header[7:23]
        now = time.monotonic()
        cutoff = now - self.window_seconds

        if token_key not in self._buckets:
            self._buckets[token_key] = deque()
        bucket = self._buckets[token_key]

        # Evict timestamps outside the window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.requests_per_minute:
            retry_after = int(self.window_seconds - (now - bucket[0])) + 1
            logger.warning(
                "Rate limit exceeded",
                extra={
                    "path": path,
                    "requests_in_window": len(bucket),
                    "limit": self.requests_per_minute,
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": (
                        f"Too many requests ({len(bucket)} in 60s). "
                        f"Limit: {self.requests_per_minute}/min. "
                        f"Retry in {retry_after}s."
                    ),
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        return await call_next(request)

    def clear_stale_buckets(self) -> int:
        """Remove buckets with no activity in the last window. Returns count removed."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        stale = [k for k, b in self._buckets.items() if not b or b[-1] < cutoff]
        for k in stale:
            del self._buckets[k]
        return len(stale)

