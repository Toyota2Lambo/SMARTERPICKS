"""
SMARTERPICKS — Score yesterday's picks (auto-runs before today's generator)
==========================================================================
Reads the current picks.json (= yesterday's slate, since today's hasn't
been generated yet), fetches final scores from The Odds API for each
game, asks Claude to determine W/L + unit P/L per pick, then writes
results.json and appends one entry to history.json.

The script is idempotent: if picks.json's date is already today, it
skips. If a pick can't be scored (game not found, player prop the
Odds API doesn't cover), it's marked "PENDING" with 0 units rather
than guessed.

Required env vars (same as picks_generator.py):
  ODDS_API_KEY        — from the-odds-api.com
  ANTHROPIC_API_KEY   — from console.anthropic.com
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT         = Path(__file__).resolve().parent
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_ID          = "claude-sonnet-4-6"

# Eastern Time (matches picks_generator.now_et). Date strings the site
# displays go through ET; using UTC here was 4-5 hours ahead during EDT
# and caused late-night runs to think it was already tomorrow.
ET = ZoneInfo("America/New_York")


# ── league string → Odds API sport key ─────────────────────────
def sport_key_for(league_str: str):
    s = (league_str or "").lower()
    if "nba" in s or "basketball" in s: return "basketball_nba"
    if "mlb" in s or "baseball"   in s: return "baseball_mlb"
    if "nhl" in s or "hockey"     in s: return "icehockey_nhl"
    if "nfl" in s or "football"   in s: return "americanfootball_nfl"
    return None


# ── Odds API: yesterday's completed scores for a given sport ──
def fetch_scores(sport_key: str):
    qs = urllib.parse.urlencode({"apiKey": ODDS_API_KEY, "daysFrom": "2"})
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"   ✗ {sport_key}: {e}", file=sys.stderr)
        return []


# ── match a game from the scores list by team names ─────────────
def find_game(scores, away_team: str, home_team: str):
    a = (away_team or "").lower()
    h = (home_team or "").lower()
    def tokens(name): return [w for w in re.split(r"\s+", name) if len(w) > 3]
    a_tokens, h_tokens = tokens(a), tokens(h)

    for g in scores:
        if not g.get("completed"):
            continue
        ga = g.get("away_team", "").lower()
        gh = g.get("home_team", "").lower()
        # match: any meaningful token from our pick appears in the api game name
        am = any(t in ga for t in a_tokens) or a in ga or ga in a
        hm = any(t in gh for t in h_tokens) or h in gh or gh in h
        if am and hm:
            return g
    return None


# ── ask Claude to score one pick given the final score ──────────
def score_pick_with_claude(pick: dict, away_score: int, home_score: int):
    if not ANTHROPIC_API_KEY:
        return ("PENDING", 0.0)

    prompt = (
        "Score this sports betting pick based on the final game score.\n\n"
        f"PICK:\n"
        f"  League: {pick.get('league')}\n"
        f"  Game:   {pick.get('away_team')} at {pick.get('home_team')}\n"
        f"  Detail: {pick.get('game_detail', '')}\n"
        f"  Pick:   {pick.get('pick')}\n"
        f"  Odds:   {pick.get('odds')}\n"
        f"  Stake:  {pick.get('stake')}\n\n"
        f"FINAL SCORE:\n"
        f"  {pick.get('away_team')}: {away_score}\n"
        f"  {pick.get('home_team')}: {home_score}\n\n"
        "DETERMINE:\n"
        "1. Did the pick WIN, LOSE, or PUSH? (PUSH if a spread or total lands exactly on the line.)\n"
        "2. Unit P/L using American odds math:\n"
        "     WON at -110, stake 1u  -> units = 1.0 * (100/110) ~= +0.91\n"
        "     WON at +135, stake 1u  -> units = 1.0 * (135/100)  =  +1.35\n"
        "     LOST any odds          -> units = -stake\n"
        "     PUSH                   -> units =  0\n"
        "3. If this is a PLAYER PROP whose outcome cannot be determined from team scores alone,\n"
        "   return 'PENDING' with units = 0.\n\n"
        "Return ONLY this exact JSON (no markdown, no commentary):\n"
        '{"result":"WON|LOST|PUSH|PENDING","units":<number>}\n'
    )

    payload = json.dumps({
        "model": MODEL_ID,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["content"][0]["text"].strip()
        # tolerate accidental markdown fencing
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        return (parsed.get("result", "PENDING"), float(parsed.get("units", 0.0)))
    except Exception as e:
        print(f"   ✗ Claude scoring error: {e}", file=sys.stderr)
        return ("PENDING", 0.0)


# ── main ────────────────────────────────────────────────────────
def main():
    today_full  = datetime.now(ET).strftime("%B %d, %Y")
    picks_path  = REPO_ROOT / "picks.json"

    if not picks_path.exists():
        print("⚠ picks.json not found — nothing to score.")
        return 0

    picks_data = json.loads(picks_path.read_text())
    pick_date  = picks_data.get("date", "")

    # Skip if the file is already today's — means we're scoring before the
    # generator overwrites yesterday's data. If it's today, someone already
    # ran the generator for today (out-of-order or rerun); nothing to score.
    if pick_date == today_full:
        print(f"ℹ picks.json date is today ({today_full}). Nothing yesterday to score.")
        return 0

    picks = picks_data.get("picks") or []
    if not picks:
        print(f"⚠ picks.json for {pick_date} has no picks. Skipping.")
        return 0

    print(f"\n📋 Scoring {len(picks)} picks for {pick_date}...\n")

    # Fetch yesterday's scores for every sport in the slate (one call per sport).
    sports_needed = {sport_key_for(p.get("league", "")) for p in picks}
    sports_needed.discard(None)
    print("📡 Fetching final scores from The Odds API...")
    all_scores = {}
    for sk in sports_needed:
        all_scores[sk] = fetch_scores(sk) or []
        print(f"   ✓ {sk}: {len(all_scores[sk])} completed games")

    # Score each pick
    scored = []
    wins = losses = pushes = pending = 0
    net_units = 0.0

    for p in picks:
        sp = dict(p)
        sk = sport_key_for(p.get("league", ""))
        game = find_game(all_scores.get(sk, []), p["away_team"], p["home_team"])
        if not game or not game.get("scores"):
            sp["result"], sp["units"] = "PENDING", 0.0
            pending += 1
            scored.append(sp)
            print(f"   ? {p['pick']}: game not found / no score yet")
            continue

        # The Odds API gives scores keyed by team name; match to home/away
        away_s = home_s = None
        for s in game["scores"]:
            if s.get("name", "").lower() == game["home_team"].lower():
                home_s = int(s["score"])
            else:
                away_s = int(s["score"])
        if away_s is None or home_s is None:
            sp["result"], sp["units"] = "PENDING", 0.0
            pending += 1
            scored.append(sp)
            continue

        result, units = score_pick_with_claude(p, away_s, home_s)
        sp["result"]      = result
        sp["units"]       = round(units, 2)
        # final_score kept for backward compat ("10-3" string format).
        # away_score / home_score / winner_side are the new structured
        # fields — consumed by social_generator.py for hit-card.html's
        # final-score strip (away/home logos + scores + gold-highlight
        # on the winning side). The hit-card was previously asking
        # Claude to guess scores from training-data context; now it
        # gets them straight from The Odds API via results.json.
        sp["final_score"] = f"{away_s}-{home_s}"
        sp["away_score"]  = away_s
        sp["home_score"]  = home_s
        if away_s > home_s:
            sp["winner_side"] = "away"
        elif home_s > away_s:
            sp["winner_side"] = "home"
        else:
            sp["winner_side"] = "tie"
        scored.append(sp)

        if   result == "WON":     wins   += 1; net_units += units
        elif result == "LOST":    losses += 1; net_units += units
        elif result == "PUSH":    pushes += 1
        else:                     pending += 1

        print(f"   {result:7s} {p['pick']}: {units:+.2f}u  (final {away_s}-{home_s})")

    net_units = round(net_units, 2)
    summary = f"{wins}-{losses}{'-' + str(pushes) if pushes else ''} · {'+' if net_units >= 0 else ''}{net_units}u"
    print(f"\n📊 {pick_date} closed: {summary} ({pending} pending)")

    # ── write results.json ──
    results = {
        "date":         pick_date,
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "pending":      pending,
        "net_units":    net_units,
        "summary_text": summary,
        "picks":        scored,
    }
    (REPO_ROOT / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"✅ Wrote results.json")

    # ── append to history.json ──
    history_path = REPO_ROOT / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {"stats": {}, "daily": []}

    try:
        date_iso = datetime.strptime(pick_date, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        date_iso = pick_date

    last_cum = history["daily"][-1]["cumulative"] if history["daily"] else 0.0
    new_cum  = round(last_cum + net_units, 2)
    entry    = {"date": date_iso, "units": net_units, "cumulative": new_cum}

    # idempotent: replace the last entry if it's the same date, else append
    if history["daily"] and history["daily"][-1]["date"] == date_iso:
        # Subtract the previously-attributed units so we don't double-count
        prev = history["daily"][-1]
        baseline = prev["cumulative"] - prev["units"]
        entry["cumulative"] = round(baseline + net_units, 2)
        history["daily"][-1] = entry
    else:
        history["daily"].append(entry)

    # Rough aggregate stats (best-effort; the front-end mostly uses daily[])
    stats = history.setdefault("stats", {})
    stats["wins"]          = stats.get("wins", 0)   + wins
    stats["losses"]        = stats.get("losses", 0) + losses
    stats["pushes"]        = stats.get("pushes", 0) + pushes
    stats["net_units"]     = history["daily"][-1]["cumulative"]
    stats["days_recorded"] = len(history["daily"])
    total = stats["wins"] + stats["losses"]
    if total:
        stats["win_pct"] = round(100 * stats["wins"] / total, 1)

    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"✅ Appended to history.json (cumulative = {new_cum}u)")

    # ── append to archive.json ──
    # archive.html consumes {stats, days: [{iso_date, date_display, wins,
    # losses, pushes, net_units, summary_text, picks: [...] }]}. We append
    # yesterday's scored slate as one new day entry and refresh the stats
    # totals so the trust headlines on /archive stay current.
    archive_path = REPO_ROOT / "archive.json"
    archive = json.loads(archive_path.read_text()) if archive_path.exists() else {"stats": {}, "days": []}

    # Trim each pick to just what /archive renders (drops large fields
    # like reasoning if you want to keep file size down — leaving in for
    # now since the existing archive includes reasoning).
    day_entry = {
        "iso_date":     date_iso,
        "date_display": pick_date,
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "net_units":    net_units,
        "summary_text": summary,
        "picks":        scored,
    }

    days = archive.setdefault("days", [])
    # Idempotent: replace existing entry for this date if rerun, else append.
    replaced = False
    for i, d in enumerate(days):
        if d.get("iso_date") == date_iso:
            days[i] = day_entry
            replaced = True
            break
    if not replaced:
        days.append(day_entry)
    # Keep days sorted oldest -> newest so the front-end pagination behaves.
    days.sort(key=lambda d: d.get("iso_date") or "")

    # Recompute aggregate stats from scratch (one pass, can't drift).
    agg = {"total_picks": 0, "wins": 0, "losses": 0, "pushes": 0, "net_units": 0.0}
    for d in days:
        agg["total_picks"] += len(d.get("picks") or [])
        agg["wins"]        += int(d.get("wins") or 0)
        agg["losses"]      += int(d.get("losses") or 0)
        agg["pushes"]      += int(d.get("pushes") or 0)
        agg["net_units"]   += float(d.get("net_units") or 0)
    decided = agg["wins"] + agg["losses"]
    agg["win_pct"]   = round(100 * agg["wins"] / decided, 1) if decided else 0
    agg["roi_pct"]   = round(100 * agg["net_units"] / max(agg["total_picks"], 1), 1)
    agg["net_units"] = round(agg["net_units"], 2)
    agg["days_recorded"] = len(days)
    # Preserve any pre-existing manually-curated stats (best month, etc.).
    archive["stats"] = {**archive.get("stats", {}), **agg}

    archive_path.write_text(json.dumps(archive, indent=2, ensure_ascii=False))
    print(f"✅ Appended to archive.json ({len(days)} days · {agg['total_picks']} picks)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
