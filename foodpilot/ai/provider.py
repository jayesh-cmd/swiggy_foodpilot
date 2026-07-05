"""
foodpilot/ai/provider.py

The AIProvider Protocol — the contract every AI provider must follow.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS A PROTOCOL AND WHY USE ONE?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A Protocol is Python's way of saying: "I don't care WHAT class you are,
I just care that you have these specific methods."

Example in real life:
  You don't care which brand of USB charger you use — you just need
  it to fit the port and deliver power. The port is the Protocol.
  Claude and OpenAI are different charger brands.

Without a Protocol:
  chat/service.py would be full of:
    if provider == "claude": ...
    elif provider == "openai": ...
  Every new provider = edit every file that uses AI.

With a Protocol:
  chat/service.py calls provider.chat(...) and has zero knowledge of
  which provider it's talking to. Swap Claude for OpenAI by changing
  one line in the DI configuration. Nothing else changes.

WHY AsyncIterator[str] AS RETURN TYPE?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AI responses are streamed token by token — you don't want to wait
for the ENTIRE response before showing anything to the user.

AsyncIterator[str] means the caller does:
    async for chunk in provider.chat(...):
        send_to_client(chunk)   # user sees text appearing word by word

This is how ChatGPT, Claude.ai, and every modern AI interface works.
"""
from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class AIProvider(Protocol):
    """
    Interface that every AI provider implementation must satisfy.

    Any class with a matching `chat` method signature is automatically
    a valid AIProvider — no inheritance needed (structural subtyping).

    `@runtime_checkable` allows isinstance(provider, AIProvider) checks
    at runtime, which is useful for validation in tests and DI.
    """

    async def chat(
        self,
        messages: list[dict],
        system: str,
        mcp_servers: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        """
        Send a conversation to the AI and stream the response.

        Args:
            messages:    Full conversation history in OpenAI/Anthropic format:
                         [{"role": "user", "content": "..."}, ...]
            system:      The system prompt — persona, instructions, constraints.
            mcp_servers: List of MCP server configs (Swiggy Food + Instamart).
                         Claude uses these to call Swiggy tools automatically.

        Yields:
            str chunks of the response as they are generated.
        """
        ...  # Protocol body — never executed, just defines the signature
