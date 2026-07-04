-- Migration 002: swiggy_tokens table
-- Applied in: M3 (Swiggy OAuth)
-- Depends on: 001_users.sql

CREATE TABLE IF NOT EXISTS swiggy_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    access_token TEXT NOT NULL,          -- Fernet-encrypted at rest; never store plaintext
    expires_at   TIMESTAMPTZ NOT NULL,   -- NOW() + 432000s (5 days, per Swiggy OAuth docs)
    scope        TEXT,                   -- "mcp:tools mcp:resources mcp:prompts"
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One active Swiggy session per user (UPSERT replaces on re-auth)
    CONSTRAINT uq_swiggy_tokens_user UNIQUE (user_id)
);

-- Fast lookup by user for every chat request
CREATE INDEX IF NOT EXISTS idx_swiggy_tokens_user_id ON swiggy_tokens (user_id);

-- Partial index to quickly find tokens that are near expiry or already expired
CREATE INDEX IF NOT EXISTS idx_swiggy_tokens_expires_at
    ON swiggy_tokens (expires_at)
    WHERE expires_at < NOW() + INTERVAL '60 seconds';
