-- Migration 003: conversations + messages tables
-- Applied in: M6 (Conversation Persistence)
-- Depends on: 001_users.sql

CREATE TABLE IF NOT EXISTS conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '48 hours')  -- 2-day TTL
);

-- List conversations for a user, newest first
CREATE INDEX IF NOT EXISTS idx_conversations_user_id
    ON conversations (user_id, updated_at DESC);

-- Used by the pg_cron purge job to find expired rows
CREATE INDEX IF NOT EXISTS idx_conversations_expires_at
    ON conversations (expires_at);


CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    tool_use        JSONB,          -- raw Anthropic tool_use blocks; NULL for user messages
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fetch messages for a conversation in order
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages (conversation_id, created_at ASC);


-- ── pg_cron auto-purge job (enable pg_cron extension in Supabase dashboard first) ──
-- Uncomment once pg_cron is enabled in your Supabase project:
--
-- SELECT cron.schedule(
--     'purge-expired-conversations',
--     '0 * * * *',   -- every hour
--     $$ DELETE FROM conversations WHERE expires_at < NOW() $$
-- );
--
-- Cascading FK on messages means conversation rows + all their messages are deleted.
