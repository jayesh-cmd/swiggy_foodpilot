"""
foodpilot/db/client.py

Supabase async client — singleton pattern.

get_db()   → returns the shared AsyncClient (initializes on first call)
close_db() → gracefully closes the connection (called in lifespan teardown)

Uses the service-role key so the backend can bypass Supabase RLS when needed.
The anon key is reserved for client-side Supabase Auth flows.
"""
from __future__ import annotations

from supabase import AsyncClient, acreate_client

from foodpilot.config import get_settings
from foodpilot.core.logging import get_logger

logger = get_logger(__name__)

_client: AsyncClient | None = None


async def get_db() -> AsyncClient:
    """
    Return the shared Supabase AsyncClient.
    Initializes the connection on the first call; subsequent calls return the cached instance.
    """
    global _client

    if _client is None:
        settings = get_settings()
        logger.info("Initializing Supabase client", extra={"url": settings.supabase_url})

        _client = await acreate_client(
            supabase_url=settings.supabase_url,
            supabase_key=settings.supabase_service_role_key.get_secret_value(),
        )

        logger.info("Supabase client ready")

    return _client


async def close_db() -> None:
    """Close the Supabase client. Called on application shutdown."""
    global _client

    if _client is not None:
        # supabase-py AsyncClient does not have a native aclose method
        _client = None
        logger.info("Supabase client closed")
