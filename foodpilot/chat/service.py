"""
foodpilot/chat/service.py

The core chat service — the brain of FoodPilot AI.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT HAPPENS ON EVERY /chat REQUEST (in order):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Create or verify the conversation row in DB
2. Save the user's message to the messages table
3. Decrypt the user's Swiggy token from swiggy_tokens
   → Raises SwiggyNotLinkedError if no token → 403 before stream starts
   → Raises SwiggyTokenExpiredError if expired → 401 before stream starts
4. Build mcp_servers list (Food + Instamart with the user's token)
5. Load full conversation history from DB (for multi-turn context)
6. Build the system prompt (personalised with user's name)
7. Call ClaudeProvider.chat(messages, system, mcp_servers)
   → Claude autonomously calls Swiggy tools (get_addresses, search_restaurants…)
   → 401/403: Stop AI, emit special warning event so UI prompts reconnect
   → If Claude fails → yield SSE error natively without crashing the app
8. Accumulate the full response text
9. Save the assistant response to the messages table
10. Yield the final "done" SSE event with conversation_id

WHY YIELD FROM INSIDE THE SERVICE (not the router)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The router's job is routing. The service's job is business logic.
Keeping the streaming generator here means the router just does:
    return StreamingResponse(service.stream(...), media_type="text/event-stream")
The router has zero knowledge of Claude, Swiggy, or DB.

WHY IS SWIGGY TOKEN CHECKED BEFORE STREAM STARTS?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Once StreamingResponse begins, HTTP headers are already sent (status 200).
We can't change them to 401/403 mid-stream. So we decrypt the token
before yielding a single byte. If it fails, FastAPI sends the proper
error response instead of a corrupted stream.
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

from supabase import AsyncClient

from foodpilot.ai.claude import ClaudeProvider
from foodpilot.ai.prompt import build_system_prompt
from foodpilot.config import get_settings
from foodpilot.core.errors import AIProviderError
from foodpilot.core.logging import get_logger
from foodpilot.db.models import UserRow
from foodpilot.swiggy.client import build_mcp_servers
from foodpilot.swiggy.token_store import get_decrypted_token, get_token_expiry_status

logger = get_logger(__name__)

# Singleton providers — instantiated once, reused across all requests
# They hold no per-request state so this is safe
_claude = ClaudeProvider()


class ChatService:
    """
    Encapsulates all business logic for a single chat request.

    Instantiated once per request (via DI in the router).
    Holds the DB client and authenticated user for the lifetime of the request.
    """

    def __init__(self, db: AsyncClient, user: UserRow) -> None:
        self.db = db
        self.user = user

    # ── Conversation management ────────────────────────────────────────────────

    async def create_conversation(self, first_message: str) -> str:
        """
        Create a new conversation row with explicit expires_at.

        WHY SET expires_at EXPLICITLY (not rely on DB default)?
          The DB default is correct, but setting it explicitly:
          1. Lets us use conversation_ttl_hours from config (tunable without a migration)
          2. Makes the value visible in application logs
          3. Ensures tests can mock time without fighting DB-level NOW()

        Title = first 60 chars of the first message (shown in conversation list UI).
        """
        settings = get_settings()
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(hours=settings.conversation_ttl_hours)
        )
        title = first_message[:60].strip() + ("…" if len(first_message) > 60 else "")
        result = (
            await self.db.table("conversations")
            .insert({
                "user_id": self.user["id"],
                "title": title,
                "expires_at": expires_at.isoformat(),
            })
            .execute()
        )
        conv_id = result.data[0]["id"]
        logger.info(
            "Conversation created",
            extra={
                "conversation_id": conv_id,
                "expires_at": expires_at.isoformat(),
                "ttl_hours": settings.conversation_ttl_hours,
            },
        )
        return conv_id

    async def verify_conversation(self, conversation_id: str) -> dict:
        """
        Verify the conversation exists and belongs to the current user.
        Raises ConversationNotFoundError if not found or expired.
        """
        from foodpilot.core.errors import ConversationNotFoundError

        result = (
            await self.db.table("conversations")
            .select("*")
            .eq("id", conversation_id)
            .eq("user_id", self.user["id"])
            .maybe_single()
            .execute()
        )
        if result.data is None:
            raise ConversationNotFoundError(conversation_id)

        # Check TTL — expires_at is set to NOW() + 48 hours on creation
        conv = result.data
        expires_at_str = conv["expires_at"]
        if isinstance(expires_at_str, str):
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        else:
            expires_at = expires_at_str

        if datetime.now(timezone.utc) >= expires_at:
            raise ConversationNotFoundError(conversation_id)

        return conv

    async def _save_message(self, conversation_id: str, role: str, content: str) -> None:
        """Insert a message row and bump conversation.updated_at."""
        await self.db.table("messages").insert({
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
        }).execute()

        # Touch updated_at so the conversation appears at the top of the list
        await self.db.table("conversations").update({
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", conversation_id).execute()

    async def _load_history(self, conversation_id: str) -> list[dict]:
        """
        Load conversation messages formatted for Claude — with smart context windowing.

        CONTEXT WINDOW STRATEGY (M6):
        ────────────────────────────────
        Problem: A 48-hour conversation could have 200+ messages. Loading all of
        them into every API call is wasteful (costs tokens) and eventually hits
        Claude's context window.

        Solution: "Anchor + Recent" strategy
          1. Always include the first 2 messages (establish user intent, first address choice)
          2. If total messages > max_conversation_history:
             - Insert a [CONTEXT TRUNCATED] marker so Claude knows history was cut
             - Keep only the most recent (max_history - 2) messages after the anchor
          3. If total messages <= max_conversation_history: keep everything

        WHY KEEP THE FIRST 2 MESSAGES (the anchor)?
          The user's very first message often sets key context:
          e.g. "I'm vegetarian, help me order dinner" or "always use my home address"
          Losing this would make Claude forget user preferences mid-conversation.

        WHY NOT TOKEN COUNTING?
          Token counting requires an extra API call or a library (tiktoken).
          For 48h TTL conversations at 50-message limit, we're at ~10k tokens max
          which is well within Claude's 200k window. Token counting is M7+ territory.
        """
        settings = get_settings()
        max_history = settings.max_conversation_history

        result = (
            await self.db.table("messages")
            .select("role, content")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .execute()
        )
        all_messages = [{"role": r["role"], "content": r["content"]} for r in result.data]

        if len(all_messages) <= max_history:
            return all_messages

        # Truncation: anchor (first 2) + marker + recent (last max_history-2)
        anchor = all_messages[:2]
        recent_count = max_history - 2
        recent = all_messages[-recent_count:]
        skipped = len(all_messages) - 2 - recent_count

        truncation_marker = {
            "role": "user",
            "content": (
                f"[CONTEXT TRUNCATED: {skipped} earlier messages were removed to fit the context window. "
                "The conversation above shows the beginning and the most recent exchanges.]"
            ),
        }

        logger.info(
            "History truncated for context window",
            extra={
                "conversation_id": conversation_id,
                "total": len(all_messages),
                "kept": max_history,
                "skipped": skipped,
            },
        )
        return anchor + [truncation_marker] + recent

    # ── Core streaming method ──────────────────────────────────────────────────

    async def stream(
        self,
        conversation_id: str,
        user_message: str,
    ) -> AsyncGenerator[str, None]:
        """
        Main entry point — called by the router to get an SSE stream.

        This method is an async generator. Each `yield` sends an SSE event
        to the client immediately. The router wraps this in StreamingResponse.

        SSE format used:
          data: {"type": "chunk", "content": "..."}\n\n   ← text chunks
          data: {"type": "done",  "conversation_id": "..."}\n\n  ← final event
          data: {"type": "error", "message": "..."}\n\n   ← on failure

        Step 1-2: Token check (MUST happen before first yield — see module docstring)
        Step 3+:  DB save, history load, AI call, SSE emit, DB save response
        """
        # ── STEP 1: Decrypt Swiggy token BEFORE starting stream ───────────────
        # get_decrypted_token raises SwiggyNotLinkedError or SwiggyTokenExpiredError
        # These propagate to the router's exception handlers → 401/403 response
        # (No SSE has been sent yet, so proper HTTP error codes can be used)
        swiggy_token = await get_decrypted_token(self.db, self.user["id"])

        # ── STEP 1b: Proactive expiry warning (M7) ────────────────────────────
        # After get_decrypted_token succeeds (token is valid), check if it's
        # expiring SOON (< 24h). If so, emit a non-fatal 'warning' SSE event
        # so the frontend can show "Reconnect Swiggy before it expires" banner.
        #
        # WHY AFTER THE FIRST yield (i.e., inside the stream), NOT before?
        # Because:
        #   a) HTTP headers are already committed at this point (status 200, SSE)
        #   b) This is informational — the token is VALID, order can proceed
        #   c) The user can ignore the warning and still place their order
        #
        # HOWEVER — we check expiry_status but only yield after the first
        # successful decrypt. get_decrypted_token has already done the hard check.
        # get_token_expiry_status is a cheaper, non-raising re-check for warning data.
        expiry_status = await get_token_expiry_status(self.db, self.user["id"])
        if expiry_status["expiring_soon"]:
            hours_left = expiry_status["hours_remaining"]
            yield _sse_event("warning", {
                "code": "SWIGGY_TOKEN_EXPIRING_SOON",
                "message": (
                    f"Your Swiggy session expires in {hours_left:.0f}h. "
                    f"Visit /auth/swiggy/connect to reconnect before it expires."
                ),
                "hours_remaining": hours_left,
                "reconnect_url": "/auth/swiggy/connect",
            })

        # ── STEP 2: Save the user's message ───────────────────────────────────
        await self._save_message(conversation_id, "user", user_message)

        # ── STEP 3: Load full conversation history ─────────────────────────────
        # Includes the message we just saved — history ends with the user turn
        history = await self._load_history(conversation_id)

        # ── STEP 4: Build MCP servers config (with decrypted token) ───────────
        mcp_servers = build_mcp_servers(swiggy_token)

        # ── STEP 5: Build system prompt (personalised with user name) ──────────
        system = build_system_prompt(user_name=self.user.get("name"))

        # ── STEP 6: Call AI and stream response ────────────────────────────────
        accumulated_chunks: list[str] = []

        try:
            # Primary: Claude Sonnet 4.6 with Swiggy MCP
            logger.info(
                "Starting Claude chat",
                extra={"user_id": self.user["id"], "conversation_id": conversation_id},
            )
            async for chunk in _claude.chat(history, system, mcp_servers):
                accumulated_chunks.append(chunk)
                yield _sse_chunk(chunk)

        except AIProviderError as claude_err:
            logger.error(
                "Claude failed",
                extra={"error": str(claude_err), "conversation_id": conversation_id},
            )
            # Yield error event natively, don't crash the stream
            yield _sse_event("error", {
                "message": "AI service is currently unavailable. Please try again later."
            })
            return  # Don't save an empty response

        # ── STEP 7: Save the assistant's full response ─────────────────────────
        full_response = "".join(accumulated_chunks)
        if full_response.strip():
            await self._save_message(conversation_id, "assistant", full_response)
            logger.info(
                "Response saved",
                extra={
                    "conversation_id": conversation_id,
                    "response_length": len(full_response),
                },
            )

        # ── STEP 8: Signal completion ──────────────────────────────────────────
        yield _sse_event("done", {"conversation_id": conversation_id})


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse_chunk(content: str) -> str:
    """Format a text chunk as an SSE data event."""
    return f"data: {json.dumps({'type': 'chunk', 'content': content})}\n\n"


def _sse_event(event_type: str, payload: dict) -> str:
    """Format a typed SSE event (done, error, fallback)."""
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"
