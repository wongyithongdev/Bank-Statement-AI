-- Bank Statement API: DB migration
-- Run once: PGPASSWORD=root psql -h 10.0.10.63 -U root -d root -f docker/migration.sql

ALTER TABLE bankstatement
  ADD COLUMN IF NOT EXISTS score FLOAT,
  ADD COLUMN IF NOT EXISTS iterations INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS stream_events JSONB DEFAULT '[]';
