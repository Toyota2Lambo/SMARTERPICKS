// =============================================================
// /api/odds-history — line movement endpoint
// =============================================================
// Reads the last N price snapshots for a specific matchup from
// the Supabase odds_history table populated by /api/terminal-data.
//
// Usage from the browser:
//   GET /api/odds-history?league=NBA&away=Boston%20Celtics
//       &home=Los%20Angeles%20Lakers&limit=20
//
// Returns:
//   {
//     league, away_team, home_team,
//     snapshots: [{ away_ml, home_ml, spread, total, recorded_at }, ...],
//     // Oldest first → newest last so client-side sparkline math is simple
//     delta: {
//       away_ml: { first, last, change },
//       home_ml: { first, last, change },
//       spread:  { first, last, change },
//       total:   { first, last, change },
//     }
//   }
//
// If Supabase isn't configured, returns 503 with a hint. The
// front-end treats that as "no movement data yet, skip the panel."
// =============================================================

const sb = require("../lib/supabase");

// In-memory cache so repeated drawer opens for the same matchup
// within a short window don't re-hit Supabase. 60s is plenty —
// the upstream odds_history is only being written every 10 min
// anyway.
const CACHE_TTL_MS = 60_000;
const cache = new Map();

const PER_IP_LIMIT   = 120;        // higher than the terminal data feed since it's lightweight
const RATE_WINDOW_MS = 60_000;
const ipBuckets = new Map();

function rateLimit(ip) {
  const now = Date.now();
  const bucket = ipBuckets.get(ip) || { count: 0, resetAt: now + RATE_WINDOW_MS };
  if (now > bucket.resetAt) {
    bucket.count = 0;
    bucket.resetAt = now + RATE_WINDOW_MS;
  }
  bucket.count += 1;
  ipBuckets.set(ip, bucket);
  return bucket.count <= PER_IP_LIMIT;
}

function deltaFor(snapshots, field) {
  // snapshots are sorted oldest → newest. We want the first
  // non-null reading vs the last non-null reading.
  let first = null, last = null;
  for (const s of snapshots) {
    if (s[field] != null) {
      if (first === null) first = s[field];
      last = s[field];
    }
  }
  if (first === null || last === null) return null;
  const a = Number(first), b = Number(last);
  if (!isFinite(a) || !isFinite(b)) return null;
  return {
    first: a,
    last:  b,
    change: +(b - a).toFixed(2),
  };
}

module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin",  "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")     return res.status(405).json({ error: "GET only" });

  if (!sb.configured()) {
    return res.status(503).json({ error: "supabase_not_configured" });
  }

  const ip = (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (!rateLimit(ip)) {
    return res.status(429).json({ error: "rate limit exceeded" });
  }

  const league    = String(req.query.league || "").toUpperCase();
  const away_team = String(req.query.away   || "").trim();
  const home_team = String(req.query.home   || "").trim();
  const limit     = Math.min(100, Math.max(2, parseInt(req.query.limit || "30", 10)));

  if (!league || !away_team || !home_team) {
    return res.status(400).json({ error: "league, away, home are required" });
  }

  const cacheKey = `${league}|${away_team}|${home_team}|${limit}`;
  const now = Date.now();
  const hit = cache.get(cacheKey);
  if (hit && (now - hit.at) < CACHE_TTL_MS) {
    res.setHeader("X-Odds-History-Cache", "HIT");
    return res.status(200).json(hit.payload);
  }

  try {
    // readOddsHistory returns newest first — flip for the client so
    // the sparkline reads left-to-right in time order.
    const recentDesc = await sb.readOddsHistory(league, away_team, home_team, limit);
    const snapshots = (recentDesc || []).slice().reverse();

    const payload = {
      league,
      away_team,
      home_team,
      count: snapshots.length,
      snapshots,
      delta: {
        away_ml: deltaFor(snapshots, "away_ml"),
        home_ml: deltaFor(snapshots, "home_ml"),
        spread:  deltaFor(snapshots, "spread"),
        total:   deltaFor(snapshots, "total"),
      },
    };

    cache.set(cacheKey, { at: now, payload });
    res.setHeader("X-Odds-History-Cache", "MISS");
    return res.status(200).json(payload);
  } catch (e) {
    return res.status(500).json({
      error: "odds_history_read_failed",
      detail: String(e).slice(0, 240),
    });
  }
};
