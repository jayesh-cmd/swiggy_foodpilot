"""
foodpilot/chat/models.py

Pydantic request/response schemas for the chat API.

WHY PYDANTIC HERE?
  FastAPI uses these models to:
  1. Validate incoming JSON automatically (wrong type = 422 before route logic runs)
  2. Generate OpenAPI docs (the /docs UI shows exact field types)
  3. Serialize responses consistently

WHY SSE INSTEAD OF REGULAR JSON RESPONSES?
  AI responses are generated token-by-token. If we waited for the ENTIRE
  response before sending anything, the user would stare at a blank screen
  for 5-15 seconds. SSE (Server-Sent Events) streams each chunk as it
  arrives — users see text appearing in real time, like Claude.ai or ChatGPT.

  SSE format:
    data: {"type": "chunk", "content": "Hello"}\n\n
    data: {"type": "chunk", "content": " world!"}\n\n
    data: {"type": "done", "conversation_id": "uuid"}\n\n
"""
from pydantic import BaseModel, Field


# ── Requests ─────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Body for POST /chat and POST /chat/{id}"""

    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="The user's message to FoodPilot AI.",
        examples=["Order biryani to my home address"],
    )


# ── Responses (non-streaming) ─────────────────────────────────────────────────


class ConversationSummary(BaseModel):
    """Single item in the GET /chat list response."""

    id: str
    title: str | None
    created_at: str
    updated_at: str
    expires_at: str


class MessageOut(BaseModel):
    """Single message in the GET /chat/{id} detail response."""

    id: str
    role: str  # "user" | "assistant"
    content: str
    created_at: str


class ConversationDetail(BaseModel):
    """Full conversation with all messages — GET /chat/{id}"""

    id: str
    title: str | None
    messages: list[MessageOut]
    created_at: str
    expires_at: str
