# ============================================================
# SHARPLINE - Daily Picks Generator
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
import sys
from datetime import datetime

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

# How many picks to generate total (1 will be free, rest locked)
TOTAL_PICKS = 7

# Your brand name (shows up in the script logs)
BRAND_NAME = "SHARPLINE"

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

    # Only send the first 15 games to keep the prompt manageable
    # Claude gets the matchups, lines, and odds for each game
    games_for_prompt = games[:15] if len(games) > 15 else games

    # Format the data cleanly for Claude
    games_text = json.dumps(games_for_prompt, indent=2)

    # ── THE PROMPT ──
    # This is the instruction we give Claude.
    # You can edit the text inside the triple quotes to change
    # the style, tone, or focus of the picks.
    prompt = f"""You are a sharp sports betting analyst for {BRAND_NAME}.
Today is {datetime.now().strftime("%A, %B %d, %Y")}.

Here is today's live odds data from sportsbooks:

{games_text}

Generate exactly {TOTAL_PICKS} betting picks from this data.

Rules:
- Only pick games that are actually in the data above
- Be specific — reference actual numbers, spreads, and totals from the data
- Reasoning should be 2-3 sentences, analytical, and reference the odds or line
- Confidence ratings: A (very confident), B+ (confident), B (solid), C+ (speculative)
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
            "model": "claude-sonnet-4-20250514",
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

    # Run the pipeline
    games = get_todays_games() if ODDS_API_KEY else []
    data  = generate_picks(games)
    save_picks(data)

    print(f"\n{'='*50}")
    print(f"  Done! Your site will update automatically.")
    print(f"{'='*50}\n")
