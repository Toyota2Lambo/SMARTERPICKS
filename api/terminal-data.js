// =============================================================
// Vercel serverless: SmarterPicks Terminal — aggregated data feed
// =============================================================
// One endpoint, multiple sources combined into a single JSON
// response so the terminal page can refresh atomically without
// chasing N parallel client fetches.
//
// Sources combined:
//   - ESPN public site API (no auth)
//       · scoreboards per league (live/today's games + top performers)
//       · news headlines (with player/team mention extraction)
//       · injury reports (rich — desc, status, position)
//       · standings per league
//   - The Odds API (server-side, ODDS_API_KEY in env)
//       · today's lines across major books
//
// Returned shape (the terminal frontend treats this as canonical):
//   {
//     generated_at: ISO timestamp,
//     leagues: {
//       nba: {
//         label, scoreboard[], injuries[], odds[], standings[]
//       }, mlb, nhl, nfl
//     },
//     news:      [{ headline, published, url, league, mentions: [] }],
//     players:   [{ name, team, league, status?, last_news?, mentions, image_url }],
//     analysis:  { date, summary, picks[], pick_count, free_count },
//     meta:      { sources, latency_ms, cached }
//   }
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

const fs   = require("fs");
const path = require("path");

const ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports";
const ESPN_V2   = "https://site.api.espn.com/apis/v2/sports";
const ODDS_BASE = "https://api.the-odds-api.com/v4/sports";

const PICKS_PATH = path.join(process.cwd(), "picks.json");

function readPicks() {
  try {
    const raw = fs.readFileSync(PICKS_PATH, "utf8");
    const data = JSON.parse(raw);
    const picks = (data.picks || []).map(p => ({
      league:    p.league || "",
      time:      p.time || "",
      away_team: p.away_team || "",
      home_team: p.home_team || "",
      pick:      p.pick || "",
      odds:      p.odds || "",
      book:      p.book || "",
      stake:     p.stake || "",
      reasoning: p.reasoning || "",
      tags:      Array.isArray(p.tags) ? p.tags : [],
      is_premium: !!p.is_premium,
    }));
    return {
      date:        data.date || "",
      summary:     data.sport_summary || "",
      picks,
      pick_count:  picks.length,
      free_count:  picks.filter(p => !p.is_premium).length,
    };
  } catch (e) {
    return { date: "", summary: "", picks: [], pick_count: 0, free_count: 0, error: String(e).slice(0, 120) };
  }
}

// Each entry: ESPN path (for scoreboard + news) and Odds API key (for odds).
const LEAGUES = [
  { key: "nba", espn: "basketball/nba", odds: "basketball_nba",        label: "NBA" },
  { key: "mlb", espn: "baseball/mlb",   odds: "baseball_mlb",          label: "MLB" },
  { key: "nhl", espn: "hockey/nhl",     odds: "icehockey_nhl",         label: "NHL" },
  { key: "nfl", espn: "football/nfl",   odds: "americanfootball_nfl",  label: "NFL" },
];

let CACHE = { at: 0, payload: null };
const CACHE_TTL_MS = 30_000;

const PER_IP_LIMIT   = 60;
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

// ── ESPN scoreboard → normalized rows with TOP PERFORMERS ──────
// ESPN bakes per-game "leaders" (top scorer/rebounder/passer or
// per-sport equivalent) into the scoreboard. We extract that so
// the terminal can show who's lighting it up tonight without an
// extra round-trip per game.
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
      const venue  = comp.venue && comp.venue.fullName ? comp.venue.fullName : "";
      const broadcasts = Array.isArray(comp.broadcasts) ? comp.broadcasts.flatMap(b => b.names || []) : [];

      // Top performers — ESPN shape varies but each entry has a name
      // ("rating", "points") and a leaders[] with the actual player.
      const top_performers = [];
      const buckets = comp.leaders || [];
      for (const cat of buckets.slice(0, 3)) {
        const leader = (cat.leaders || [])[0];
        if (!leader || !leader.athlete) continue;
        top_performers.push({
          stat:        cat.shortDisplayName || cat.displayName || cat.name || "",
          value:       leader.displayValue || "",
          player:      leader.athlete.displayName || "",
          player_id:   String(leader.athlete.id || ""),
          team:        (leader.team && leader.team.abbreviation) || "",
          headshot:    leader.athlete.headshot || "",
          position:    (leader.athlete.position && leader.athlete.position.abbreviation) || "",
        });
      }

      return {
        id:           ev.id,
        away_team:    (away.team && away.team.abbreviation) || (away.team && away.team.shortDisplayName) || "",
        away_name:    (away.team && away.team.displayName) || "",
        away_logo:    (away.team && away.team.logo) || "",
        away_score:   away.score || "0",
        away_record:  (away.records && away.records[0] && away.records[0].summary) || "",
        home_team:    (home.team && home.team.abbreviation) || (home.team && home.team.shortDisplayName) || "",
        home_name:    (home.team && home.team.displayName) || "",
        home_logo:    (home.team && home.team.logo) || "",
        home_score:   home.score || "0",
        home_record:  (home.records && home.records[0] && home.records[0].summary) || "",
        state:        status.state || "",
        detail:       status.shortDetail || "",
        completed:    !!status.completed,
        start_time:   ev.date,
        venue,
        broadcasts:   broadcasts.slice(0, 3),
        top_performers,
      };
    });
  } catch (_) {
    return [];
  }
}

// ── ESPN news — keeps richer payload (image, byline, players[]) ─
async function fetchEspnNews(espnPath) {
  try {
    const r = await fetch(`${ESPN_BASE}/${espnPath}/news`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    return (data.articles || []).slice(0, 12).map(a => {
      // ESPN categorizes each article with team/athlete tags.
      const mentions_teams   = [];
      const mentions_players = [];
      for (const cat of (a.categories || [])) {
        if (cat.type === "team" && cat.team)
          mentions_teams.push({ name: cat.team.description || "", abbr: cat.team.abbreviation || "" });
        if (cat.type === "athlete" && cat.athlete)
          mentions_players.push({
            name: cat.athlete.description || "",
            id:   String(cat.athlete.id || ""),
          });
      }
      // Extract first image URL if present
      let image = "";
      if (Array.isArray(a.images) && a.images.length) {
        image = a.images[0].url || "";
      }
      return {
        headline:  a.headline || a.title || "",
        published: a.published || a.lastModified || "",
        url:       (a.links && a.links.web && a.links.web.href) || "",
        type:      a.type || "",
        image,
        mentions_teams,
        mentions_players,
      };
    });
  } catch (_) {
    return [];
  }
}

// ── ESPN injuries (per-league) — keeps richer payload ───────────
async function fetchEspnInjuries(espnPath) {
  try {
    const r = await fetch(`${ESPN_BASE}/${espnPath}/news/injuries`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    const out = [];
    for (const teamBlock of (data.injuries || [])) {
      const team_abbr = (teamBlock.team && teamBlock.team.abbreviation) || "";
      const team_name = (teamBlock.team && teamBlock.team.displayName) || "";
      const team_logo = (teamBlock.team && teamBlock.team.logo) || "";
      for (const inj of (teamBlock.injuries || []).slice(0, 5)) {
        out.push({
          team:        team_abbr,
          team_name,
          team_logo,
          athlete:     (inj.athlete && inj.athlete.displayName) || "",
          athlete_id:  String((inj.athlete && inj.athlete.id) || ""),
          headshot:    (inj.athlete && inj.athlete.headshot) || "",
          position:    (inj.athlete && inj.athlete.position && inj.athlete.position.abbreviation) || "",
          status:      inj.status || "",
          short_desc:  (inj.shortComment || "").slice(0, 220),
          long_desc:   (inj.longComment || inj.details || "").slice(0, 320),
          date:        inj.date || "",
        });
      }
    }
    return out.slice(0, 20);
  } catch (_) {
    return [];
  }
}

// ── ESPN standings — division-grouped league standings ──────────
async function fetchEspnStandings(espnPath) {
  try {
    // The v2 standings endpoint returns the cleanest grouped output.
    const r = await fetch(`${ESPN_V2}/${espnPath}/standings`, { cache: "no-store" });
    if (!r.ok) return [];
    const data = await r.json();
    const out = [];
    // Walk children (divisions / conferences). Some leagues return
    // children[].children[]; some return entries[] directly. Handle both.
    const groups = (data.children && data.children.length) ? data.children : [data];
    for (const grp of groups.slice(0, 8)) {
      const grp_name = grp.name || grp.shortName || "";
      const inner = (grp.children && grp.children.length) ? grp.children : [grp];
      for (const sub of inner) {
        const sub_name = sub.name || sub.shortName || grp_name;
        const entries  = (sub.standings && sub.standings.entries) || sub.entries || [];
        for (const e of entries.slice(0, 6)) {
          const stats = (e.stats || []).reduce((acc, s) => {
            acc[s.name || s.type] = s.displayValue || s.value;
            return acc;
          }, {});
          out.push({
            group:    sub_name,
            team:     (e.team && e.team.abbreviation) || "",
            team_name:(e.team && e.team.displayName) || "",
            team_logo:(e.team && e.team.logos && e.team.logos[0] && e.team.logos[0].href) || "",
            wins:     stats.wins      || "",
            losses:   stats.losses    || "",
            ties:     stats.ties      || "",
            pct:      stats.winPercent || stats["win-percent"] || "",
            gb:       stats.gamesBehind || stats.gb || "",
            streak:   stats.streak    || "",
            rank:     stats.playoffSeed || stats.rank || "",
          });
        }
      }
    }
    return out;
  } catch (_) {
    return [];
  }
}

// ── The Odds API: today's lines per sport ──────────────────────
async function fetchOdds(sportKey, apiKey) {
  if (!apiKey) return [];
  try {
    const qs = new URLSearchParams({
      apiKey,
      regions:    "us",
      markets:    "h2h,spreads,totals",
      oddsFormat: "american",
    });
    const r = await fetch(`${ODDS_BASE}/${sportKey}/odds?${qs}`, { cache: "no-store" });
    if (!r.ok) return [];
    const games = await r.json();
    return (games || []).slice(0, 16).map(g => {
      const h2h_quotes = [];
      const spread_quotes = [];
      const total_quotes = [];
      for (const bk of (g.bookmakers || [])) {
        for (const m of (bk.markets || [])) {
          if (m.key === "h2h")     h2h_quotes.push({ book: bk.key, outcomes: m.outcomes });
          if (m.key === "spreads") spread_quotes.push({ book: bk.key, outcomes: m.outcomes });
          if (m.key === "totals")  total_quotes.push({ book: bk.key, outcomes: m.outcomes });
        }
      }
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

      // Median total (over/under)
      const totals = [];
      for (const q of total_quotes) {
        for (const o of (q.outcomes || [])) {
          if (o.name === "Over") totals.push(o.point);
        }
      }
      totals.sort((a,b) => a-b);
      const median_total = totals.length
        ? totals[Math.floor(totals.length / 2)]
        : null;

      // Sample of bookmakers (book list — useful for "where to bet")
      const books = (g.bookmakers || []).slice(0, 8).map(b => b.key);

      return {
        commence_time: g.commence_time,
        away_team:     g.away_team,
        home_team:     g.home_team,
        away_ml_price: away_ml.price,
        away_ml_book:  away_ml.book,
        home_ml_price: home_ml.price,
        home_ml_book:  home_ml.book,
        home_spread:   median_spread,
        total:         median_total,
        book_count:    h2h_quotes.length,
        books,
      };
    });
  } catch (_) {
    return [];
  }
}

// ── Player spotlight ─────────────────────────────────────────
// Aggregate the most notable players right now from two sources:
//   1) Injury wire (player + status + team)
//   2) News mentions (athletes ESPN explicitly tagged in articles)
// De-dup by athlete_id, attach the latest related news headline + url.
function buildPlayerSpotlight(leagueResults, allNews) {
  const byId = new Map();   // athlete_id → player record

  // Pass 1 — injured players go in first (these have the most context)
  for (const [lgKey, lg] of leagueResults) {
    const lgLabel = lg.label || lgKey.toUpperCase();
    for (const inj of (lg.injuries || [])) {
      if (!inj.athlete) continue;
      const key = inj.athlete_id || (inj.team + ":" + inj.athlete);
      if (byId.has(key)) continue;
      byId.set(key, {
        id:        inj.athlete_id,
        name:      inj.athlete,
        team:      inj.team,
        team_name: inj.team_name,
        league:    lgLabel,
        position:  inj.position,
        status:    inj.status,
        short_desc: inj.short_desc,
        headshot:  inj.headshot,
        mentions:  [],
        kind:      "injury",
      });
    }
  }

  // Pass 2 — news mentions. If we already have the player, append the
  // article. Otherwise, add the player from news context.
  for (const n of allNews) {
    for (const mp of (n.mentions_players || [])) {
      const key = mp.id || mp.name;
      if (!byId.has(key)) {
        byId.set(key, {
          id:        mp.id,
          name:      mp.name,
          team:      (n.mentions_teams && n.mentions_teams[0] && n.mentions_teams[0].abbr) || "",
          team_name: (n.mentions_teams && n.mentions_teams[0] && n.mentions_teams[0].name) || "",
          league:    n.league,
          status:    "",
          short_desc: "",
          headshot:  "",
          mentions:  [],
          kind:      "news",
        });
      }
      const p = byId.get(key);
      if (p.mentions.length < 4) {
        p.mentions.push({
          headline:  n.headline,
          published: n.published,
          url:       n.url,
        });
      }
    }
  }

  // Rank: any player with news mentions > any without; within those,
  // more mentions = higher. Cap at 18.
  return Array.from(byId.values())
    .sort((a, b) => (b.mentions.length - a.mentions.length))
    .slice(0, 18);
}

// ── Build the full payload ─────────────────────────────────────
async function buildPayload() {
  const t0 = Date.now();
  const oddsKey = process.env.ODDS_API_KEY || "";

  const leagueResults = await Promise.all(LEAGUES.map(async lg => {
    const [scoreboard, injuries, odds, standings] = await Promise.all([
      fetchEspnScoreboard(lg.espn),
      fetchEspnInjuries(lg.espn),
      fetchOdds(lg.odds, oddsKey),
      fetchEspnStandings(lg.espn),
    ]);
    return [lg.key, { label: lg.label, scoreboard, injuries, odds, standings }];
  }));

  // News: pull from each league's news feed, interleave by published time
  const newsLists = await Promise.all(LEAGUES.map(lg => fetchEspnNews(lg.espn)));
  const allNews = [];
  newsLists.forEach((items, i) => {
    const lg = LEAGUES[i];
    for (const a of items) allNews.push({ ...a, league: lg.label });
  });
  allNews.sort((a, b) => new Date(b.published) - new Date(a.published));

  // Player spotlight — needs to read both injuries (per-league) and news
  const players = buildPlayerSpotlight(leagueResults, allNews);

  const leagues = Object.fromEntries(leagueResults);

  return {
    generated_at: new Date().toISOString(),
    leagues,
    news: allNews.slice(0, 40),
    players,
    analysis: readPicks(),
    meta: {
      sources:    ["ESPN", "The Odds API"],
      latency_ms: Date.now() - t0,
      cached:     false,
    },
  };
}

// ── Handler ────────────────────────────────────────────────────
module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin",  "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")     return res.status(405).json({ error: "GET only" });

  const ip = (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (!rateLimit(ip)) {
    return res.status(429).json({ error: "rate limit exceeded" });
  }

  const now = Date.now();
  if (CACHE.payload && (now - CACHE.at) < CACHE_TTL_MS) {
    res.setHeader("X-Terminal-Cache", "HIT");
    // Mutate meta.cached without clobbering CACHE.payload
    const out = { ...CACHE.payload, meta: { ...(CACHE.payload.meta || {}), cached: true, age_ms: now - CACHE.at } };
    return res.status(200).json(out);
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
