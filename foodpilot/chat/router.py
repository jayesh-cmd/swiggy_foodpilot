"""
foodpilot/chat/router.py

Chat API endpoints.

  POST   /chat               → new conversation + first message → SSE stream
  POST   /chat/{id}          → continue conversation → SSE stream
  GET    /chat               → list user's conversations (newest first)
  GET    /chat/{id}          → get conversation + full message history
  DELETE /chat/{id}          → delete conversation (and all its messages via CASCADE)

WHY TWO POST ENDPOINTS INSTEAD OF ONE?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POST /chat creates the conversation row AND sends the first message.
POST /chat/{id} resumes an existing conversation.

Separating them means:
  - The client gets a conversation_id from the first request
  - All subsequent messages reference that id
  - No need for the client to pre-create conversations

SSE STREAMING IN FASTAPI:
━━━━━━━━━━━━━━━━━━━━━━━━━━
StreamingResponse(generator, media_type="text/event-stream") is FastAPI's
way to stream SSE. The generator yields SSE-formatted strings, FastAPI
writes each chunk to the HTTP response body as it comes.

The client reads the stream like this (in JavaScript):
  const source = new EventSource('/chat/uuid');
  source.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.type === 'chunk') appendToUI(data.content);
    if (data.type === 'done') source.close();
  };
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from supabase import AsyncClient

from foodpilot.chat.models import ChatRequest, ConversationDetail, ConversationSummary, MessageOut
from foodpilot.chat.service import ChatService
from foodpilot.core.logging import get_logger
from foodpilot.db.models import UserRow
from foodpilot.dependencies import get_current_user, get_database

logger = get_logger(__name__)
router = APIRouter(tags=["Chat"])

# SSE response headers — required for browsers and SSE clients
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",   # Disables Nginx buffering so chunks aren't held
}


# ── 1. New conversation ───────────────────────────────────────────────────────


@router.post(
    "",
    summary="Start a new conversation",
    description=(
        "Creates a new conversation and sends the first message. "
        "Streams the AI response as Server-Sent Events. "
        "Returns conversation_id in the final 'done' event."
    ),
)
async def new_conversation(
    body: ChatRequest,
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> StreamingResponse:
    service = ChatService(db, current_user)

    # Create conversation BEFORE streaming — conversation_id is in the done event
    # Token check happens inside service.stream() before first yield
    conversation_id = await service.create_conversation(body.message)

    logger.info(
        "New conversation started",
        extra={"user_id": current_user["id"], "conversation_id": conversation_id},
    )

    return StreamingResponse(
        service.stream(conversation_id, body.message),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, "X-Conversation-ID": conversation_id},
    )


# ── 2. Continue conversation ──────────────────────────────────────────────────


@router.post(
    "/{conversation_id}",
    summary="Continue an existing conversation",
    description=(
        "Sends a message in an existing conversation. "
        "Loads the full conversation history for multi-turn context. "
        "Streams the AI response as Server-Sent Events."
    ),
)
async def continue_conversation(
    conversation_id: str,
    body: ChatRequest,
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> StreamingResponse:
    service = ChatService(db, current_user)

    # Verify the conversation exists and belongs to this user (raises 404 if not)
    await service.verify_conversation(conversation_id)

    logger.info(
        "Conversation continued",
        extra={"user_id": current_user["id"], "conversation_id": conversation_id},
    )

    return StreamingResponse(
        service.stream(conversation_id, body.message),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, "X-Conversation-ID": conversation_id},
    )


# ── 3. List conversations ─────────────────────────────────────────────────────


@router.get(
    "",
    summary="List user's conversations",
    description="Returns the user's conversations, newest first. Expired conversations are excluded.",
)
async def list_conversations(
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()

    result = (
        await db.table("conversations")
        .select("id, title, created_at, updated_at, expires_at")
        .eq("user_id", current_user["id"])
        .gt("expires_at", now_iso)           # exclude expired conversations
        .order("updated_at", desc=True)
        .execute()
    )

    conversations = [
        ConversationSummary(
            id=row["id"],
            title=row["title"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            expires_at=str(row["expires_at"]),
        ).model_dump()
        for row in result.data
    ]

    return JSONResponse(content={"conversations": conversations, "count": len(conversations)})


# ── 4. Get conversation detail ────────────────────────────────────────────────


@router.get(
    "/{conversation_id}",
    summary="Get conversation with full message history",
    description="Returns the conversation metadata and all messages in chronological order.",
)
async def get_conversation(
    conversation_id: str,
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    service = ChatService(db, current_user)
    conv = await service.verify_conversation(conversation_id)

    # Load messages
    msg_result = (
        await db.table("messages")
        .select("id, role, content, created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=False)
        .execute()
    )

    messages = [
        MessageOut(
            id=row["id"],
            role=row["role"],
            content=row["content"],
            created_at=str(row["created_at"]),
        ).model_dump()
        for row in msg_result.data
    ]

    detail = ConversationDetail(
        id=conv["id"],
        title=conv["title"],
        messages=messages,
        created_at=str(conv["created_at"]),
        expires_at=str(conv["expires_at"]),
    )

    return JSONResponse(content=detail.model_dump())


# ── 5. Delete conversation ────────────────────────────────────────────────────


@router.delete(
    "/{conversation_id}",
    summary="Delete a conversation",
    description=(
        "Deletes the conversation and all its messages. "
        "Messages are removed via CASCADE on the FK constraint."
    ),
)
async def delete_conversation(
    conversation_id: str,
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    service = ChatService(db, current_user)
    await service.verify_conversation(conversation_id)

    await db.table("conversations").delete().eq("id", conversation_id).execute()

    logger.info(
        "Conversation deleted",
        extra={"user_id": current_user["id"], "conversation_id": conversation_id},
    )

    return JSONResponse(content={"deleted": True, "conversation_id": conversation_id})
