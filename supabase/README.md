# Supabase setup

SmarterPicks uses Supabase as a shared persistence layer:

- **L2 cache** for the Odds API so the entire production fleet
  only spends one upstream call every 10 minutes, not one per
  cold-started Vercel instance.
- **Line-movement history** so the terminal can show "Lakers ML
  moved from -150 → -135 in 4 hours" on every game.
- **Player metadata** so we don't re-derive the same player
  records every 90 seconds.
- **(Future) Per-user watchlists + terminal layouts** keyed by
  Whop user ID for cross-device portability.

The schema lives in `schema.sql` and is **idempotent** — run it
as many times as you need.

## 1 — Create the project

1. Go to <https://supabase.com> and create a new project.
2. Pick a region close to Vercel's primary region (default: US East).
3. Set a strong DB password. You don't need to memorize it; we
   use the API keys, not the Postgres connection string.

## 2 — Run the schema

1. In the Supabase dashboard, open **SQL Editor → New query**.
2. Paste the contents of `schema.sql`.
3. Click **Run**. You should see "Success. No rows returned."
4. Confirm in **Table Editor** that `kv_cache`, `odds_history`,
   `player_meta`, `watchlists`, `terminal_layouts` all exist.

## 3 — Grab the API keys

1. **Project Settings → API**.
2. Copy:
   - **Project URL**            → goes into `SUPABASE_URL`
   - **service_role** secret    → goes into `SUPABASE_SERVICE_ROLE_KEY`

⚠️ The `service_role` key bypasses Row Level Security. **Never**
ship it to the browser. It lives only in Vercel env vars and is
read by serverless functions.

## 4 — Add to Vercel

In your Vercel project → **Settings → Environment Variables**:

| Variable                      | Value                                | Environments        |
| ----------------------------- | ------------------------------------ | ------------------- |
| `SUPABASE_URL`                | `https://xxxxx.supabase.co`          | Production, Preview |
| `SUPABASE_SERVICE_ROLE_KEY`   | `eyJ…` (the service_role key)        | Production, Preview |

Redeploy and the terminal API will start using Supabase
automatically. If the env vars are missing, the API gracefully
falls back to in-memory caching (so production never breaks if
Supabase is temporarily unreachable).

## 5 — Verify

- Hit `/api/terminal-data` once.
- In Supabase **Table Editor → kv_cache**, you should see rows
  like `odds:basketball_nba`, `scoreboard:basketball/nba`, etc.
- In **odds_history**, rows appear after the first fresh Odds
  fetch (cache miss). One row per game per fetch.
- Vercel logs will show `[odds] real fetch · sport=… · calls_this_hour=N`
  only ~once per 10 minutes, not on every request.

## 6 — Maintenance

The `kv_cache` table grows by a handful of rows then stays
roughly constant (one row per upstream key). `odds_history`
grows ~100 rows/day. To keep things tidy, add a scheduled
cleanup:

```sql
DELETE FROM kv_cache WHERE expires_at < NOW() - INTERVAL '1 day';
DELETE FROM odds_history WHERE recorded_at < NOW() - INTERVAL '90 days';
```

Set this up under **Database → Cron Jobs** (Supabase Pro) or run
it manually monthly.

## Why no SDK?

`lib/supabase.js` calls the PostgREST API directly with `fetch`.
No `@supabase/supabase-js` dependency means:

- Zero `npm install` step
- Smaller Vercel function bundles (faster cold-starts)
- One fewer thing to keep updated

PostgREST is stable and well-documented; the helpers in
`lib/supabase.js` cover everything we need (`select`, `upsert`,
`delete`).
