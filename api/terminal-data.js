// =============================================================
// Vercel serverless: SmarterPicks Terminal — aggregated data feed
// =============================================================
// One endpoint, multiple sources combined into a single JSON
// response so the terminal page can refresh atomically without
// chasing N parallel client fetches.
//
// Sources combined:
//   - ESPN public site API (no auth)
//       · scoreboards per league (live/today's games)
//       · news headlines
//       · injury reports
//   - The Odds API (server-side, ODDS_API_KEY in env)
//       · today's lines across major books
//
// Returned shape:
//   { generated_at, leagues: { nba, mlb, nhl, nfl }, news: [...] }
//   leagues[x] = { scoreboard: [...], injuries: [...], odds: [...] }
//
// CACHING (cheap + critical):
//   In-memory 30s TTL per response. Most terminal users refresh
//   the page every 30s; cache means one Odds-API call per minute
//   not one per user × per minute. Significant cost protection.
//
// REQUIRED VERCEL ENV VAR:
//   ODDS_API_KEY — from the-odds-api.com (already configured for
//   picks_generator.py / score_yesterday.py)
// =============================================================

const ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports";
const ODDS_BASE = "https://api.the-odds-api.com/v4/sports";

// Each entry: ESPN path (for scoreboard + news) and Odds API key (for odds).
// Keep this list lean to bound the work per refresh.
const LEAGUES = [
  { key: "nba", espn: "basketball/nba", odds: "basketball_nba",        label: "NBA" },
  { key: "mlb", espn: "baseball/mlb",   odds: "baseball_mlb",          label: "MLB" },
  { key: "nhl", espn: "hockey/nhl",     odds: "icehockey_nhl",         label: "NHL" },
  { key: "nfl", espn: "football/nfl",   odds: "americanfootball_nfl",  label: "NFL" },
];

// In-memory cache. Each Vercel instance has its own — fine for a
// 30s TTL since most user refreshes hit the same warm instance.
let CACHE = { at: 0, payload: null };
const CACHE_TTL_MS = 30_000;

// Per-IP rate limit (matches the pattern in claude-chat.js).
const PER_IP_LIMIT   = 60;        // 60 req / minute / IP
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

// ── ESPN scoreboard → normalized rows ──────────────────────────
// ESPN returns rich structures; we keep only fields the terminal
// needs (team names, scores, game state, time). Cuts payload size
// dramatically and stops the page from rendering huge JSON it
// doesn't use.
async function fetchEspnScoreboard(espnPath) {
  try {
    const r = await fetch(`${ESPN_BASE}/${espnPath}/scoreboard`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    return (data.events || []).map(ev => {
      const comp = (ev.competitions && ev.competitions[0]) || {};
      const competitors = comp.competitors || [];
      const away = competitors.find(c => c.homeAway === "away") || {};
      const home = competitors.find(c => c.homeAway === "home") || {};
      const status = (ev.status && ev.status.type) || {};
      return {
        id:           ev.id,
        away_team:    (away.team && away.team.abbreviation) || (away.team && away.team.shortDisplayName) || "",
        away_name:    (away.team && away.team.displayName) || "",
        away_score:   away.score || "0",
        away_record:  (away.records && away.records[0] && away.records[0].summary) || "",
        home_team:    (home.team && home.team.abbreviation) || (home.team && home.team.shortDisplayName) || "",
        home_name:    (home.team && home.team.displayName) || "",
        home_score:   home.score || "0",
        home_record:  (home.records && home.records[0] && home.records[0].summary) || "",
        state:        status.state || "",          // "pre" | "in" | "post"
        detail:       status.shortDetail || "",    // "7:30 PM ET" | "Q3 9:42" | "Final"
        completed:    !!status.completed,
        start_time:   ev.date,
      };
    });
  } catch (_) {
    return [];
  }
}

// ── ESPN news headlines → trimmed list ─────────────────────────
async function fetchEspnNews(espnPath) {
  try {
    const r = await fetch(`${ESPN_BASE}/${espnPath}/news`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    return (data.articles || []).slice(0, 8).map(a => ({
      headline:  a.headline || a.title || "",
      published: a.published || a.lastModified || "",
      url:       (a.links && a.links.web && a.links.web.href) || "",
    }));
  } catch (_) {
    return [];
  }
}

// ── ESPN injuries (per-league) ─────────────────────────────────
async function fetchEspnInjuries(espnPath) {
  try {
    const r = await fetch(`${ESPN_BASE}/${espnPath}/news/injuries`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    // ESPN's injuries endpoint structure: { injuries: [{ team, injuries: [...] }] }
    const out = [];
    for (const teamBlock of (data.injuries || [])) {
      const team = (teamBlock.team && teamBlock.team.abbreviation) || "";
      for (const inj of (teamBlock.injuries || []).slice(0, 3)) {
        out.push({
          team,
          athlete:    (inj.athlete && inj.athlete.displayName) || "",
          position:   (inj.athlete && inj.athlete.position && inj.athlete.position.abbreviation) || "",
          status:     inj.status || "",
          short_desc: (inj.shortComment || "").slice(0, 120),
        });
      }
    }
    return out.slice(0, 12);
  } catch (_) {
    return [];
  }
}

// ── The Odds API: today's lines per sport ──────────────────────
// Returns one row per game with the best moneyline + spread from
// any book, plus the implied line movement (open vs current via
// average). Keeps payload tight.
async function fetchOdds(sportKey, apiKey) {
  if (!apiKey) return [];
  try {
    const qs = new URLSearchParams({
      apiKey,
      regions:    "us",
      markets:    "h2h,spreads",
      oddsFormat: "american",
    });
    const r = await fetch(`${ODDS_BASE}/${sportKey}/odds?${qs}`, { cache: "no-store" });
    if (!r.ok) return [];
    const games = await r.json();
    return (games || []).slice(0, 12).map(g => {
      // Average h2h + best spread across books.
      const h2h_quotes = [];
      const spread_quotes = [];
      for (const bk of (g.bookmakers || [])) {
        for (const m of (bk.markets || [])) {
          if (m.key === "h2h") h2h_quotes.push({ book: bk.key, outcomes: m.outcomes });
          if (m.key === "spreads") spread_quotes.push({ book: bk.key, outcomes: m.outcomes });
        }
      }
      // Best moneyline for home + away (highest American price = best to bettor)
      const best = (team, quotes) => {
        let best_price = null, best_book = "";
        for (const q of quotes) {
          for (const o of (q.outcomes || [])) {
            if (o.name === team && (best_price === null || o.price > best_price)) {
              best_price = o.price;
              best_book  = q.book;
            }
          }
        }
        return { price: best_price, book: best_book };
      };
      const home_ml = best(g.home_team, h2h_quotes);
      const away_ml = best(g.away_team, h2h_quotes);
      // Median spread for home
      const home_spreads = [];
      for (const q of spread_quotes) {
        for (const o of (q.outcomes || [])) {
          if (o.name === g.home_team) home_spreads.push(o.point);
        }
      }
      home_spreads.sort((a,b) => a-b);
      const median_spread = home_spreads.length
        ? home_spreads[Math.floor(home_spreads.length / 2)]
        : null;
      return {
        commence_time: g.commence_time,
        away_team:     g.away_team,
        home_team:     g.home_team,
        away_ml_price: away_ml.price,
        away_ml_book:  away_ml.book,
        home_ml_price: home_ml.price,
        home_ml_book:  home_ml.book,
        home_spread:   median_spread,
        book_count:    h2h_quotes.length,
      };
    });
  } catch (_) {
    return [];
  }
}

// ── Build the full payload ─────────────────────────────────────
async function buildPayload() {
  const oddsKey = process.env.ODDS_API_KEY || "";

  const leagueResults = await Promise.all(LEAGUES.map(async lg => {
    const [scoreboard, injuries, odds] = await Promise.all([
      fetchEspnScoreboard(lg.espn),
      fetchEspnInjuries(lg.espn),
      fetchOdds(lg.odds, oddsKey),
    ]);
    return [lg.key, { label: lg.label, scoreboard, injuries, odds }];
  }));

  // News: pull from each league's news feed, interleave by published time
  // so the feed mixes sports rather than blocks them by league.
  const newsLists = await Promise.all(LEAGUES.map(lg => fetchEspnNews(lg.espn)));
  const allNews = [];
  newsLists.forEach((items, i) => {
    const lg = LEAGUES[i];
    for (const a of items) allNews.push({ ...a, league: lg.label });
  });
  allNews.sort((a, b) => new Date(b.published) - new Date(a.published));

  const leagues = Object.fromEntries(leagueResults);
  return {
    generated_at: new Date().toISOString(),
    leagues,
    news: allNews.slice(0, 30),
  };
}

// ── Handler ────────────────────────────────────────────────────
module.exports = async (req, res) => {
  // CORS — same-origin only is fine since this is consumed by
  // terminal.html on smarterpicks.io, but allow GET from anywhere
  // so curl/etc. can hit it for testing.
  res.setHeader("Access-Control-Allow-Origin",  "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")     return res.status(405).json({ error: "GET only" });

  const ip = (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (!rateLimit(ip)) {
    return res.status(429).json({ error: "rate limit exceeded" });
  }

  // Cache hit?
  const now = Date.now();
  if (CACHE.payload && (now - CACHE.at) < CACHE_TTL_MS) {
    res.setHeader("X-Terminal-Cache", "HIT");
    return res.status(200).json(CACHE.payload);
  }

  try {
    const payload = await buildPayload();
    CACHE = { at: now, payload };
    res.setHeader("X-Terminal-Cache", "MISS");
    return res.status(200).json(payload);
  } catch (e) {
    return res.status(500).json({ error: "terminal-data build failed", detail: String(e).slice(0, 240) });
  }
};
