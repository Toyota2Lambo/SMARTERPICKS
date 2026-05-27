// =============================================================
// Supabase REST client (no SDK, no dependencies)
// =============================================================
// Talks to Supabase's PostgREST endpoint directly via fetch.
// Used by /api/terminal-data for L2 cache + line-movement history.
//
// Required env vars (set in Vercel → Settings → Env Vars):
//   SUPABASE_URL                  — https://<project>.supabase.co
//   SUPABASE_SERVICE_ROLE_KEY     — the service_role key (NOT anon).
//                                   Bypasses RLS; never ship to
//                                   the browser.
//
// If either env var is missing, every helper returns null/false
// instead of throwing. Callers fall back to in-memory cache so
// production doesn't break if Supabase env is unset / down.
// =============================================================

const SB_URL = process.env.SUPABASE_URL || "";
const SB_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "";

function configured() {
  return !!(SB_URL && SB_KEY);
}

function headers(extra = {}) {
  return {
    apikey: SB_KEY,
    Authorization: `Bearer ${SB_KEY}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

// ── Low-level helpers ─────────────────────────────────────────
async function sbGet(pathAndQuery) {
  if (!configured()) return null;
  try {
    const r = await fetch(`${SB_URL}/rest/v1/${pathAndQuery}`, {
      headers: headers(),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

// Upsert one or more rows. `onConflict` is a comma-separated list
// of column names that form the conflict target (typically the
// primary key — e.g. "key" for kv_cache, "athlete_id" for
// player_meta).
async function sbUpsert(table, rows, onConflict) {
  if (!configured()) return false;
  try {
    const qs = onConflict ? `?on_conflict=${encodeURIComponent(onConflict)}` : "";
    const r = await fetch(`${SB_URL}/rest/v1/${table}${qs}`, {
      method: "POST",
      headers: headers({
        Prefer: "resolution=merge-duplicates,return=minimal",
      }),
      body: JSON.stringify(Array.isArray(rows) ? rows : [rows]),
    });
    return r.ok;
  } catch (_) {
    return false;
  }
}

// Insert without conflict handling — used for append-only tables
// like odds_history.
async function sbInsert(table, rows) {
  if (!configured()) return false;
  try {
    const r = await fetch(`${SB_URL}/rest/v1/${table}`, {
      method: "POST",
      headers: headers({ Prefer: "return=minimal" }),
      body: JSON.stringify(Array.isArray(rows) ? rows : [rows]),
    });
    return r.ok;
  } catch (_) {
    return false;
  }
}

async function sbDelete(pathAndQuery) {
  if (!configured()) return false;
  try {
    const r = await fetch(`${SB_URL}/rest/v1/${pathAndQuery}`, {
      method: "DELETE",
      headers: headers(),
    });
    return r.ok;
  } catch (_) {
    return false;
  }
}

// ── kv_cache helpers ──────────────────────────────────────────
// Generic key-value cache with TTL. Used as the shared L2 cache
// across all serverless instances. If we miss in L2, we fetch
// fresh upstream and write back to L2 so the next request from
// any other instance gets a hit.
//
// Returns null on miss / expired / Supabase-unconfigured.
async function kvGet(key) {
  const rows = await sbGet(
    `kv_cache?key=eq.${encodeURIComponent(key)}&select=value,expires_at`
  );
  if (!rows || rows.length === 0) return null;
  const row = rows[0];
  if (!row.expires_at) return null;
  if (new Date(row.expires_at).getTime() <= Date.now()) return null;
  return row.value;
}

async function kvSet(key, value, ttlMs) {
  const expires_at = new Date(Date.now() + ttlMs).toISOString();
  return await sbUpsert(
    "kv_cache",
    [{ key, value, expires_at, updated_at: new Date().toISOString() }],
    "key"
  );
}

// ── odds_history helpers ──────────────────────────────────────
// Append a snapshot of the latest odds pull. Caller passes the
// normalized odds rows from fetchOdds(); we map only what we
// want to persist (no need to store the entire bookmakers blob).
async function recordOddsHistory(league, oddsRows) {
  if (!configured()) return false;
  if (!Array.isArray(oddsRows) || oddsRows.length === 0) return false;
  const rows = oddsRows.map(g => ({
    league:     league,
    away_team:  g.away_team || "",
    home_team:  g.home_team || "",
    away_ml:    g.away_ml_price ?? null,
    home_ml:    g.home_ml_price ?? null,
    away_book:  g.away_ml_book || null,
    home_book:  g.home_ml_book || null,
    spread:     g.home_spread ?? null,
    total:      g.total ?? null,
    book_count: g.book_count ?? null,
    books:      Array.isArray(g.books) ? g.books : null,
  })).filter(r => r.away_team && r.home_team);
  return await sbInsert("odds_history", rows);
}

// Read the last N price snapshots for a specific matchup. Used
// (future) to render line-movement deltas in the game drawer.
async function readOddsHistory(league, away_team, home_team, limit = 24) {
  const qs = `odds_history?league=eq.${encodeURIComponent(league)}`
    + `&away_team=eq.${encodeURIComponent(away_team)}`
    + `&home_team=eq.${encodeURIComponent(home_team)}`
    + `&select=away_ml,home_ml,spread,total,recorded_at`
    + `&order=recorded_at.desc`
    + `&limit=${limit}`;
  return (await sbGet(qs)) || [];
}

// ── player_meta helpers ───────────────────────────────────────
// Upsert player metadata seen during the current refresh so a
// later request can render fast without re-walking ESPN's
// injury/news payloads.
async function upsertPlayerMeta(players) {
  if (!Array.isArray(players) || players.length === 0) return false;
  const rows = players
    .filter(p => p && p.id && p.name)   // skip records ESPN didn't tag with an id
    .map(p => ({
      athlete_id: String(p.id),
      name:       p.name,
      team:       p.team || null,
      team_name:  p.team_name || null,
      league:     p.league || null,
      position:   p.position || null,
      headshot:   p.headshot || null,
      initials:   p.initials || null,
      updated_at: new Date().toISOString(),
    }));
  if (rows.length === 0) return false;
  return await sbUpsert("player_meta", rows, "athlete_id");
}

module.exports = {
  configured,
  sbGet,
  sbUpsert,
  sbInsert,
  sbDelete,
  kvGet,
  kvSet,
  recordOddsHistory,
  readOddsHistory,
  upsertPlayerMeta,
};
