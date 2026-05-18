"""
SMARTERPICKS — team-name → ESPN logo URL resolver.

Hotlinks ESPN's public team-logo CDN (a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png).
ESPN doesn't block these requests, the PNGs are 500x500 with transparent
backgrounds, and the URL pattern is stable across all four major leagues.

Used by social_generator.py to inject team_logo URLs into the matchup-card
and pick-card treatments before they hit the renderer. Claude writes the
team name as plain text ("Padres", "Boston Celtics") — this module resolves
it to a CDN URL.

If a team isn't in the dict (e.g., college team, exhibition match, typo),
team_logo_url() returns None and the templates gracefully hide the logo slot
via the `img[src=""]` CSS rule.
"""
from __future__ import annotations

# ESPN abbreviations per league. Each league's dict accepts both the short
# name ("Lakers", "Yankees") and a few common variants — full name with city,
# nickname-only — so it's robust to whatever Claude writes from picks.json.

NBA = {
    "Hawks": "atl", "Atlanta Hawks": "atl",
    "Celtics": "bos", "Boston Celtics": "bos",
    "Nets": "bkn", "Brooklyn Nets": "bkn",
    "Hornets": "cha", "Charlotte Hornets": "cha",
    "Bulls": "chi", "Chicago Bulls": "chi",
    "Cavaliers": "cle", "Cleveland Cavaliers": "cle", "Cavs": "cle",
    "Mavericks": "dal", "Dallas Mavericks": "dal", "Mavs": "dal",
    "Nuggets": "den", "Denver Nuggets": "den",
    "Pistons": "det", "Detroit Pistons": "det",
    "Warriors": "gs", "Golden State Warriors": "gs",
    "Rockets": "hou", "Houston Rockets": "hou",
    "Pacers": "ind", "Indiana Pacers": "ind",
    "Clippers": "lac", "Los Angeles Clippers": "lac", "LA Clippers": "lac",
    "Lakers": "lal", "Los Angeles Lakers": "lal", "LA Lakers": "lal",
    "Grizzlies": "mem", "Memphis Grizzlies": "mem",
    "Heat": "mia", "Miami Heat": "mia",
    "Bucks": "mil", "Milwaukee Bucks": "mil",
    "Timberwolves": "min", "Minnesota Timberwolves": "min", "Wolves": "min",
    "Pelicans": "no", "New Orleans Pelicans": "no",
    "Knicks": "nyk", "New York Knicks": "nyk",
    "Thunder": "okc", "Oklahoma City Thunder": "okc",
    "Magic": "orl", "Orlando Magic": "orl",
    "76ers": "phi", "Philadelphia 76ers": "phi", "Sixers": "phi",
    "Suns": "phx", "Phoenix Suns": "phx",
    "Trail Blazers": "por", "Portland Trail Blazers": "por", "Blazers": "por",
    "Kings": "sac", "Sacramento Kings": "sac",
    "Spurs": "sa", "San Antonio Spurs": "sa",
    "Raptors": "tor", "Toronto Raptors": "tor",
    "Jazz": "utah", "Utah Jazz": "utah",
    "Wizards": "wsh", "Washington Wizards": "wsh",
}

MLB = {
    "Diamondbacks": "ari", "Arizona Diamondbacks": "ari", "D-backs": "ari",
    "Braves": "atl", "Atlanta Braves": "atl",
    "Orioles": "bal", "Baltimore Orioles": "bal", "O's": "bal",
    "Red Sox": "bos", "Boston Red Sox": "bos",
    "Cubs": "chc", "Chicago Cubs": "chc",
    "White Sox": "cws", "Chicago White Sox": "cws",
    "Reds": "cin", "Cincinnati Reds": "cin",
    "Guardians": "cle", "Cleveland Guardians": "cle",
    "Rockies": "col", "Colorado Rockies": "col",
    "Tigers": "det", "Detroit Tigers": "det",
    "Astros": "hou", "Houston Astros": "hou",
    "Royals": "kc", "Kansas City Royals": "kc",
    "Angels": "laa", "Los Angeles Angels": "laa", "LA Angels": "laa",
    "Dodgers": "lad", "Los Angeles Dodgers": "lad", "LA Dodgers": "lad",
    "Marlins": "mia", "Miami Marlins": "mia",
    "Brewers": "mil", "Milwaukee Brewers": "mil",
    "Twins": "min", "Minnesota Twins": "min",
    "Yankees": "nyy", "New York Yankees": "nyy",
    "Mets": "nym", "New York Mets": "nym",
    "Athletics": "oak", "Oakland Athletics": "oak", "A's": "oak",
    "Phillies": "phi", "Philadelphia Phillies": "phi",
    "Pirates": "pit", "Pittsburgh Pirates": "pit",
    "Padres": "sd", "San Diego Padres": "sd",
    "Giants": "sf", "San Francisco Giants": "sf",
    "Mariners": "sea", "Seattle Mariners": "sea",
    "Cardinals": "stl", "St. Louis Cardinals": "stl", "St Louis Cardinals": "stl", "Cards": "stl",
    "Rays": "tb", "Tampa Bay Rays": "tb",
    "Rangers": "tex", "Texas Rangers": "tex",
    "Blue Jays": "tor", "Toronto Blue Jays": "tor", "Jays": "tor",
    "Nationals": "wsh", "Washington Nationals": "wsh", "Nats": "wsh",
}

NHL = {
    "Ducks": "ana", "Anaheim Ducks": "ana",
    "Coyotes": "ari", "Arizona Coyotes": "ari",
    "Bruins": "bos", "Boston Bruins": "bos",
    "Sabres": "buf", "Buffalo Sabres": "buf",
    "Flames": "cgy", "Calgary Flames": "cgy",
    "Hurricanes": "car", "Carolina Hurricanes": "car", "Canes": "car",
    "Blackhawks": "chi", "Chicago Blackhawks": "chi",
    "Avalanche": "col", "Colorado Avalanche": "col", "Avs": "col",
    "Blue Jackets": "cbj", "Columbus Blue Jackets": "cbj",
    "Stars": "dal", "Dallas Stars": "dal",
    "Red Wings": "det", "Detroit Red Wings": "det", "Wings": "det",
    "Oilers": "edm", "Edmonton Oilers": "edm",
    "Panthers": "fla", "Florida Panthers": "fla",
    "Kings": "la", "Los Angeles Kings": "la", "LA Kings": "la",
    "Wild": "min", "Minnesota Wild": "min",
    "Canadiens": "mtl", "Montreal Canadiens": "mtl", "Habs": "mtl",
    "Predators": "nsh", "Nashville Predators": "nsh", "Preds": "nsh",
    "Devils": "nj", "New Jersey Devils": "nj",
    "Islanders": "nyi", "New York Islanders": "nyi",
    "Rangers": "nyr", "New York Rangers": "nyr",
    "Senators": "ott", "Ottawa Senators": "ott", "Sens": "ott",
    "Flyers": "phi", "Philadelphia Flyers": "phi",
    "Penguins": "pit", "Pittsburgh Penguins": "pit", "Pens": "pit",
    "Sharks": "sj", "San Jose Sharks": "sj",
    "Kraken": "sea", "Seattle Kraken": "sea",
    "Blues": "stl", "St. Louis Blues": "stl",
    "Lightning": "tb", "Tampa Bay Lightning": "tb", "Bolts": "tb",
    "Maple Leafs": "tor", "Toronto Maple Leafs": "tor", "Leafs": "tor",
    "Canucks": "van", "Vancouver Canucks": "van",
    "Golden Knights": "vgs", "Vegas Golden Knights": "vgs", "Knights": "vgs",
    "Capitals": "wsh", "Washington Capitals": "wsh", "Caps": "wsh",
    "Jets": "win", "Winnipeg Jets": "win",
}

NFL = {
    "Cardinals": "ari", "Arizona Cardinals": "ari",
    "Falcons": "atl", "Atlanta Falcons": "atl",
    "Ravens": "bal", "Baltimore Ravens": "bal",
    "Bills": "buf", "Buffalo Bills": "buf",
    "Panthers": "car", "Carolina Panthers": "car",
    "Bears": "chi", "Chicago Bears": "chi",
    "Bengals": "cin", "Cincinnati Bengals": "cin",
    "Browns": "cle", "Cleveland Browns": "cle",
    "Cowboys": "dal", "Dallas Cowboys": "dal",
    "Broncos": "den", "Denver Broncos": "den",
    "Lions": "det", "Detroit Lions": "det",
    "Packers": "gb", "Green Bay Packers": "gb",
    "Texans": "hou", "Houston Texans": "hou",
    "Colts": "ind", "Indianapolis Colts": "ind",
    "Jaguars": "jax", "Jacksonville Jaguars": "jax", "Jags": "jax",
    "Chiefs": "kc", "Kansas City Chiefs": "kc",
    "Raiders": "lv", "Las Vegas Raiders": "lv",
    "Chargers": "lac", "Los Angeles Chargers": "lac", "LA Chargers": "lac",
    "Rams": "lar", "Los Angeles Rams": "lar", "LA Rams": "lar",
    "Dolphins": "mia", "Miami Dolphins": "mia",
    "Vikings": "min", "Minnesota Vikings": "min",
    "Patriots": "ne", "New England Patriots": "ne", "Pats": "ne",
    "Saints": "no", "New Orleans Saints": "no",
    "Giants": "nyg", "New York Giants": "nyg",
    "Jets": "nyj", "New York Jets": "nyj",
    "Eagles": "phi", "Philadelphia Eagles": "phi",
    "Steelers": "pit", "Pittsburgh Steelers": "pit",
    "49ers": "sf", "San Francisco 49ers": "sf", "Niners": "sf",
    "Seahawks": "sea", "Seattle Seahawks": "sea",
    "Buccaneers": "tb", "Tampa Bay Buccaneers": "tb", "Bucs": "tb",
    "Titans": "ten", "Tennessee Titans": "ten",
    "Commanders": "wsh", "Washington Commanders": "wsh",
}

# League key (substring matched against the picks.json league field
# case-insensitively) → (espn-cdn-segment, teams-dict)
LEAGUES = {
    "nba":     ("nba", NBA),
    "mlb":     ("mlb", MLB),
    "nhl":     ("nhl", NHL),
    "nfl":     ("nfl", NFL),
}

CDN_BASE = "https://a.espncdn.com/i/teamlogos"


def team_logo_url(team_name: str, league: str = "") -> str | None:
    """Resolve a team name to its ESPN CDN logo URL.

    `team_name` is the string as it appears in picks.json (or as Claude
    wrote it) — accepts both nickname ("Yankees") and full name ("New York
    Yankees"). `league` is the picks.json league field (case-insensitive
    substring match — "NBA · Playoffs" still resolves to NBA).

    Returns the full URL or None if the team isn't found. Templates handle
    the None case by hiding the logo slot via the `img[src=""]` CSS rule.
    """
    if not team_name:
        return None
    name = team_name.strip()
    league_l = (league or "").lower()

    # Try the league hinted by the caller first (most reliable).
    if league_l:
        for key, (sport, teams) in LEAGUES.items():
            if key in league_l:
                abbr = _lookup(name, teams)
                if abbr:
                    return f"{CDN_BASE}/{sport}/500/{abbr}.png"
                # League hinted but team not found — don't fall through to
                # other leagues (different leagues have overlapping nicknames
                # like "Kings" in NBA vs NHL, "Cardinals" in MLB vs NFL).
                return None

    # No league hint: try every league, return first match. This is the
    # less-reliable path; ambiguous names will resolve to whichever league
    # appears first in LEAGUES.
    for sport, teams in LEAGUES.values():
        abbr = _lookup(name, teams)
        if abbr:
            return f"{CDN_BASE}/{sport}/500/{abbr}.png"
    return None


def _lookup(name: str, teams: dict[str, str]) -> str | None:
    """Direct hit on the dict, then case-insensitive, then last-word fallback."""
    if name in teams:
        return teams[name]
    name_l = name.lower()
    for key, abbr in teams.items():
        if key.lower() == name_l:
            return abbr
    # Last-word fallback: "Los Angeles Lakers" → try "Lakers"
    last = name.rsplit(" ", 1)[-1]
    if last != name:
        return _lookup(last, teams)
    return None


if __name__ == "__main__":
    # Quick smoke test:
    #   python team_logos.py
    for nm, lg in [
        ("Lakers", "NBA"),
        ("Los Angeles Lakers", "NBA"),
        ("Mariners", "MLB"),
        ("San Diego Padres", "MLB · Regular Season"),
        ("Maple Leafs", "NHL"),
        ("Chiefs", "NFL"),
        ("Aliens", "NBA"),  # not a team — should return None
        ("Pistons", "NBA · Playoffs"),
    ]:
        print(f"{nm:30s} ({lg or 'no-league'}) → {team_logo_url(nm, lg)}")
