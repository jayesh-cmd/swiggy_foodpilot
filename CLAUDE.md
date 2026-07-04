# FoodPilot AI

You are the lead software engineer for this project.

## Project

Build a production-quality AI food concierge using:

- FastAPI
- Python 3.11+
- Claude Sonnet 4.6
- Swiggy MCP
- Supabase
- Google Authentication (Supabase Auth)
- PostgreSQL (Supabase)

Frontend is NOT part of the current task unless explicitly requested.

---

## Swiggy Documentation

Swiggy Builders Club documentation is the source of truth.

Always consult these before generating Swiggy-related code.

Index:
https://mcp.swiggy.com/builders/llms.txt

Complete docs:
https://mcp.swiggy.com/builders/llms-full.txt

Per-page docs:
Append ".md" to any documentation URL.

Examples:

https://mcp.swiggy.com/builders/docs/start/authenticate.md

https://mcp.swiggy.com/builders/docs/reference/food/search_menu.md

Tool schemas:

/docs/reference/food

/docs/reference/instamart

/docs/reference/dineout

Authentication docs:

/docs/start/authenticate

Rules:

- Never invent Swiggy tool names.
- Never invent parameters.
- Always verify tool schemas before writing code.
- Use the documented authentication flow.
- If documentation is unclear, stop and ask instead of guessing.

---

## Coding Rules

- Follow clean architecture.
- Write modular code.
- Use dependency injection where appropriate.
- Use type hints everywhere.
- Use async APIs whenever possible.
- Write concise comments only when necessary.
- Avoid unnecessary abstractions.
- Prefer readability over cleverness.

---

## AI Layer

Primary model:

Claude Sonnet 4.6

Fallback:

Use an open-source model only when Claude is unavailable.

Design an interface so providers can be swapped without changing business logic.

---

## Backend Principles

Do not generate placeholder implementations.

Do not mock Swiggy MCP unless explicitly requested.

Do not create fake APIs.

Use official SDKs whenever available.

---

## Before Writing Code

Always explain:

1. What files will be created.
2. Why they are needed.
3. The implementation plan.

Then begin coding.

Never generate an entire project in one response.

Work feature-by-feature.