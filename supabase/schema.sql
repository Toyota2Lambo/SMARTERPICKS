-- =============================================================
-- SmarterPicks · Supabase schema
-- =============================================================
-- Run this once in the Supabase SQL editor to provision the
-- tables used by /api/terminal-data and (future) member-scoped
-- watchlists + terminal layouts.
--
-- Idempotent: re-running is safe.
-- =============================================================

-- ── kv_cache ─────────────────────────────────────────────────
-- Generic key-value cache with TTL. Used as a global L2 cache
-- shared across all Vercel serverless instances so we only call
-- the Odds API ~one time every 10 min worldwide instead of once
-- per cold-start, per instance.
-- =============================================================
CREATE TABLE IF NOT EXISTS kv_cache (
  key         TEXT        PRIMARY KEY,
  value       JSONB       NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS kv_cache_expires_idx
  ON kv_cache (expires_at);

-- Cleanup job (manual; cron available in Supabase Pro). Drop
-- expired entries so the table doesn't grow forever.
-- Call from a Supabase scheduled function or just run periodically:
--   DELETE FROM kv_cache WHERE expires_at < NOW() - INTERVAL '1 day';


-- ── odds_history ─────────────────────────────────────────────
-- Every fresh Odds API pull writes one row per game so we can
-- show line movement ("Lakers ML moved from -150 → -135 in 4h").
-- ~24 rows per league per day × 4 leagues = ~100 rows/day.
-- Cheap to store; small JSONB column for the bookmaker sample.
-- =============================================================
CREATE TABLE IF NOT EXISTS odds_history (
  id          BIGSERIAL   PRIMARY KEY,
  league      TEXT        NOT NULL,
  away_team   TEXT        NOT NULL,
  home_team   TEXT        NOT NULL,
  away_ml     INTEGER,        -- American odds, e.g. -150
  home_ml     INTEGER,
  away_book   TEXT,           -- which book had the best Away ML
  home_book   TEXT,
  spread      NUMERIC(4,1),   -- home-side spread, e.g. -3.5
  total       NUMERIC(5,1),   -- median over/under
  book_count  INTEGER,        -- number of books in the sample
  books       JSONB,          -- sample of book keys (small array)
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Compound index: most common query is "movement for this game,
-- newest first" — league + matchup + time DESC.
CREATE INDEX IF NOT EXISTS odds_history_game_idx
  ON odds_history (league, away_team, home_team, recorded_at DESC);

-- Secondary index: recency across all games (for a "recent moves"
-- panel on the terminal).
CREATE INDEX IF NOT EXISTS odds_history_recent_idx
  ON odds_history (recorded_at DESC);


-- ── player_meta ──────────────────────────────────────────────
-- Slow-changing player metadata cache. Built up from injury +
-- news fetches; lets us avoid re-deriving the same record every
-- 90 seconds. Future: nightly job to refresh proactively from
-- ESPN rosters.
-- =============================================================
CREATE TABLE IF NOT EXISTS player_meta (
  athlete_id  TEXT        PRIMARY KEY,
  name        TEXT        NOT NULL,
  team        TEXT,
  team_name   TEXT,
  league      TEXT,
  position    TEXT,
  headshot    TEXT,
  initials    TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS player_meta_team_idx ON player_meta (league, team);
CREATE INDEX IF NOT EXISTS player_meta_name_idx ON player_meta (LOWER(name));


-- ── watchlists ───────────────────────────────────────────────
-- Per-user watchlists. Currently the terminal stores these in
-- localStorage; once a Whop user is signed in we can sync to
-- this table keyed by their Whop user ID so the watchlist is
-- portable across devices.
-- =============================================================
CREATE TABLE IF NOT EXISTS watchlists (
  user_id     TEXT        NOT NULL,
  kind        TEXT        NOT NULL CHECK (kind IN ('team', 'player')),
  key         TEXT        NOT NULL,
  label       TEXT,
  league      TEXT,
  added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, kind, key)
);

CREATE INDEX IF NOT EXISTS watchlists_user_idx ON watchlists (user_id, added_at DESC);


-- ── terminal_layouts ─────────────────────────────────────────
-- Per-user resizable-workspace layout. Currently localStorage;
-- syncing here lets the layout follow a user across devices.
-- =============================================================
CREATE TABLE IF NOT EXISTS terminal_layouts (
  user_id     TEXT        PRIMARY KEY,
  layout      JSONB       NOT NULL,
  preset      TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── pick_results (optional) ──────────────────────────────────
-- Server-side mirror of picks.json + results.json. Lets you run
-- proper SQL queries against the public track record (win rate
-- by league, ROI by confidence tier, streak detection, etc.).
-- For now it's optional — keep this commented out and revisit
-- once you actually want to query the history.
-- =============================================================
-- CREATE TABLE IF NOT EXISTS pick_results (
--   id              BIGSERIAL PRIMARY KEY,
--   pick_date       DATE        NOT NULL,
--   league          TEXT        NOT NULL,
--   away_team       TEXT,
--   home_team       TEXT,
--   pick            TEXT        NOT NULL,
--   odds            TEXT,
--   book            TEXT,
--   stake_units     NUMERIC(3,1),
--   confidence      TEXT,
--   tags            TEXT[],
--   reasoning       TEXT,
--   is_premium      BOOLEAN     NOT NULL DEFAULT FALSE,
--   result          TEXT CHECK (result IN ('WON','LOST','PUSH','VOID',NULL)),
--   units_returned  NUMERIC(5,2),
--   final_score     TEXT,
--   recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
--   UNIQUE (pick_date, league, away_team, home_team, pick)
-- );

-- =============================================================
-- ROW LEVEL SECURITY — for now, all writes happen server-side
-- using the SERVICE_ROLE key, which bypasses RLS. When/if we
-- expose any of these tables to the browser directly (e.g. let
-- a logged-in user save their layout client-side), we'll need
-- to enable RLS + per-table policies. Leaving disabled for now
-- so the serverless functions Just Work.
-- =============================================================
