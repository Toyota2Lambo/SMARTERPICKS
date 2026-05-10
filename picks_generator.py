# ============================================================
# SMARTERPICKS - Daily Picks Generator
# ============================================================
# What this script does:
#   1. Pulls today's games and odds from The Odds API
#   2. Sends that data to Claude AI to generate picks
#   3. Saves the picks to a file called picks.json
#   4. Your website reads picks.json and shows the picks automatically
#
# You NEVER need to edit this file.
# To run it manually, see the README.
# ============================================================

import requests
import json
import os
import re
import sys
from datetime import datetime
from statistics import median

# ── API KEYS ──────────────────────────────────────────────
# These come from your GitHub Secrets (explained in README)
# You never type your keys directly in this file — that would
# expose them publicly. GitHub keeps them hidden and secure.
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── SETTINGS YOU CAN CHANGE ───────────────────────────────
# Which sports to cover. Remove any you don't want.
SPORTS = [
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "americanfootball_nfl",
]

# Pick-count is adaptive — see target_pick_count() below. We cap the
# slate at this ceiling on the busiest days; on light days we generate
# fewer high-confidence plays rather than padding to fill a quota.
MAX_PICKS = 7
MIN_PICKS = 3

# ── EV / CONSENSUS HELPERS ────────────────────────────────
# When we ship games to Claude, we also ship a per-market consensus
# summary so Claude can avoid "value plays" that aren't actually value.
# A pick at -110 isn't value if every book on earth has it at -110;
# it's only value if the cited book is meaningfully better than the
# median across books.

def american_to_implied(odds: int) -> float:
    """American odds → implied win probability (0-1)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def compute_market_consensus(game: dict) -> dict:
    """Walk every bookmaker for a game and, per market+outcome, build
    {best_price, best_book, median_price, edge_bps}. Returns a dict
    keyed by market_key → outcome_name → consensus.
    edge_bps = how many basis points of implied-probability the BEST
    price beats the MEDIAN by. Positive bps = the best book is offering
    a meaningfully better number; <50 bps means there's basically no
    line to exploit."""
    out = {}
    for bk in game.get("bookmakers", []) or []:
        book_title = bk.get("title") or bk.get("key") or "?"
        for mkt in bk.get("markets", []) or []:
            mkey = mkt.get("key")  # h2h | spreads | totals
            if not mkey:
                continue
            for oc in mkt.get("outcomes", []) or []:
                price = oc.get("price")
                if price is None:
                    continue
                # Outcome name + (point if present) keys it uniquely:
                # spread/total outcomes share names, point disambiguates.
                point = oc.get("point")
                key = (oc.get("name", "?"), point) if point is not None else (oc.get("name", "?"),)
                out.setdefault(mkey, {}).setdefault(key, []).append((int(price), book_title))

    summary = {}
    for mkey, outcomes in out.items():
        summary[mkey] = {}
        for key, prices in outcomes.items():
            if not prices:
                continue
            # Best = most generous to the bettor (highest American odds value
            # when treating positive as positive, negative as less-negative).
            best_price, best_book = max(prices, key=lambda p: american_to_implied(p[0]) * -1)
            med = int(round(median(p[0] for p in prices)))
            edge = (american_to_implied(med) - american_to_implied(best_price)) * 10000  # in bps
            summary[mkey]["__".join(str(k) for k in key)] = {
                "best": best_price,
                "best_book": best_book,
                "median": med,
                "edge_bps": round(edge, 1),
                "n_books": len(prices),
            }
    return summary


# ── ESPN CONTEXT ENRICHMENT ───────────────────────────────
# ESPN exposes undocumented JSON endpoints for scoreboards and league
# news. We pull two things best-effort: today's MLB starting pitchers
# (the single most predictive signal in MLB betting) and a short
# league-wide injury feed for NBA/NHL. Failures are non-fatal — the
# generator works without enrichment, just less informed.

ESPN_TIMEOUT = 8

def _espn_get(url: str, params: dict | None = None):
    try:
        r = requests.get(url, params=params or {}, timeout=ESPN_TIMEOUT,
                         headers={"User-Agent": "SmarterPicks/1.0"})
        if r.ok:
            return r.json()
        print(f"   ⚠ ESPN {r.status_code} for {url}")
    except Exception as e:
        print(f"   ⚠ ESPN fetch failed ({e}) for {url}")
    return None


def fetch_espn_context(games: list) -> dict:
    """Returns {team_name_lower: 'short context string'} merged across
    sources. Used downstream to annotate each game line in the prompt."""
    ctx: dict[str, list[str]] = {}
    sport_keys = {g.get("sport_key") for g in games}

    # ── MLB starters ──
    if "baseball_mlb" in sport_keys:
        today = datetime.now().strftime("%Y%m%d")
        data = _espn_get(
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
            params={"dates": today},
        )
        for ev in (data or {}).get("events", []) or []:
            for comp in (ev.get("competitions") or [{}])[0].get("competitors", []):
                team_name = (comp.get("team", {}).get("displayName") or "").lower()
                probables = comp.get("probables") or []
                if not team_name or not probables:
                    continue
                ath = probables[0].get("athlete", {})
                name = ath.get("displayName")
                if not name:
                    continue
                # Pull ERA from the pitcher's recent stats if present
                era = None
                for s in (ath.get("statistics") or []):
                    if s.get("name", "").lower() == "era" and s.get("value") is not None:
                        era = s["value"]
                        break
                line = f"Probable starter: {name}" + (f" (ERA {era})" if era else "")
                ctx.setdefault(team_name, []).append(line)

    # ── NBA / NHL injuries (league-wide feed) ──
    league_paths = []
    if "basketball_nba" in sport_keys:
        league_paths.append(("basketball/nba", "NBA"))
    if "icehockey_nhl" in sport_keys:
        league_paths.append(("hockey/nhl", "NHL"))

    for path, label in league_paths:
        data = _espn_get(f"https://site.api.espn.com/apis/site/v2/sports/{path}/news/injuries")
        for art in (data or {}).get("articles", []) or []:
            # ESPN injury articles describe individual player situations.
            # We pull the team name and a single-line summary. Best-effort.
            cats = art.get("categories", []) or []
            team_name = next(
                (c.get("team", {}).get("displayName", "").lower()
                 for c in cats if c.get("type") == "team" and c.get("team")),
                None,
            )
            headline = (art.get("headline") or art.get("description") or "").strip()
            if team_name and headline:
                ctx.setdefault(team_name, []).append(f"[{label} injury] {headline}")

    # Cap each team's context at 3 lines so the prompt doesn't bloat
    return {team: " | ".join(lines[:3]) for team, lines in ctx.items()}


def annotate_game_with_context(game: dict, espn_ctx: dict) -> str:
    """Build a single short context line for a game by looking up
    home + away team names in the ESPN context map. Returns "" if no
    context was found."""
    bits = []
    for team_field in ("home_team", "away_team"):
        name = (game.get(team_field) or "").lower()
        # ESPN team names sometimes differ from Odds API ("Yankees" vs
        # "New York Yankees"). Try direct match, then suffix match.
        match = espn_ctx.get(name)
        if not match:
            match = next((v for k, v in espn_ctx.items() if name in k or k in name), None)
        if match:
            bits.append(f"{game.get(team_field)}: {match}")
    return " || ".join(bits)


def target_pick_count(num_games: int) -> int:
    """Slate-aware cap. Forcing 7 picks on a 5-game Tuesday is how you
    end up with a card full of C+ filler. Roughly: ~1 pick per 4 games,
    bounded by [MIN_PICKS, MAX_PICKS]. Claude is also allowed to return
    FEWER than this if it can't honestly identify that many plays it
    likes."""
    if num_games <= 0:
        return MIN_PICKS
    return max(MIN_PICKS, min(MAX_PICKS, num_games // 4 + 2))

# Your brand name (shows up in the script logs)
BRAND_NAME = "SMARTERPICKS"

# ── STEP 1: FETCH TODAY'S GAMES ───────────────────────────
def get_todays_games():
    """
    Calls The Odds API to get today's games and current betting lines.
    Returns a list of games with home/away teams and odds.
    """
    print("📡 Fetching today's games from The Odds API...")

    all_games = []

    for sport in SPORTS:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
        params = {
            "apiKey":      ODDS_API_KEY,
            "regions":     "us",
            "markets":     "h2h,spreads,totals",
            "oddsFormat":  "american",
            "dateFormat":  "iso",
        }

        try:
            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                games = response.json()
                all_games.extend(games)
                print(f"   ✓ {sport}: {len(games)} games found")
            elif response.status_code == 401:
                print(f"   ✗ {sport}: Bad API key — check your ODDS_API_KEY secret")
            elif response.status_code == 422:
                print(f"   ✗ {sport}: No games today for this sport (that's fine)")
            else:
                print(f"   ✗ {sport}: Error {response.status_code}")

        except Exception as e:
            print(f"   ✗ {sport}: Connection error — {e}")

    print(f"\n📊 Total games found today: {len(all_games)}")
    return all_games


# ── STEP 2: GENERATE PICKS WITH CLAUDE ────────────────────
def generate_picks(games):
    """
    Sends the games and odds to Claude AI.
    Claude analyzes the data and returns structured pick recommendations.
    """
    print("\n🤖 Sending data to Claude AI to generate picks...")

    # Send up to 20 games to Claude (was 15) so on big slates there's
    # more material to filter from. Adaptive pick-count + tighter rules
    # below mean we don't pad — we just have a wider candidate pool.
    games_for_prompt = games[:20] if len(games) > 20 else games

    # Pre-compute per-market consensus across books for each game.
    # Claude sees this annotated next to the raw game so it can avoid
    # picking sides where the cited book isn't actually beating the
    # market. Full odds objects stay available too — consensus is
    # additive, not a replacement.
    print("   📐 Computing per-market consensus across books…")
    enriched = []
    for g in games_for_prompt:
        consensus = compute_market_consensus(g)
        enriched.append({**g, "_consensus": consensus})

    # Best-effort ESPN enrichment — MLB starters + NBA/NHL injury notes.
    # Attached as a separate "context" block in the prompt because
    # cramming it inside each game object made the prompt noisy.
    print("   📡 Fetching ESPN context (starters, injuries)…")
    espn_ctx = fetch_espn_context(games_for_prompt)
    context_lines = []
    for g in games_for_prompt:
        line = annotate_game_with_context(g, espn_ctx)
        if line:
            context_lines.append(f"- {line}")
    context_block = (
        "\n".join(context_lines) if context_lines
        else "(no ESPN context available — pick on odds alone, with extra caution)"
    )
    print(f"   ✓ ESPN context: {len(context_lines)} games annotated")

    games_text = json.dumps(enriched, indent=2)

    # Adaptive ceiling: 5-game slate caps at 3 picks, 28+ games caps at 7.
    pick_target = target_pick_count(len(games))

    # ── THE PROMPT ──
    # This is the instruction we give Claude.
    # You can edit the text inside the triple quotes to change
    # the style, tone, or focus of the picks.
    prompt = f"""You are a sharp sports betting analyst for {BRAND_NAME}.
Today is {datetime.now().strftime("%A, %B %d, %Y")}.

Here is today's live odds data from sportsbooks ({len(games_for_prompt)} games).
Each game object includes a "_consensus" field we computed locally:
  - For each market+outcome it gives {{best, best_book, median, edge_bps, n_books}}
  - "best" = most generous American odds across all books
  - "median" = the median price across all books offering that side
  - "edge_bps" = how many basis points of implied probability the BEST beats the MEDIAN.
                edge_bps < 50 means there is essentially NO line value — every book is in
                agreement on that number, so pretending to find an edge there is a fiction.

{games_text}

ADDITIONAL CONTEXT — pulled live from ESPN (probable starters, injuries):
{context_block}

Generate UP TO {pick_target} betting picks from this data — but it is BETTER to return
fewer high-quality picks than to pad the card with weak plays. Quality over quantity.
A {pick_target}-pick day where every play is genuinely Confidence B or higher beats a
7-pick day with three C+ filler plays.

Pick-quality rules (all enforced — a pick that violates these will hurt the track record):
- Only pick games that are actually in the data above.
- USE the "_consensus" field. A pick is only worth posting if "edge_bps" for that side
  is at least 100 (1% of implied probability) — preferably 200+. If every book is within
  a tick of the median, there is no edge to exploit. Skip it.
- USE the ESPN context block above. If a probable MLB starter has a high ERA, that is
  relevant to a totals pick. If a star NBA player is listed injured, that changes the
  spread analysis. Reference these specifically in your reasoning when applicable.
- AT MOST ONE pick may have odds shorter than -150 (heavy moneyline favorites).
  These crater the unit math when they lose. One per day, max — and only if the
  edge is genuine (edge_bps ≥ 200), not because "they should win".
- Avoid stacking the card with a single bet type. No more than 2 totals (Over/Under)
  in a row, no more than 3 moneylines total.
- Pick strings must be CLEAN. Just the side: "Lakers +5.5", "Under 7.5", "Cardinals ML".
  Do NOT add parenthetical commentary inside the pick string (e.g. "Under 7.5 (Yamamoto pitching)")
  — put that in the reasoning field instead. Parentheticals break our automated scorer.
- Cite the "best_book" from the consensus block as the book in your pick. Do not invent
  a book name not present in the data.

Output rules:
- Be specific — reference actual numbers, spreads, and totals from the data
- Reasoning should be 2-3 sentences, analytical, and reference the odds or line
- Confidence ratings: A (very confident), B+ (confident), B (solid), C+ (speculative).
  If a candidate would only rate C+, prefer to drop it entirely.
- Stake sizes: 2u (strong), 1.5u (moderate), 1u (small/speculative)
- The first pick (index 0) has is_premium set to FALSE — this is the free pick
- All other picks have is_premium set to TRUE — these are locked for subscribers
- Mix different sports and bet types (spreads, totals, moneylines, player props)

Return ONLY a valid JSON object with this exact structure, no other text before or after:

{{
  "date": "{datetime.now().strftime("%B %d, %Y")}",
  "date_short": "{datetime.now().strftime("%m-%d")}",
  "sport_summary": "Brief 1-line summary of today's slate (e.g. 'NBA Playoffs Game 6 headlining a 12-game Friday slate')",
  "picks": [
    {{
      "league": "NBA · Playoffs",
      "time": "7:30 PM ET",
      "away_team": "Team Name",
      "home_team": "Team Name",
      "game_detail": "Game 6 · Series tied 2-2",
      "pick": "Lakers +5.5",
      "odds": "-110",
      "book": "DraftKings",
      "stake": "2u",
      "reasoning": "2-3 sentence analysis referencing specific data from the odds.",
      "tags": ["Line Move", "Situational", "Confidence B+"],
      "is_premium": false
    }}
  ]
}}

Important: Return ONLY the JSON. No markdown, no backticks, no explanation."""

    # Call the Claude API
    try:
        import urllib.request

        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            }
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        raw_text = result["content"][0]["text"].strip()

        # Clean up in case Claude added markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        data = json.loads(raw_text)
        picks = data.get("picks", [])

        print(f"   ✓ Claude generated {len(picks)} picks")
        return data

    except Exception as e:
        print(f"   ✗ Claude API error: {e}")
        print("   → Falling back to manual picks template...")
        return get_manual_fallback()


# ── MANUAL FALLBACK ────────────────────────────────────────
# If the API fails, the site uses this template instead of breaking.
# You or an agent can also edit this directly to manually set picks.
def get_manual_fallback():
    """
    Returns a basic template when the API is unavailable.
    Edit this if you ever want to manually set picks without running the script.
    """
    return {
        "date": datetime.now().strftime("%B %d, %Y"),
        "date_short": datetime.now().strftime("%m-%d"),
        "sport_summary": "Today's picks are being updated — check back shortly.",
        "picks": [
            {
                "league": "Coming Soon",
                "time": "TBD",
                "away_team": "Away",
                "home_team": "Home",
                "game_detail": "Picks updating...",
                "pick": "Pick loading",
                "odds": "—",
                "book": "—",
                "stake": "—",
                "reasoning": "Today's picks are being generated. Refresh the page in a few minutes.",
                "tags": ["Updating"],
                "is_premium": False,
            }
        ]
    }


# ── STEP 3: SAVE TO PICKS.JSON ────────────────────────────
def save_picks(data):
    """
    Saves the picks data to picks.json.
    Your website reads this file to show the picks.
    """
    # Add a timestamp so you can see when it was last updated
    data["generated_at"] = datetime.now().isoformat()
    data["generated_at_readable"] = datetime.now().strftime("%B %d, %Y at %I:%M %p ET")

    with open("picks.json", "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n✅ picks.json saved successfully!")
    print(f"   Date: {data['date']}")
    print(f"   Picks: {len(data['picks'])}")
    print(f"   Free picks: {len([p for p in data['picks'] if not p['is_premium']])}")
    print(f"   Premium picks: {len([p for p in data['picks'] if p['is_premium']])}")


# ============================================================
# RESULTS SCORING — runs at the start of each daily run.
# Reads yesterday's picks.json, fetches final scores from the
# Odds API, scores each pick (W/L/PUSH/PENDING), writes
# results.json (yesterday's results card) and appends to
# history.json (cumulative chart + profit calculator).
# ============================================================

def team_in(needle, haystack):
    """Loose case-insensitive name match. 'Knicks' matches 'New York Knicks'."""
    if not needle or not haystack:
        return False
    a, b = needle.lower(), haystack.lower()
    return a in b or b in a

def find_game_by_teams(away, home, scores):
    """Find a game in the Odds API /scores response by team names."""
    for g in scores:
        ht = g.get("home_team", "")
        at = g.get("away_team", "")
        if team_in(home, ht) and team_in(away, at):
            return g
    return None

def parse_stake(s):
    """'1.5u' -> 1.5; defaults to 1.0 if unparseable."""
    m = re.match(r"(\d+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1)) if m else 1.0

def american_payout(odds_str):
    """Units WON per unit risked at the given American odds. -110 -> 0.909, +120 -> 1.20."""
    s = str(odds_str or "").strip().replace("+", "")
    try:
        n = int(float(s))
    except ValueError:
        return 1.0
    if n > 0:
        return n / 100.0
    if n < 0:
        return 100.0 / abs(n)
    return 1.0

def get_score(game, team_name):
    for s in (game.get("scores") or []):
        if s.get("name") == team_name:
            try:
                return int(s.get("score"))
            except (TypeError, ValueError):
                return None
    return None

def score_pick_string(pick_str, game, home_score, away_score):
    """Returns 'WON' / 'LOST' / 'PUSH' / 'PENDING' for spreads, MLs, totals.
    Player props / parlays / puck lines fall through to PENDING (manual)."""
    s = (pick_str or "").strip()
    # Strip parenthetical commentary BEFORE regex matching so picks like
    # "Under 7.5 (Yamamoto pitching)" or "Lakers -3.5 (best line at DK)"
    # still match the canonical "<side> <number>" patterns below. The
    # generator prompt also tells Claude not to write parentheticals,
    # but defense-in-depth: when a pending-scorer pick blows up the
    # results page, the parser should be the second line of defense.
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    home, away = game.get("home_team", ""), game.get("away_team", "")

    # Total: "Over 214.5", "Under 7.5"
    m = re.match(r"^(over|under)\s+(\d+(?:\.\d+)?)$", s, re.I)
    if m:
        side, total = m.group(1).lower(), float(m.group(2))
        actual = home_score + away_score
        if actual == total: return "PUSH"
        return "WON" if (side == "over") == (actual > total) else "LOST"

    # Moneyline: "Cardinals ML"
    m = re.match(r"^(.+?)\s+ml$", s, re.I)
    if m:
        name = m.group(1).strip()
        if home_score == away_score: return "PUSH"
        if team_in(name, home): return "WON" if home_score > away_score else "LOST"
        if team_in(name, away): return "WON" if away_score > home_score else "LOST"
        return "PENDING"

    # Spread: "Knicks +6.5", "Lakers -3.5"
    m = re.match(r"^(.+?)\s+([+-]\d+(?:\.\d+)?)$", s)
    if m:
        name, spread = m.group(1).strip(), float(m.group(2))
        if team_in(name, home):
            adj = home_score + spread
            if adj == away_score: return "PUSH"
            return "WON" if adj > away_score else "LOST"
        if team_in(name, away):
            adj = away_score + spread
            if adj == home_score: return "PUSH"
            return "WON" if adj > home_score else "LOST"
        return "PENDING"

    # Anything else (player props, parlays, puck lines): need a human.
    return "PENDING"

def score_one_pick(pick, all_scores):
    out = dict(pick)
    game = find_game_by_teams(pick.get("away_team"), pick.get("home_team"), all_scores)
    if not game or not game.get("completed"):
        out["result"], out["units"] = "PENDING", 0.0
        return out

    home_score = get_score(game, game.get("home_team", ""))
    away_score = get_score(game, game.get("away_team", ""))
    if home_score is None or away_score is None:
        out["result"], out["units"] = "PENDING", 0.0
        return out

    result = score_pick_string(pick.get("pick", ""), game, home_score, away_score)
    stake = parse_stake(pick.get("stake"))
    if   result == "WON":  units = round(stake * american_payout(pick.get("odds")), 2)
    elif result == "LOST": units = round(-stake, 2)
    else:                  units = 0.0
    out["result"], out["units"] = result, units
    return out

def fetch_completed_scores():
    """Pull last 2 days of completed scores across all configured sports."""
    if not ODDS_API_KEY:
        return []
    rows = []
    for sport in SPORTS:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/scores",
                params={"apiKey": ODDS_API_KEY, "daysFrom": 2},
                timeout=10,
            )
            if r.status_code == 200:
                rows.extend(r.json())
            else:
                print(f"   ✗ scores {sport}: HTTP {r.status_code}")
        except Exception as e:
            print(f"   ✗ scores {sport}: {e}")
    return rows

def update_history_for_yesterday(date_str, net_units, wins, losses, pushes):
    """Append yesterday's net into history.json and bump cumulative + stats.
    No-ops if we've already recorded that date."""
    history = {"stats": {}, "daily": []}
    if os.path.exists("history.json"):
        try:
            with open("history.json") as f:
                history = json.load(f)
        except Exception as e:
            print(f"   ⚠ couldn't read history.json: {e}")

    iso = None
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            iso = datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            break
        except (TypeError, ValueError):
            continue
    if not iso:
        iso = datetime.now().strftime("%Y-%m-%d")

    daily = history.get("daily", [])
    if any(d.get("date") == iso for d in daily):
        print(f"   {iso} already in history — skipping append")
        return

    last_cum = daily[-1]["cumulative"] if daily else 0.0
    daily.append({"date": iso, "units": net_units, "cumulative": round(last_cum + net_units, 2)})

    stats = history.get("stats", {}) or {}
    stats["wins"]      = stats.get("wins", 0) + wins
    stats["losses"]    = stats.get("losses", 0) + losses
    stats["pushes"]    = stats.get("pushes", 0) + pushes
    stats["total_picks"] = stats.get("total_picks", 0) + wins + losses + pushes
    stats["net_units"]  = round(daily[-1]["cumulative"], 2)
    stats["days_recorded"] = len(daily)
    if stats["wins"] + stats["losses"]:
        stats["win_pct"] = round(100 * stats["wins"] / (stats["wins"] + stats["losses"]), 1)
    if daily:
        stats["avg_daily_units"] = round(sum(d["units"] for d in daily) / len(daily), 2)
    history["stats"] = stats
    history["daily"] = daily

    with open("history.json", "w") as f:
        json.dump(history, f, indent=2)
    print(f"   ✅ history.json updated ({iso}: {net_units:+.2f}u)")

def append_to_archive(results):
    """Append a day's full per-pick results to archive.json so the
    public archive page can show every pick we've ever made.

    Idempotent: keyed off iso_date so re-runs of the same day don't
    duplicate. Stats block is mirrored from history.json each time
    so the two files can never disagree.
    """
    archive = {"stats": {}, "days": []}
    if os.path.exists("archive.json"):
        try:
            with open("archive.json") as f:
                archive = json.load(f)
        except Exception as e:
            print(f"   ⚠ couldn't read archive.json: {e}")

    date_str = results.get("date") or ""
    iso = None
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            iso = datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            break
        except (TypeError, ValueError):
            continue
    if not iso:
        iso = datetime.now().strftime("%Y-%m-%d")

    days = archive.get("days", [])
    if any(d.get("iso_date") == iso for d in days):
        print(f"   {iso} already in archive.json — skipping append")
        return

    # Trim each pick to fields the archive page renders. Reasoning is
    # kept so members can cross-check the rationale we posted that day.
    trimmed_picks = []
    for p in results.get("picks", []) or []:
        trimmed_picks.append({
            "league":     p.get("league"),
            "away_team":  p.get("away_team"),
            "home_team":  p.get("home_team"),
            "pick":       p.get("pick"),
            "odds":       p.get("odds"),
            "stake":      p.get("stake"),
            "result":     p.get("result"),
            "units":      p.get("units"),
            "reasoning":  p.get("reasoning"),
        })

    days.insert(0, {
        "iso_date":     iso,
        "date_display": results.get("date") or iso,
        "wins":         results.get("wins", 0),
        "losses":       results.get("losses", 0),
        "pushes":       results.get("pushes", 0),
        "net_units":    results.get("net_units", 0.0),
        "summary_text": results.get("summary_text", ""),
        "picks":        trimmed_picks,
    })

    # Mirror history.json stats so the archive page's headline numbers
    # match what the chart on the members page is reading from.
    if os.path.exists("history.json"):
        try:
            with open("history.json") as f:
                hist_stats = json.load(f).get("stats") or {}
            archive["stats"] = hist_stats
        except Exception as e:
            print(f"   ⚠ couldn't mirror history stats into archive: {e}")
    archive["days"] = days

    with open("archive.json", "w") as f:
        json.dump(archive, f, indent=2)
    print(f"   ✅ archive.json updated ({iso}: {len(trimmed_picks)} picks)")


def score_and_archive_yesterday():
    """Read existing picks.json, score each pick using yesterday's final
    scores from the Odds API, write results.json + update history.json."""
    if not os.path.exists("picks.json"):
        print("📊 No previous picks.json found — first run, skipping scoring")
        return
    try:
        with open("picks.json") as f:
            old = json.load(f)
    except Exception as e:
        print(f"📊 Couldn't read picks.json ({e}) — skipping scoring")
        return

    picks = old.get("picks") or []
    if not picks or picks[0].get("league") == "Coming Soon":
        print("📊 Previous picks.json was the fallback template — skipping scoring")
        return

    print(f"\n📊 Scoring yesterday's picks ({old.get('date', 'unknown date')})...")
    all_scores = fetch_completed_scores()
    print(f"   Pulled {len(all_scores)} completed games from Odds API")

    scored = [score_one_pick(p, all_scores) for p in picks]
    wins   = sum(1 for p in scored if p["result"] == "WON")
    losses = sum(1 for p in scored if p["result"] == "LOST")
    pushes = sum(1 for p in scored if p["result"] == "PUSH")
    pending = sum(1 for p in scored if p["result"] == "PENDING")
    net    = round(sum(p["units"] for p in scored), 2)

    summary = f"{wins}-{losses}"
    if pushes: summary += f"-{pushes}"
    summary += f" · {('+' if net >= 0 else '')}{net}u"
    if pending: summary += f"  ({pending} pending manual scoring)"

    results = {
        "date":         old.get("date"),
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "net_units":    net,
        "summary_text": summary,
        "picks":        scored,
    }
    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"   ✅ results.json saved: {summary}")

    # Only count fully-scored days against history (so a slate full of
    # PENDING props doesn't pollute the cumulative chart).
    if pending == 0 or wins + losses + pushes > 0:
        update_history_for_yesterday(old.get("date"), net, wins, losses, pushes)
        # Per-pick archive happens on the same condition so archive
        # and history can never drift out of sync.
        try:
            append_to_archive(results)
        except Exception as e:
            print(f"   ⚠ archive append failed: {e}")
    else:
        print("   ⚠ all picks pending — skipping history append")


# ── MAIN: RUN EVERYTHING ──────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  {BRAND_NAME} — Daily Picks Generator")
    print(f"  {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}")
    print(f"{'='*50}\n")

    # Check API keys are set
    if not ODDS_API_KEY:
        print("⚠️  WARNING: ODDS_API_KEY not found in environment variables")
        print("   Using Claude only with limited game data\n")

    if not ANTHROPIC_API_KEY:
        print("❌ ERROR: ANTHROPIC_API_KEY not found — cannot generate picks")
        print("   Check your GitHub Secrets (see README Step 4)")
        sys.exit(1)

    # Step 0: score yesterday's picks BEFORE we overwrite picks.json
    try:
        score_and_archive_yesterday()
    except Exception as e:
        print(f"⚠ Scoring step failed: {e}")
        print("   Continuing with today's pick generation anyway.")

    # Step 1: generate today's new card.
    #
    # Refusing to call Claude on an empty games list is important: with
    # nothing to ground on, Claude will happily fabricate picks for
    # games that don't exist. Better to ship the "Coming Soon" template
    # so the site clearly shows we don't have data, and exit non-zero
    # so the workflow's failure-notification step fires.
    games = get_todays_games() if ODDS_API_KEY else []
    if not games:
        print("\n❌ No games returned from The Odds API.")
        print("   This usually means: API key is invalid, daily quota is")
        print("   exhausted, or every configured sport's season is dark.")
        print("   Skipping Claude call — would otherwise fabricate picks.")
        print("   Saving the fallback template so the site shows 'updating'.")
        save_picks(get_manual_fallback())
        sys.exit(2)
    data  = generate_picks(games)
    save_picks(data)

    print(f"\n{'='*50}")
    print(f"  Done! Your site will update automatically.")
    print(f"{'='*50}\n")
