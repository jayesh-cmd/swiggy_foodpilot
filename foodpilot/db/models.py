"""
foodpilot/db/models.py

TypedDict shapes that mirror Supabase table rows.
Used for type hints on query results — not ORM models.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, TypedDict


class UserRow(TypedDict):
    id: str                    # UUID (primary key)
    supabase_id: str           # UUID from Supabase Auth
    email: str
    name: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime


class SwiggyTokenRow(TypedDict):
    id: str                    # UUID (primary key)
    user_id: str               # FK → users.id
    access_token: str          # Fernet-encrypted Swiggy Bearer token
    expires_at: datetime       # now() + 432000s (5 days, per Swiggy docs)
    scope: Optional[str]
    created_at: datetime


class ConversationRow(TypedDict):
    id: str                    # UUID (primary key)
    user_id: str               # FK → users.id
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime       # now() + 48h TTL; pg_cron purges after this


class MessageRow(TypedDict):
    id: str                    # UUID (primary key)
    conversation_id: str       # FK → conversations.id
    role: str                  # "user" | "assistant"
    content: str
    tool_use: Optional[dict[str, Any]]  # raw tool_use blocks (for conversation replay)
    created_at: datetime
