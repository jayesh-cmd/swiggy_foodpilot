-- Migration 004: Enable pg_cron for automatic conversation cleanup
-- ──────────────────────────────────────────────────────────────────────────────
-- WHAT THIS DOES:
--   Sets up a cron job that runs every hour and deletes conversations whose
--   expires_at has passed. Because messages.conversation_id has ON DELETE CASCADE,
--   all messages in expired conversations are also deleted automatically.
--
-- WHY pg_cron INSTEAD OF APPLICATION-LEVEL CLEANUP?
--   Option A (app): Run a cleanup task every hour in Python.
--     Problem: The app may be down. The cleanup misses. Rows accumulate forever.
--   Option B (pg_cron): Postgres runs the cleanup itself.
--     Advantage: Database-level, runs even when the app is offline. Reliable.
--
-- HOW TO RUN THIS:
--   Step 1: Enable pg_cron in Supabase
--     → Dashboard → Database → Extensions → search "pg_cron" → Enable
--
--   Step 2: Run this SQL in Supabase SQL Editor
--     → Dashboard → SQL Editor → New Query → paste → Run
--
-- VERIFY:
--   SELECT * FROM cron.job WHERE jobname = 'purge-expired-conversations';
-- ──────────────────────────────────────────────────────────────────────────────

-- Enable the pg_cron extension (idempotent — safe to run twice)
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Remove existing job if re-running this migration
SELECT cron.unschedule('purge-expired-conversations')
WHERE EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'purge-expired-conversations'
);

-- Schedule: delete expired conversations every hour at :00
-- Cascade deletes all their messages via the FK constraint
SELECT cron.schedule(
    'purge-expired-conversations',
    '0 * * * *',
    $$ DELETE FROM conversations WHERE expires_at < NOW() $$
);

-- Also update migration 003's commented-out pg_cron block status
-- (No SQL needed — the block in 003 is a comment, this migration supersedes it)

-- Confirm the job is registered
SELECT
    jobid,
    jobname,
    schedule,
    command,
    active
FROM cron.job
WHERE jobname = 'purge-expired-conversations';
