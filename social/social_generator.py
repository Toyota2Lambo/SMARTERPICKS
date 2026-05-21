"""
SMARTERPICKS — Daily Instagram content generator
================================================
Reads today's picks.json, yesterday's results.json, and the cumulative
history.json, then asks Claude to draft every piece of Instagram content
for the day in a single structured response.

Output is a single JSON file at:
    social/YYYY-MM-DD/content.json

That file is the input to:
    - renderer.js     (turns content + templates into PNGs)
    - ig_publisher.py (posts the rendered images + captions)

Run locally with:
    ANTHROPIC_API_KEY=... python social/social_generator.py

In CI, .github/workflows/social-daily.yml wires it up automatically.

Notes
-----
- Uses the official Anthropic Python SDK (pip install anthropic pydantic).
- Uses claude-sonnet-4-6 — the latest Sonnet. Voice quality matters here
  more than raw reasoning, so Sonnet (faster + cheaper) over Opus.
- Output is constrained by a Pydantic schema via messages.parse(), so we
  never have to grovel through "valid JSON only" instructions or sweat
  ```json fences in the response.
- Carousel-topic rotation is deterministic per day-of-month so a series of
  daily runs covers all 12 educational topics over the course of a month.
- Errors fail loud: non-zero exit so the CI workflow's notify-on-failure
  step (mirroring picks_generator.py) can fire.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# anthropic is imported lazily inside main() so --dry-run works in
# environments that haven't installed the SDK (e.g. sandboxes, CI image
# warmups). pydantic stays top-level because the schemas below depend
# on it at import time.
from pydantic import BaseModel, Field


# ── CONFIG ────────────────────────────────────────────────
MODEL_ID    = "claude-sonnet-4-6"
MAX_TOKENS  = 4000
TEMPERATURE = 0.7      # voice has personality; we want some variance

REPO_ROOT   = Path(__file__).resolve().parent.parent
HERE        = Path(__file__).resolve().parent
PICKS_FILE  = REPO_ROOT / "picks.json"
RESULTS_FILE = REPO_ROOT / "results.json"
HISTORY_FILE = REPO_ROOT / "history.json"
SOCIAL_ROOT = REPO_ROOT / "social"
SAMPLES_FILE = HERE / "sample-payloads.json"

# Allow `from photo_fetcher import fetch_photo` when run from social/.
sys.path.insert(0, str(HERE))

# Educational carousel topics — rotated by day-of-month so a month of
# daily posts cycles every angle without us repeating ourselves.
CAROUSEL_TOPICS = [
    "What 'units' actually mean (and why win % alone is a lie)",
    "Why your win rate matters less than your ROI",
    "Reading line movement — what it tells you the public can't see",
    "CLV (closing line value) and why sharps obsess over it",
    "Bankroll math for people who've never thought about bankroll",
    "Public money vs. sharp money — how to tell which is moving the line",
    "The math of parlays (and why bookmakers love them)",
    "Hedging — when it's smart, when it's just buying back losses",
    "Player props vs. game lines — different math, different edges",
    "Why variance is real and one losing month proves nothing",
    "The Kelly criterion — explained without the math degree",
    "Why anyone claiming an 80% win rate is lying or selling something",
]


# ── OUTPUT SCHEMA (Pydantic) ──────────────────────────────
# IMPORTANT: keep these descriptions free of em dashes and arrows. Claude
# reads the schema and treats the descriptions as voice examples; if they
# contain "—" it will sprinkle them everywhere in its output.
class IGPickPost(BaseModel):
    """3-slide carousel announcing today's free pick."""
    caption: str          = Field(description="Instagram caption, 80 to 220 chars, no hashtags here.")
    slide1_text: str      = Field(description="Slide 1, the headline or hook for today's free pick. Short, punchy, fits a card.")
    slide2_text: str      = Field(description="Slide 2, the actual pick plus odds plus brief why. Tight, 2 to 3 lines.")
    slide3_text: str      = Field(description="Slide 3, CTA line, e.g. 'Full card unlocked at smarterpicks.io. 7-day free trial.'")
    hashtags: List[str]   = Field(min_length=8, max_length=18, description="Mix of broad (#sportsbetting) and specific (#nbapicks). No leading #.")


class IGResultsPost(BaseModel):
    """Single-image post recapping yesterday's slate."""
    caption: str          = Field(description="80 to 180 char caption acknowledging both wins and losses.")
    headline_text: str    = Field(description="Big-text overlay for the image. Example: 'Yesterday: 5-2, +3.7u'.")
    hashtags: List[str]   = Field(min_length=8, max_length=18)


class IGCarouselTopic(BaseModel):
    """5-slide educational carousel on a rotating topic."""
    topic: str            = Field(description="The chosen educational topic (echoed back from the prompt).")
    slide1_text: str      = Field(description="Slide 1, title slide. Single bold question or claim.")
    slide2_text: str      = Field(description="Slide 2, set up the misconception or starting point.")
    slide3_text: str      = Field(description="Slide 3, the key insight or correction.")
    slide4_text: str      = Field(description="Slide 4, concrete example or worked number.")
    slide5_text: str      = Field(description="Slide 5, payoff plus soft CTA.")
    caption: str          = Field(description="120 to 260 char caption summarizing the carousel and inviting saves or shares.")
    hashtags: List[str]   = Field(min_length=8, max_length=18)


class MemePost(BaseModel):
    """Meme-style image post for personality / community."""
    top_text: str         = Field(description="Top caption, set up the joke, max 90 chars.")
    bottom_text: str      = Field(description="Bottom caption, punchline, max 90 chars.")
    image_concept: str    = Field(description="One-sentence description of the visual concept (used in alt text and Discord notification).")


class Treatment(BaseModel):
    """One render from the modern editorial template grid (social/templates/).
    Eleven templates total, documented in templates-registry.js. You pick 3 to
    5 of these per day to complement the five standard groups above. Each
    treatment is a single image render. The renderer pulls them from
    content.treatments[] when it builds the manifest.

    Why fields_json is a string and not a typed sub-model:
    we tried `fields: TreatmentFields` with 47 typed fields + nested chunk
    schemas (RecapPickRow / ChartBar / IndexCell), and Anthropic's strict
    structured output compiled the combined grammar to something too large
    to enforce ('The compiled grammar is too large' from messages.parse()).
    Returning the field bag as a JSON-string keeps the schema tiny while
    still letting Claude produce any field shape — we parse + validate it
    in main() after the API call returns."""
    template:  str = Field(description="One of the 11 template filenames from the registry shown in the user prompt. Must end in .html.")
    rationale: str = Field(description="One short sentence on why this template fits today's narrative.")
    group:     str = Field(description="Publisher group routing. One of: ig_pick_post, ig_results_post, ig_carousel_topic, or treatments.")
    size:      str = Field(description="feed for 1080x1080 grid posts, story for 1080x1920 vertical stories.")
    fields_json: str = Field(description=(
        "VALID JSON-ENCODED OBJECT STRING with every field the chosen "
        "template needs, matching the field_example for that template "
        "shown in the user prompt EXACTLY (same keys, same types).\n\n"
        "Examples:\n"
        "pick-card.html: {\"tag\":\"TODAY'S FREE PICK\",\"matchup\":\"Lakers @ Celtics\",\"game_time\":\"7:30 PM ET\",\"pick_headline_html\":\"Celtics cover the <em>4.5</em>\",\"line\":\"-4.5\",\"edge\":\"+6.2%\",\"confidence\":\"A-\"}\n\n"
        "stat-card.html: {\"eyebrow\":\"NBA ROAD FAVS · 2024-25\",\"stat_value\":\"72\",\"stat_unit\":\"%\",\"caption_html\":\"of road favorites of <em>-4 or more</em> failed to cover this season.\",\"source\":\"n=142 · since Jan 1\"}\n\n"
        "recap-card.html: {\"tag\":\"YESTERDAY'S BOARD\",\"wins\":\"3\",\"losses\":\"1\",\"units\":\"+3.7u\",\"pick_rows\":[{\"matchup\":\"NBA · LAL @ BOS\",\"play\":\"Celtics -4.5\",\"result\":\"win\",\"units\":1.4}]}\n\n"
        "Escape inner double quotes properly. Return the whole thing as ONE JSON STRING (which itself must parse via json.loads on the server). NEVER return an empty object — every field for the chosen template must be populated with today's actual content."
    ))
    caption:   str       = Field(default="", description="IG caption for this treatment. Required only when group=treatments (publishes as standalone). For ig_pick_post / ig_results_post / ig_carousel_topic the treatment rides the group's own caption, leave this empty.")
    hashtags:  List[str] = Field(default_factory=list, description="Hashtags for standalone treatments (group=treatments). 8 to 18 mixed broad and specific, no leading #.")


class DailyContent(BaseModel):
    """Top-level container for everything a day's IG run needs."""
    ig_pick_post: IGPickPost
    ig_results_post: IGResultsPost
    ig_carousel_topic: IGCarouselTopic
    story_sequence: List[str] = Field(min_length=5, max_length=5, description="Five short story texts: morning teaser, midday poll, pre-lock reminder, live tracker, night recap.")
    meme_post: MemePost
    treatments: List[Treatment] = Field(default_factory=list, min_length=0, max_length=8, description="3 to 5 selected modern treatments for today, layered on top of the standard 5 groups. Pick whichever templates fit today's narrative best. Skip the field if today is genuinely a no-action day with nothing extra to say.")


# ── POST-PROCESSING SCRUB ─────────────────────────────────
# Hard backstop: walks the generated content and strips characters Claude
# was told not to use. The prompt is the primary defense; this catches
# anything that slipped through regardless. Touches every string in
# the nested dict (captions, slides, hashtags) and leaves non-strings alone.
_PUNCT_REPLACEMENTS = [
    # Em dashes → comma (most natural in our voice)
    (" — ", ", "),
    (" —", ","),
    ("— ", ", "),
    ("—", "-"),
    # En dashes outside of numeric contexts
    (" – ", ", "),
    ("–", "-"),
    # Arrows
    (" → ", " to "),
    (" ← ", " from "),
    ("→", " to "),
    ("←", " from "),
    ("↑", "up"),
    ("↓", "down"),
    ("⇒", " to "),
    ("⟶", " to "),
    # Smart quotes
    ("‘", "'"),
    ("’", "'"),
    ("“", '"'),
    ("”", '"'),
    # Ellipsis character
    ("…", "..."),
]

def strip_ai_punct(obj):
    """Recursively scrub banned punctuation from every string in a structure."""
    if isinstance(obj, str):
        s = obj
        for old, new in _PUNCT_REPLACEMENTS:
            s = s.replace(old, new)
        # Collapse any accidental double spaces / double commas the replacements may have created
        while "  " in s:
            s = s.replace("  ", " ")
        s = s.replace(", ,", ",").replace(",,", ",")
        return s.strip()
    if isinstance(obj, dict):
        return {k: strip_ai_punct(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_ai_punct(v) for v in obj]
    return obj


# ── POST-PROCESSING SCRUB ─────────────────────────────────
# Hard backstop: walks the generated content and strips characters Claude
# was told not to use. The prompt is the primary defense; this catches
# anything that slipped through regardless. Touches every string in
# the nested dict (captions, slides, hashtags) and leaves non-strings alone.
_PUNCT_REPLACEMENTS = [
    # Em dashes → comma (most natural in our voice)
    (" — ", ", "),
    (" —", ","),
    ("— ", ", "),
    ("—", "-"),
    # En dashes outside of numeric contexts
    (" – ", ", "),
    ("–", "-"),
    # Arrows
    (" → ", " to "),
    (" ← ", " from "),
    ("→", " to "),
    ("←", " from "),
    ("↑", "up"),
    ("↓", "down"),
    ("⇒", " to "),
    ("⟶", " to "),
    # Smart quotes
    ("‘", "'"),
    ("’", "'"),
    ("“", '"'),
    ("”", '"'),
    # Ellipsis character
    ("…", "..."),
]

def strip_ai_punct(obj):
    """Recursively scrub banned punctuation from every string in a structure."""
    if isinstance(obj, str):
        s = obj
        for old, new in _PUNCT_REPLACEMENTS:
            s = s.replace(old, new)
        # Collapse any accidental double spaces / double commas the replacements may have created
        while "  " in s:
            s = s.replace("  ", " ")
        s = s.replace(", ,", ",").replace(",,", ",")
        return s.strip()
    if isinstance(obj, dict):
        return {k: strip_ai_punct(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_ai_punct(v) for v in obj]
    return obj


# ── DATA LOADING ──────────────────────────────────────────
def safe_load_json(path: Path, default):
    """Read a JSON file or return a sensible default if it's missing / unreadable.
    The generator should still produce SOMETHING when one of the upstream files
    isn't there yet — better partial than total failure."""
    if not path.exists():
        print(f"   ⚠ {path.name} not found — using default", file=sys.stderr)
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"   ⚠ couldn't parse {path.name} ({e}) — using default", file=sys.stderr)
        return default


def pick_carousel_topic() -> str:
    """Deterministic day-of-month rotation. Day 1 → topic 0, day 13 → topic 0
    again, etc. With 12 topics on a ~30-day cycle each topic surfaces 2-3
    times a month — enough variety, enough repetition for retention."""
    return CAROUSEL_TOPICS[(datetime.now().day - 1) % len(CAROUSEL_TOPICS)]


def picks_summary(picks_data: dict) -> str:
    """Format today's picks into a readable block for the prompt. Includes
    away/home teams alongside the pick text so Claude can fill matchup-card
    and pick-card matchup labels with the real team names instead of
    falling back to 'Opponent'."""
    picks = picks_data.get("picks") or []
    if not picks:
        return "(no picks loaded — picks.json was empty or missing)"
    lines = []
    for i, p in enumerate(picks, 1):
        league = p.get("league", "—")
        away   = p.get("away_team", "")
        home   = p.get("home_team", "")
        matchup = f" {away} @ {home}" if (away and home) else ""
        pick   = p.get("pick", "—")
        odds   = p.get("odds", "—")
        reason = (p.get("reasoning") or "").strip()
        if len(reason) > 220:
            reason = reason[:217] + "…"
        free_tag = " (FREE)" if not p.get("is_premium", True) else ""
        lines.append(f"{i}. [{league}]{free_tag}{matchup} — {pick} ({odds}) — {reason}")
    return "\n".join(lines)


def results_summary(results_data: dict) -> str:
    """One-line summary of yesterday's results."""
    if not results_data:
        return "(no results loaded — results.json was empty or missing)"
    return (
        f"{results_data.get('date','yesterday')}: "
        f"{results_data.get('wins',0)}-{results_data.get('losses',0)}"
        f"{('-' + str(results_data['pushes'])) if results_data.get('pushes') else ''}"
        f", net {results_data.get('net_units',0):+.2f}u"
    )


def results_picks_detail(results_data: dict) -> str:
    """Each graded pick from yesterday — pick text, league/matchup, result,
    units. recap-card.pick_rows[].play has to mirror the pick text from
    here; without these lines in the prompt Claude was filling rows with
    'Pick 1' / 'Pick 2' placeholders. Capped at 6 rows, which is more than
    any single recap-card needs."""
    picks = results_data.get("picks") or []
    if not picks:
        return "(no individual graded picks in results.json)"
    lines = []
    for i, p in enumerate(picks[:6], 1):
        league = p.get("league", "")
        away   = p.get("away_team", "")
        home   = p.get("home_team", "")
        matchup = f"{away} @ {home}" if (away and home) else (p.get("game_detail") or "")
        pick   = p.get("pick", "—")
        result = str(p.get("result", "?")).upper()
        units  = p.get("units", 0)
        lines.append(f"{i}. [{league}] {matchup} — {pick} — {result} ({units:+.2f}u)")
    return "\n".join(lines)


def history_headline(history_data: dict) -> str:
    """One-line YTD summary."""
    s = (history_data or {}).get("stats") or {}
    if not s:
        return "(no history loaded)"
    return (
        f"YTD: {s.get('wins',0)}-{s.get('losses',0)} "
        f"({s.get('win_pct','?')}% win rate), "
        f"net {s.get('net_units',0):+.1f}u, "
        f"ROI {s.get('roi_pct','?')}%, "
        f"{s.get('days_recorded',0)} days logged"
    )


# ── PROMPTS ───────────────────────────────────────────────
SYSTEM_PROMPT = """You write the Instagram content for SmarterPicks, a daily AI-powered sports-betting analysis service. You speak in the brand's voice across every post we publish.

VOICE (non-negotiable):
- DIRECT SALES VOICE. Talk TO the reader using "you" and "your". The post's job is to make a sportsbook bettor decide they want what we have, today. Lead with the offer. Not the math.
- Layman-readable. Most readers don't bet for fun, they want to win more games, more weeks, more often. Write so a casual ESPN watcher gets the message in 3 seconds. "Tonight's free pick" beats "today's free edge analysis". "Win more bets" beats "improved closing line value". A grandma reading the caption should be able to repeat it back to a friend.
- Outcome-forward headlines. "You can win more this week." "Tonight could change your Sunday." "Stop guessing on parlays." "Smart bets. Better Sundays." Frame everything around what the reader GETS. Never lead with what we did.
- Numbers as proof, not as the message. "+41.9u this season" and "4,100+ members" appear at the END of a caption as backup, never as the opener. The stats prove the offer is real; the offer is the headline.
- Honest, not deceptive. Hard line: NEVER write "guaranteed", "lock" (as a noun describing a pick), "sure thing", "can't lose", "easy money", "free money", "100% profit", "get rich", or any explicit promise of certain wins. "Can win more", "could win more", "more wins than guessing", "stop losing on dumb bets" are all fine — those are aspirational, not promises.
- We are an analysis service. The site has the disclaimer. Captions don't preach about variance every post (that kills conversion) but they never claim wins that haven't happened.
- One emoji per caption max, usually none. We sell, but we don't yell.

PUNCTUATION (hard rules, do not break these):
- NEVER use em dashes. The character "—" is banned. Use a comma, period, or parentheses instead.
- NEVER use en dashes ("–") except inside numeric ranges that are already conventional like "53-58%". Default: use a hyphen "-" if you need a dash.
- NEVER use arrows of any kind: →, ←, ↑, ↓, ⇒, ⟶. Write the word: "to", "from", "up", "down", "leads to".
- NEVER use smart quotes (curly quotes). Use plain ' and ".
- NEVER use the ellipsis character "…". Write three dots: "...".

PHRASES TO AVOID (these are AI tells, never write them):
- "Here's the case" / "Here's the deal" / "Here's the thing"
- "Let's dive in" / "Let's break it down" / "Let's unpack"
- "It's worth noting" / "Worth mentioning"
- "When it comes to..."
- "The key takeaway is..."
- "In this guide" / "In this post" / "In this carousel"
- "Save this one" / "Save for reference" / "Bookmark this" / "Pin this"
- "Plain English version" / "Explained without the math degree" / "The simple version"
- "You don't need X, you need Y" parallel structures
- "Bigger X, bigger Y. Smaller X, smaller Y" balanced parallels
- Closing summary lines that wrap up what you just said
- Anything that sounds like a LinkedIn carousel or a Medium post intro

SENTENCE STRUCTURE:
- Vary length aggressively. Mix 5-word sentences with 15-word sentences.
- Don't open every paragraph with the same connector word.
- Drop the wrap-up sentence at the end. End on a strong specific line and stop.
- Skip the rhetorical question opener ("Ever wonder why...?"). Just state the thing.
- If you want emphasis, use a short sentence, not a dash break.

WHAT WE STAND FOR:
- Public track record. We post wins AND losses every morning.
- Realistic win rates (53 to 58 percent is the honest target, anyone claiming 80 percent is selling something).
- Subscription is $29.99 a month or $199 a year through Whop. New members get a 7-day free trial, then bill on day 8. Cancel anytime.
- 21 plus only. Bet responsibly. We include a soft responsible-gambling note when we mention real money.

CONTENT FORMAT RULES:
- Captions are 80 to 260 characters depending on the post type. Tighter is better.
- Hashtags go in the dedicated hashtags array (NEVER in the caption text). Mix broad (#sportsbetting, #sportspicks) with specific (#nbapicks, #mlbpicks).
- Slide texts are tight. They get rendered onto an image card, so 1 to 3 short lines each. Lead with the punchy line.
- Story texts are even tighter, 1 to 2 lines each, conversational.
- For meme posts: set up, then punchline. Self-deprecating bettor humor only, never punching down at users.

MODERN TREATMENTS LAYER (additive on top of the 5 standard groups):
Beyond the standard 5 groups, you also select 3 to 5 from a grid of 11 editorial templates. Each one is a single image render. The user prompt lists every template with its role, a "when to use" hint, and a complete field example you must mirror exactly.

How to choose treatments for the day:
- If yesterday had real action: pick a recap-card OR an index-card (not both).
- If there is a free pick today: pick a pick-card or matchup-card or slip-card.
- MATCHUP-CARDS CAN REPEAT (2-3 per day) BUT EVERY MATCHUP-CARD MUST BE A UNIQUE GAME. Never two matchup-cards about the same team_a/team_b pairing. One per game on the slate, pulled from different entries in picks.json (free OR premium picks both count). Same matchup duplicated = bot-spam look on the grid; don't.
- NEWS-CARDS: include 1-2 per day surfacing sports-media-style observations from picks.json reasoning (team trends, player news, line moves, recent performance). Each news-card focuses on a DIFFERENT team or storyline — no two news-cards about the same team in one day. ALWAYS set photo_query to a sport/stadium-relevant query (e.g. 'basketball arena empty night', 'baseball stadium dusk', 'hockey ice closeup', 'football field empty') so the post lands with a real-world backdrop like the lifestyle-card pattern.
- HIT-CARDS: include 1 per day MAX, only when results.json has a notable winning pick from yesterday worth spotlighting. Visual hero for one specific big win — team logo callout + final-style score + "we saw" reasoning. Skip on losing days or days with no standout hit.
- Educational thread: carousel-card across 3 to 5 numbered slides only if you commit to the whole thread.
- cover-card or photo-cover-card: at most one per day, and only when today is a genuine event (hot streak, season opener, marquee night). Skip both on quiet days.
- quote-card: a single sharp editorial pull-quote. Atmospheric, optional.
- Don't repeat templates EXCEPT matchup-card (2-3 ok) and news-card (1-2 ok). All others appear at most once per day.

EVERY treatment publishes as its own standalone single-image post — never as a slide inside the legacy ig_pick_post / ig_results_post / ig_carousel_topic carousels. That means EVERY treatment needs its own caption (80-200 chars) AND its own hashtags (8-18, mixed broad+specific). Empty captions ship as blank Instagram posts, which look broken.

MANDATORY DAILY LIFESTYLE POST:
Every day's treatment array MUST include exactly one lifestyle-card.html. The luxury photo + a direct sales-forward headline = aspirational call to action. The image shows "what winning looks like"; the copy gives the reader a one-line reason to start TODAY.

Voice for lifestyle-card copy:
- headline_html is OUTCOME-FORWARD SALES COPY in "you" language. Wrap the punchy phrase in <em> for gold accent. Examples that nail the brief:
  - "Tonight could change your <em>week</em>."
  - "Win more bets. <em>Live better Sundays.</em>"
  - "Your free pick drops at <em>7 AM</em>."
  - "Stop losing money on <em>dumb bets</em>."
  - "<em>More wins</em>. Less guessing."
  - "What <em>winning</em> feels like."
  - "<em>You</em> bet smarter with us."
- sub_html is the call to action + light proof. Examples:
  - "Today's free pick locks at first pitch. Tap the bio."
  - "Members up <em>+41.9u</em>. Start a 7-day free trial."
  - "<em>4,100+</em> bettors using today's card. Join them."
  - "Start your 7-day free trial. Cancel anytime."
- caption (the IG caption) is the salesy hook. Lead with the offer, end with a CTA (link in bio, free trial mention). 80-200 chars. Use "you" liberally. Numbers appear only as proof at the end, never as the opener.
- photo_query rotates daily — pick a fresh luxury-aesthetic angle: 'luxury home interior modern night', 'vintage porsche 911 classic', 'swiss watch macro detail', 'tuscan villa pool sunset', 'manhattan penthouse view', 'private jet tarmac dusk', 'monaco yacht harbor', 'amalfi coast cliff villa', 'lamborghini huracan', 'rolex submariner closeup'. Don't repeat the same one twice in a week.
- HARD BANS (regulatory + ethical): "guaranteed", "lock of the year", "lock" as a noun describing a pick, "sure thing", "can't lose", "easy money", "free money", "100% profit", "get rich", any explicit promise of certain wins. "Can win more", "could win more", "win more than you do now" are all fine because they're aspirational, not promises.

Field rules for treatments:
- Headlines wrap the single most important word or number in <em>...</em>. That word renders gold italic. Pick that word deliberately, never wrap a connector word like "the" or "a".
- Field names ending in _html accept inline HTML (<em>, <strong>). All other fields are plain text only.
- Plain numeric fields (line, edge, confidence) are mono-rendered. Use the conventional sportsbook formatting: "-4.5", "+135", "+6.2%", "A-".
- Photo fields: if a template supports a photo_query, set it to a short concrete English query like "basketball arena empty" or "Boston Celtics court". Two or three words. The system resolves the photo before render.
- Voice rules (no em dashes, no arrows, no smart quotes, no hype words, vary sentence length) apply to ALL treatment fields the same way they apply to the standard groups.

LENGTH CAPS (text overflows the card and gets clipped if these are exceeded):
- caption_html: at most 100 chars total including HTML tags.
- title_html and pick_headline_html and headline_html and the_pick_html: at most 80 chars total.
- deck_html and quote_text_html and body_html: at most 140 chars total.
- note_html and quote_attrib: at most 60 chars total.
- recap-card pick_rows: at most 5 rows. Each play string at most 28 chars.
- index-card cells: exactly 6 cells. Each cell num at most 10 chars, label at most 12 chars, foot at most 22 chars.
- chart-card bars: at most 6 bars. Each bar label at most 24 chars.

DATA SOURCING (the most common failure mode — fix this before everything else):
- ALWAYS pull real values from today's picks_data, yesterday's results_data, and the games_today slate. NEVER invent placeholder text like "Opponent", "Dog", "AWAY", "Win 1", "Loss 2", "Game 1", "Team A", or any generic stand-in. A placeholder on a published card looks like a broken bot.
- recap-card pick_rows[].play must be the literal pick text from results_data.picks[].pick (e.g. "Celtics -4.5", "Yankees ML", "Under 218.5"). NEVER write "Win 1" or "MLB Game 1".
- recap-card pick_rows[].matchup is "LEAGUE · AWAY @ HOME" pulled from the same row (e.g. "NBA · LAL @ BOS").
- matchup-card REQUIRES both team names from a single picks_data entry. If only one team is known, do NOT use matchup-card — use pick-card or slip-card instead.
- matchup-card.stat_a and .stat_b must be a comparable metric (offensive rating, run differential, expected goals, etc.) — NEVER a moneyline range or odds-shaped value.
- If you can't fill a required field with real data, drop the treatment entirely. Fewer good treatments beats a card with "Opponent" on it.

You will be given today's picks, yesterday's result, and the cumulative track record. Use those numbers and be specific, not vague."""


def build_treatment_registry_block(samples: dict) -> str:
    """Compact, Claude-friendly catalog of every modern treatment. Pulls the
    `role` + `when` hint per template plus the full field example, so Claude
    can pattern-match field names + values without having to invent them."""
    # Kept in Python (rather than parsing the JS registry) so this script has
    # no Node round-trip. The dict below mirrors templates-registry.js's role
    # + selection guidance. When a template is added there, add it here too.
    docs = {
        "quote-card.html":       ("Editorial pull-quote, one thesis-line that breathes.",
                                  "Sharp observation about the market or a sport. Optional photo backdrop."),
        "pick-card.html":        ("Daily pick brief, matchup label plus giant italic play plus 3-cell data row.",
                                  "When there is a free pick to announce. The workhorse."),
        "stat-card.html":        ("Hero statistic, one massive number anchoring the post.",
                                  "A single statistic IS the story (e.g. 72 percent of road favs failed to cover)."),
        "matchup-card.html":     ("Head-to-head, symmetric vs grid plus 140px inset photo on favored side.",
                                  "Marquee game tonight worth comparing directly."),
        "slip-card.html":        ("Bet slip receipt aesthetic, perforated edge plus dashed rules.",
                                  "Showing the play as a concrete ticket. Mono-heavy."),
        "recap-card.html":       ("Results ledger, color chip W and L list plus giant hero record.",
                                  "After a results day. The receipts post. Never on a no-action day."),
        "carousel-card.html":    ("Numbered slide for multi-card educational threads.",
                                  "Walking through a concept across 3 to 5 stepped slides. Commit to the whole thread or skip."),
        "chart-card.html":       ("Bar chart visualization with one row highlighted.",
                                  "ATS records by side, ROI by market, win rate by sport."),
        "cover-card.html":       ("Magazine cover masthead, issue number plus date plus huge italic headline.",
                                  "Announcing a feature, hot streak, season opener. Editorial event posts. Optional photo."),
        "index-card.html":       ("Six-cell stat grid, season receipts in atomic form.",
                                  "Credibility lift post, cumulative record. Weekly or monthly cadence."),
        "photo-cover-card.html": ("Atmospheric magazine spread with full-bleed treated photo.",
                                  "Marquee moment, game-day hype, feature, season opener. Photo required. Max one per day."),
        "lifestyle-card.html":   ("Aspirational lifestyle post. Luxury photo + receipt-style headline. Image is the hook, math is the trust.",
                                  "INCLUDE EXACTLY ONE PER DAY. Photo of luxury (home, car, watch, villa, yacht, jet). Headline is ALWAYS a numeric receipt with the number wrapped in <em>. Sub-line is a time horizon or math note. Field example: tag='COMPOUND', eyebrow='RECEIPTS · 44 DAYS LOGGED', headline_html='Compound at <em>0.85u</em> a day.', sub_html='Two-fifty trading days a year. <em>Five years.</em> Math, not magic.', photo_query='luxury home interior modern night'. ROTATE photo_query daily across home / car / watch / villa / yacht / jet. NEVER write 'this could be yours', 'imagine the life', 'manifest', or any promise of outcomes."),
        "news-card.html":        ("Team or sport news brief WITH RELEVANT STOCK PHOTO BACKDROP. Single team logo overlay + italic-serif headline + body brief, all on a treated sport/stadium photo. Reads like an ESPN/sports-media beat post with cinematic context.",
                                  "INCLUDE 1-2 PER DAY when picks.json reasoning surfaces a sharp observation. Each card MUST set photo_query to a sport/stadium-relevant query like 'basketball arena empty night', 'baseball stadium dusk', 'hockey ice closeup', 'football field empty', 'mlb dugout', 'nba court'. team_name + league fields drive the logo overlay. Body 1-2 sentences from picks.json reasoning. Different team/storyline per card — no two news-cards about the same team in one day."),
        "hit-card.html":         ("Single-win spotlight. Banner headline ('Yesterday's strongest hit') + boxed team-logo callout + final score row + 'we saw' reasoning. The hero shot for one specific winning pick.",
                                  "INCLUDE 1 PER DAY MAX, only when results.json has a clear standout winning pick worth spotlighting (highest units won, biggest edge that hit, etc.). Source the pick + reasoning from results.json. Skip on losing days. Different visual from recap-card — this is one play big, not the full ledger."),
    }
    blocks = []
    for tpl, (role, when) in docs.items():
        example = samples.get(tpl, {})
        blocks.append(
            f"--- {tpl} ---\n"
            f"role: {role}\n"
            f"when: {when}\n"
            f"field_example:\n{json.dumps(example, indent=2, ensure_ascii=False)}"
        )
    return "\n\n".join(blocks)


def build_user_prompt(picks_data, results_data, history_data, carousel_topic, samples) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    treatment_block = build_treatment_registry_block(samples)
    return f"""Today is {today}.

YESTERDAY'S RESULT (headline)
{results_summary(results_data)}

YESTERDAY'S GRADED PICKS (verbatim — use these for recap-card pick_rows[].play and .matchup)
{results_picks_detail(results_data)}

YEAR-TO-DATE TRACK RECORD
{history_headline(history_data)}

TODAY'S PICKS (from picks.json — the FREE pick is the public-facing one we lead with)
{picks_summary(picks_data)}

EDUCATIONAL CAROUSEL TOPIC FOR TODAY
{carousel_topic}

Generate the full Instagram content package for today. Five separate pieces:

1. **ig_pick_post** — 3-slide carousel announcing TODAY'S FREE pick (the one tagged FREE in the list above). Slide 1 hooks, slide 2 reveals + odds + 1 line of why, slide 3 closes with the soft "full card at smarterpicks.io" CTA mentioning the 7-day free trial.

2. **ig_results_post** — single-image post showing yesterday's record. Caption acknowledges what we won AND what we lost — never spin a losing day as a win.

3. **ig_carousel_topic** — 5-slide educational carousel on the topic above. Each slide builds on the last; ends with a payoff that doesn't feel like a sales pitch.

4. **story_sequence** — exactly 5 stories pacing the day:
   - [0] morning teaser — "card drops in 30 min" energy
   - [1] midday poll — invite engagement on something light
   - [2] pre-lock reminder — last call to read the card
   - [3] live tracker — "X-Y so far" update
   - [4] night recap — set up tomorrow

5. **meme_post** — one bettor-humor meme (top + bottom text + visual concept). Self-deprecating > punching down.

6. **treatments** — 3 to 5 entries from the modern grid below. Each entry has template, rationale, group, size, fields. Mirror the field_example for the chosen template exactly. Skip entirely on a true no-action day.

MODERN TREATMENT REGISTRY (pick 3 to 5):

{treatment_block}

Be specific. Use the actual numbers. Don't say "we had a good run", say "5-2 with +3.7u". Don't say "good odds", say "+135"."""


# ── TEAM LOGO INJECTION ───────────────────────────────────
def _inject_team_logos(treatment: dict, picks_data: dict, results_data: dict, resolver) -> None:
    """For matchup-card + pick-card treatments, resolve team names to ESPN
    CDN logo URLs and stuff them into the fields dict. Mutates in place.

    matchup-card uses team_a + team_b (Claude writes the team names);
    pick-card uses away_logo + home_logo (derived from today's free pick
    in picks.json — Claude doesn't write these, we know them from source).

    Soft-fails if team_logos isn't importable or the team isn't in the
    league dict; the template hides the logo slot in either case via the
    img[src=""] CSS rule plus the onerror handler."""
    if resolver is None:
        return
    tpl    = treatment.get("template", "")
    fields = treatment.get("fields") or {}

    if tpl == "matchup-card.html":
        # Try to find the league from the picks_data — match either team
        # name against today's picks to figure out which sport.
        league = _league_for_team(fields.get("team_a"), fields.get("team_b"), picks_data)
        if fields.get("team_a") and "team_a_logo" not in fields:
            url = resolver(fields["team_a"], league)
            if url:
                fields["team_a_logo"] = url
        if fields.get("team_b") and "team_b_logo" not in fields:
            url = resolver(fields["team_b"], league)
            if url:
                fields["team_b_logo"] = url
        treatment["fields"] = fields

    elif tpl == "pick-card.html":
        # For pick-card, pull away/home from picks_data directly — Claude
        # didn't write the team names structured (just a matchup string).
        pick = _today_free_pick(picks_data)
        if pick:
            away = pick.get("away_team")
            home = pick.get("home_team")
            league = pick.get("league", "")
            if away and "away_logo" not in fields:
                url = resolver(away, league)
                if url:
                    fields["away_logo"] = url
            if home and "home_logo" not in fields:
                url = resolver(home, league)
                if url:
                    fields["home_logo"] = url
        treatment["fields"] = fields

    elif tpl == "news-card.html":
        # news-card has an optional single team focus. Claude writes
        # team_name + league strings; we resolve to team_logo URL.
        team = fields.get("team_name")
        league = fields.get("league", "")
        if team and "team_logo" not in fields:
            url = resolver(team, league)
            if url:
                fields["team_logo"] = url
        treatment["fields"] = fields

    elif tpl == "slip-card.html":
        # slip-card uses today's free pick for the away+home logos at
        # the top of the ticket. Same source as pick-card.
        pick = _today_free_pick(picks_data)
        if pick:
            away = pick.get("away_team")
            home = pick.get("home_team")
            league = pick.get("league", "")
            if away and "away_logo" not in fields:
                url = resolver(away, league)
                if url:
                    fields["away_logo"] = url
            if home and "home_logo" not in fields:
                url = resolver(home, league)
                if url:
                    fields["home_logo"] = url
        treatment["fields"] = fields

    elif tpl == "hit-card.html":
        # hit-card spotlights one yesterday win. Three logo slots:
        # pick_team_logo (the winning team in the callout) + away_logo
        # + home_logo (final score row). Plus winner-side highlight
        # class so the score color goes gold on the winning side.
        league = fields.get("league", "")
        for name_field, logo_field in (
            ("pick_team_name", "pick_team_logo"),
            ("away_team_name", "away_logo"),
            ("home_team_name", "home_logo"),
        ):
            team = fields.get(name_field)
            if team and logo_field not in fields:
                url = resolver(team, league)
                if url:
                    fields[logo_field] = url
        # winner_side: 'away' | 'home' → drives the gold-color class
        # on the final score strip.
        side = (fields.get("winner_side") or "").lower()
        if side == "away":
            fields.setdefault("away_winner_class", "winner")
            fields.setdefault("home_winner_class", "")
        elif side == "home":
            fields.setdefault("away_winner_class", "")
            fields.setdefault("home_winner_class", "winner")
        else:
            fields.setdefault("away_winner_class", "")
            fields.setdefault("home_winner_class", "")
        treatment["fields"] = fields


def _today_free_pick(picks_data: dict) -> dict | None:
    """Return the first free (is_premium=False) pick from picks.json,
    or None if none. Mirrors the inline lookup at the bottom of main()."""
    for p in (picks_data.get("picks") or []):
        if not p.get("is_premium", True):
            return p
    return None


def _league_for_team(team_a: str, team_b: str, picks_data: dict) -> str:
    """Infer the league for a matchup-card by finding either team in
    today's picks. Returns the empty string if neither team is on the
    slate — the resolver still works without a league hint, it just
    falls through to its own all-leagues search."""
    if not (team_a or team_b):
        return ""
    a_l = (team_a or "").lower()
    b_l = (team_b or "").lower()
    for p in (picks_data.get("picks") or []):
        for k in ("away_team", "home_team"):
            t = (p.get(k) or "").lower()
            if t and (t == a_l or t == b_l or a_l in t or b_l in t):
                return p.get("league", "")
    return ""


# ── PHOTO RESOLUTION ──────────────────────────────────────
def resolve_treatment_photos(treatments_list: list) -> list:
    """For each treatment whose fields carry a photo_query, fetch a topic-
    relevant photo and inject photo_url + photo_credit. Mutates in place +
    returns for chaining. Soft-fails when photo_fetcher isn't on the path
    so the generator still works in environments without it (e.g. CI
    runners that haven't pulled the social/ directory yet)."""
    try:
        from photo_fetcher import fetch_photo
    except ImportError:
        print("   ⚠ photo_fetcher not importable — skipping photo resolution.")
        return treatments_list

    for t in treatments_list:
        f = t.get("fields") or {}
        q = f.get("photo_query")
        if not q:
            continue
        orientation = "portrait" if t.get("size") == "story" else "squarish"
        try:
            photo = fetch_photo(q, orientation=orientation)
            f["photo_url"]    = f"file://{photo['local_path']}"
            f["photo_credit"] = photo["credit"]
            t["fields"] = f
            print(f"     · photo '{q}' ({photo.get('source','?')}) -> {Path(photo['local_path']).name}")
        except Exception as e:
            print(f"     ✗ photo resolve failed for '{q}': {e}")
    return treatments_list


def dry_run_treatments(samples: dict) -> list:
    """Build a 4-treatment drop straight from the registry samples. No
    Claude call. Useful for testing the renderer + publisher pipeline
    end to end before paying for tokens."""
    chosen = [
        ("matchup-card.html",     "ig_pick_post",     "feed"),
        ("slip-card.html",        "ig_pick_post",     "feed"),
        ("recap-card.html",       "ig_results_post",  "feed"),
        ("photo-cover-card.html", "treatments",       "story"),
    ]
    out = []
    for tpl, group, size in chosen:
        sample = samples.get(tpl)
        if not sample:
            continue
        out.append({
            "template":  tpl,
            "rationale": f"[dry-run] registry sample for {tpl}",
            "group":     group,
            "size":      size,
            "fields":    dict(sample, size=size),
        })
    return out


# ── MAIN ──────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate today's SmarterPicks social content")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Claude. Emit a sample content.json (registry samples + photo resolution only).")
    args = parser.parse_args()

    print("📡 Loading picks/results/history…")
    picks_data   = safe_load_json(PICKS_FILE,   {})
    results_data = safe_load_json(RESULTS_FILE, {})
    history_data = safe_load_json(HISTORY_FILE, {})
    samples      = safe_load_json(SAMPLES_FILE, {})

    if not samples:
        print(f"   ⚠ No registry samples at {SAMPLES_FILE.name} — treatments will be skipped.", file=sys.stderr)

    carousel_topic = pick_carousel_topic()
    print(f"   Carousel topic of the day: {carousel_topic}")

    if args.dry_run:
        print("\n🧪 Dry-run: skipping Claude, using registry samples for treatments.")
        # Stub out the standard 5 groups with minimal placeholders so the
        # downstream renderer + publisher still see a valid content.json.
        content_dict = {
            "ig_pick_post":      {"caption": "[dry-run] caption", "slide1_text": "[dry-run]", "slide2_text": "[dry-run]", "slide3_text": "[dry-run]", "hashtags": ["dryrun"]},
            "ig_results_post":   {"caption": "[dry-run]", "headline_text": "[dry-run]", "hashtags": ["dryrun"]},
            "ig_carousel_topic": {"topic": carousel_topic, "slide1_text": "[dry-run]", "slide2_text": "[dry-run]", "slide3_text": "[dry-run]", "slide4_text": "[dry-run]", "slide5_text": "[dry-run]", "caption": "[dry-run]", "hashtags": ["dryrun"]},
            "story_sequence":    ["[dry-run-1]", "[dry-run-2]", "[dry-run-3]", "[dry-run-4]", "[dry-run-5]"],
            "meme_post":         {"top_text": "[dry-run]", "bottom_text": "[dry-run]", "image_concept": "[dry-run]"},
            "treatments":        dry_run_treatments(samples),
        }
        usage_in, usage_out = 0, 0
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("❌ ANTHROPIC_API_KEY not set — refusing to run.", file=sys.stderr)
            return 1

        try:
            import anthropic
        except ImportError:
            print("❌ pip install anthropic pydantic", file=sys.stderr)
            return 1

        user_prompt = build_user_prompt(picks_data, results_data, history_data, carousel_topic, samples)

        print(f"\n🤖 Asking Claude ({MODEL_ID}) for today's content package…")
        client = anthropic.Anthropic(api_key=api_key)

        try:
            response = client.messages.parse(
                model=MODEL_ID,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_format=DailyContent,
            )
        except anthropic.APIStatusError as e:
            print(f"❌ Anthropic API error ({e.status_code}): {e.message}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"❌ Unexpected error calling Claude: {e}", file=sys.stderr)
            return 2

        content: DailyContent = response.parsed_output
        content_dict = content.model_dump()
        usage_in, usage_out = response.usage.input_tokens, response.usage.output_tokens
        print(f"   ✓ Got valid content (in: {usage_in} tok, out: {usage_out} tok)")
        print(f"   ✓ Treatments selected: {len(content_dict.get('treatments', []))}")
        for t in content_dict.get("treatments", []):
            print(f"     · {t.get('template',''):24s} -> group={t.get('group','?'):20s} size={t.get('size','feed')}")

    # Write to social/YYYY-MM-DD/content.json (UTC date so the workflow and
    # the publisher always agree on which day is "today").
    today_dir = SOCIAL_ROOT / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_dir.mkdir(parents=True, exist_ok=True)
    out_path = today_dir / "content.json"

    # Pull today's free pick out of picks.json so the renderer can paint
    # slide 2 of the carousel as a faithful copy of the live site card
    # (sport icon, league chip, "The Play" block, reasoning, tags). We
    # grab the first pick where is_premium is falsy.
    today_free_pick = None
    for p in (picks_data.get("picks") or []):
        if not p.get("is_premium", True):
            today_free_pick = p
            break

    # Parse Treatment.fields_json (string) into Treatment.fields (dict) for
    # each treatment from the Claude path. Skip the conversion when the
    # treatment already has a `fields` dict — that's the dry-run path,
    # which constructs treatments directly from registry samples and never
    # produces a fields_json string. Done BEFORE strip_ai_punct so the
    # scrubber walks the parsed dict like any other content branch.
    #
    # ALSO: force every treatment's group to "treatments" so each one
    # publishes as a standalone single-image post via ig_publisher's
    # treatments-group handler. The legacy ig_pick_post / ig_results_post
    # / ig_carousel_topic carousels stay on their original templates
    # (daily-pick-card, results-recap, educational-carousel); treatments
    # never ride along inside those carousels. This is the "post every
    # treatment, curate on IG" mode — the alternative was filtering at
    # publish time, which kept losing files to mismatched-group filters.
    #
    # AND: inject ESPN team logo URLs for matchup-card + pick-card.
    # Claude writes team_a/team_b/matchup as plain text; team_logos.py
    # maps those strings to ESPN's CDN URLs. Done here (post-Claude,
    # pre-render) so Claude never has to know about logo URLs.
    try:
        from team_logos import team_logo_url
    except ImportError:
        team_logo_url = None

    for t in (content_dict.get("treatments") or []):
        if "fields_json" in t:
            raw = t.pop("fields_json", "") or "{}"
            try:
                t["fields"] = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"   ⚠ {t.get('template','?')}: fields_json parse failed ({e}); using empty dict")
                t["fields"] = {}
        t["group"] = "treatments"
        _inject_team_logos(t, picks_data, results_data, team_logo_url)

    # Scrub punctuation first, THEN resolve photos. Photo resolution
    # injects photo_url + photo_credit (URL strings, formatted credit
    # text) that we don't want the scrubber touching.
    cleaned_content = strip_ai_punct(content_dict)
    if cleaned_content.get("treatments"):
        cleaned_content["treatments"] = resolve_treatment_photos(cleaned_content["treatments"])

    payload = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "model":             "dry-run" if args.dry_run else MODEL_ID,
        "carousel_topic":    carousel_topic,
        "source": {
            "picks_date":      picks_data.get("date"),
            "results_date":    results_data.get("date"),
            "history_stats":   (history_data or {}).get("stats") or {},
            "today_free_pick": today_free_pick,
            "results_picks":   results_data.get("picks") or [],
        },
        "content": cleaned_content,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
