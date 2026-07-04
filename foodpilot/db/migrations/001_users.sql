-- Migration 001: users table
-- Applied in: M2 (Supabase Auth)
-- Run with: supabase db push

CREATE TABLE IF NOT EXISTS users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supabase_id  UUID UNIQUE NOT NULL,  -- from Supabase Auth (auth.users.id)
    email        TEXT UNIQUE NOT NULL,
    name         TEXT,
    avatar_url   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast lookup by Supabase Auth user ID (used on every request)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_supabase_id ON users (supabase_id);

-- Index for email lookup (login, dedup)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users (email);
