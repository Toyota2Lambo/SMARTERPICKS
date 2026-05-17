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
from pydantic import BaseModel, ConfigDict, Field


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


class RecapPickRow(BaseModel):
    """One row inside recap-card.pick_rows."""
    matchup: str   = Field(description="Matchup label. e.g. 'NBA · LAL @ BOS'.")
    play:    str   = Field(description="The pick itself, italic-serif. e.g. 'Celtics -4.5' or 'Under 218.5'.")
    result:  str   = Field(description="Lowercase: 'win', 'loss', or 'push'.")
    units:   float = Field(description="Net units. Positive for wins, negative for losses.")


class ChartBar(BaseModel):
    """One bar inside chart-card.bars."""
    label:  str   = Field(description="Mono row label, e.g. 'Road Dogs +3 to +6'.")
    value:  str   = Field(description="Numeric display, e.g. '7-3' or '+8.4%'.")
    width:  float = Field(description="0 to 100. Bar fill width as percentage.")
    hilite: bool  = Field(default=False, description="True for the one row that's the answer; renders gold. Others dim.")


class IndexCell(BaseModel):
    """One cell inside index-card.cells. Exactly 6 expected."""
    label: str = Field(description="Mono cell header, e.g. 'OVERALL', 'ROI', 'NET UNITS'.")
    num:   str = Field(description="Big italic numeric. e.g. '187-149' or '+8.4%' or '336'.")
    foot:  str = Field(description="Mono footnote underneath. e.g. 'ATS · units fair' or 'season-to-date'.")
    tone:  Optional[str] = Field(default=None, description="Optional accent. One of: 'accent' (gold), 'win' (green), 'loss' (red), or null for default.")


class TreatmentFields(BaseModel):
    """The full field bag for ALL eleven templates. Every field is Optional
    so a treatment only populates the subset its chosen template needs.
    extra='allow' tolerates future templates adding fields without breaking
    the schema. Anthropic's structured output reads this schema to know
    which keys are valid AND what each is for; the previous Dict[str, Any]
    shape gave it nothing concrete to fill and it returned empty dicts."""
    model_config = ConfigDict(extra="allow")

    # ── shared fields used across multiple templates ──
    size:        Optional[str] = Field(default=None, description="'feed' or 'story'. Mirrors Treatment.size — fine to leave None.")
    tag:         Optional[str] = Field(default=None, description="Top-right mono tag. e.g. \"TODAY'S FREE PICK\", 'MATCHUP', 'RECEIPTS'.")
    eyebrow:     Optional[str] = Field(default=None, description="Mono category line. e.g. 'NBA · EASTERN CONFERENCE', 'ATS RECORDS · LAST 10'.")
    matchup:     Optional[str] = Field(default=None, description="Matchup label. pick-card uses 'Lakers @ Celtics'; slip-card uses 'LAL @ BOS · 7:30 PM ET'.")
    game_time:   Optional[str] = Field(default=None, description="Tip-off / first pitch / kick. e.g. '7:30 PM ET'.")
    line:        Optional[str] = Field(default=None, description="Mono numeric line. e.g. '-4.5', 'Under 218.5', '+135'.")
    edge:        Optional[str] = Field(default=None, description="Our calculated edge. e.g. '+6.2%' or '+190 bps'.")
    confidence:  Optional[str] = Field(default=None, description="Letter grade. e.g. 'A-', 'B+', 'A'.")
    market:      Optional[str] = Field(default=None, description="Market type. e.g. 'Spread', 'Total · Under', 'Moneyline'.")
    odds:        Optional[str] = Field(default=None, description="American odds. e.g. '-110', '+135'.")

    # ── headline-class HTML fields (wrap key word/number in <em>...</em>) ──
    quote_text_html:    Optional[str] = Field(default=None, description="quote-card pull quote. <em> for accent. Up to ~14 words.")
    quote_attrib:       Optional[str] = Field(default=None, description="quote-card mono attribution. e.g. 'SMARTERPICKS · NBA'.")
    pick_headline_html: Optional[str] = Field(default=None, description="pick-card play headline, italic serif. e.g. 'Celtics cover the <em>4.5</em>'.")
    the_pick_html:      Optional[str] = Field(default=None, description="slip-card pick line. <em> for accent. e.g. 'Celtics -4.5 over <em>Lakers</em>'.")
    headline_html:      Optional[str] = Field(default=None, description="Used by cover-card, carousel-card, photo-cover-card. <em> for accent.")
    deck_html:          Optional[str] = Field(default=None, description="cover-card / photo-cover-card subhead beneath hairline. <em> for accent.")
    caption_html:       Optional[str] = Field(default=None, description="stat-card and chart-card so-what line. <em> for accent.")
    title_html:         Optional[str] = Field(default=None, description="chart-card or index-card section title. <em> for accent.")
    note_html:          Optional[str] = Field(default=None, description="index-card footnote. <em> for accent.")
    body_html:          Optional[str] = Field(default=None, description="carousel-card body copy. <em>=accent gold, <strong>=text color.")

    # ── matchup-card ──
    team_a:       Optional[str] = Field(default=None, description="Left team name.")
    team_a_class: Optional[str] = Field(default=None, description="Empty string '' or 'favored'. Marks the favored side (gets the accent + photo inset).")
    record_a:     Optional[str] = Field(default=None, description="Team A record. e.g. '24-18 ATS'.")
    stat_a:       Optional[str] = Field(default=None, description="Team A numeric headline. e.g. '115.2'.")
    team_b:       Optional[str] = Field(default=None, description="Right team name.")
    team_b_class: Optional[str] = Field(default=None, description="Empty or 'favored'.")
    record_b:     Optional[str] = Field(default=None, description="Team B record.")
    stat_b:       Optional[str] = Field(default=None, description="Team B numeric headline.")
    stat_label:   Optional[str] = Field(default=None, description="Shared label for both team stats. e.g. 'OFFENSIVE RATING', 'RUN DIFFERENTIAL'.")
    the_play:     Optional[str] = Field(default=None, description="The pick. Italic-serif gold. e.g. 'Celtics -4.5'.")

    # ── stat-card ──
    stat_value: Optional[str] = Field(default=None, description="The big number, without unit suffix. e.g. '72' or '+14.6'.")
    stat_unit:  Optional[str] = Field(default=None, description="Unit suffix. '%', 'u', or '' (empty string).")
    source:     Optional[str] = Field(default=None, description="Mono receipt line. e.g. 'n=142 · since Jan 1'.")

    # ── slip-card ──
    slip_title: Optional[str] = Field(default=None, description="Bet slip header. e.g. 'BET SLIP · 05.17.26'.")
    slip_ref:   Optional[str] = Field(default=None, description="Slip reference number. e.g. 'REF #SP-04217'.")

    # ── recap-card ──
    wins:      Optional[str] = Field(default=None, description="Win count as string. e.g. '3'.")
    losses:    Optional[str] = Field(default=None, description="Loss count as string.")
    units:     Optional[str] = Field(default=None, description="Net units string. e.g. '+3.7u'.")
    pick_rows: Optional[List[RecapPickRow]] = Field(default=None, description="recap-card pick list. 3 to 5 rows. Required for recap-card.")

    # ── carousel-card ──
    step_num:   Optional[str] = Field(default=None, description="Numeral for the ghost watermark. e.g. '02'.")
    step_label: Optional[str] = Field(default=None, description="Top-right step counter. e.g. 'STEP 02 / 04'.")

    # ── chart-card ──
    bars: Optional[List[ChartBar]] = Field(default=None, description="chart-card bar list. 4 to 6 bars. Required for chart-card.")

    # ── cover-card ──
    issue:      Optional[str] = Field(default=None, description="Issue label. e.g. 'ISSUE 042'.")
    date_label: Optional[str] = Field(default=None, description="Cover date. e.g. 'MAY 17, 2026'.")
    section:    Optional[str] = Field(default=None, description="Section line. e.g. 'NBA · FEATURE'.")

    # ── index-card ──
    cells: Optional[List[IndexCell]] = Field(default=None, description="index-card stat grid. Exactly 6 cells. Required for index-card.")

    # ── photo support (photo-aware templates only) ──
    photo_query: Optional[str] = Field(default=None, description="Stock photo search query for photo-aware templates (quote-card, matchup-card, cover-card, photo-cover-card). Concrete English 2-3 words. e.g. 'basketball arena empty', 'baseball stadium dusk', 'NHL ice closeup'. The system resolves the photo asynchronously.")


class Treatment(BaseModel):
    """One render from the modern editorial template grid (social/templates/).
    Eleven templates total, documented in templates-registry.js. You pick 3 to
    5 of these per day to complement the five standard groups above. Each
    treatment is a single image render. The renderer pulls them from
    content.treatments[] when it builds the manifest."""
    template:  str = Field(description="One of the 11 template filenames from the registry shown in the user prompt. Must end in .html.")
    rationale: str = Field(description="One short sentence on why this template fits today's narrative.")
    group:     str = Field(description="Publisher group routing. One of: ig_pick_post, ig_results_post, ig_carousel_topic, or treatments.")
    size:      str = Field(description="feed for 1080x1080 grid posts, story for 1080x1920 vertical stories.")
    fields:    TreatmentFields = Field(description="The field bag for this treatment. Populate every field that maps to the chosen template's field_example (shown in the user prompt). Leave unused fields as null/None. This is the difference between a published card and a blank one — every relevant field must be filled.")
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
    """Format today's picks into a readable block for the prompt.
    We only send pick / odds / brief reasoning — not the full card data, because
    Claude doesn't need it and shorter prompts == cheaper + lower latency."""
    picks = picks_data.get("picks") or []
    if not picks:
        return "(no picks loaded — picks.json was empty or missing)"
    lines = []
    for i, p in enumerate(picks, 1):
        league = p.get("league", "—")
        pick   = p.get("pick", "—")
        odds   = p.get("odds", "—")
        reason = (p.get("reasoning") or "").strip()
        if len(reason) > 220:
            reason = reason[:217] + "…"
        free_tag = " (FREE)" if not p.get("is_premium", True) else ""
        lines.append(f"{i}. [{league}]{free_tag} {pick} ({odds}) — {reason}")
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
- Confident but humble. Direct. Clean. Evidence-driven.
- Never hype. Never "lock of the year". Never "easy money". Never "guaranteed".
- Acknowledge variance and losing days openly. We post the losses publicly, and your captions reflect that honesty.
- We are an analysis service, not a sportsbook and not financial advice. The site says this, your captions match that posture.
- Tone is like a sharp friend explaining a play, not a Twitter degen yelling. No emoji-spam. One emoji per caption max, usually none.
- Plain English. If you'd say "expected value" out loud, write "expected value", not "+EV".

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
- For a marquee game tonight: matchup-card carries it best (the favored side gets a photo accent automatically).
- Educational thread: carousel-card across 3 to 5 numbered slides only if you commit to the whole thread.
- cover-card or photo-cover-card: at most one per day, and only when today is a genuine event (hot streak, season opener, marquee night). Skip both on quiet days.
- quote-card: a single sharp editorial pull-quote. Atmospheric, optional.
- Never repeat the same template twice in the same drop.

Field rules for treatments:
- Headlines wrap the single most important word or number in <em>...</em>. That word renders gold italic. Pick that word deliberately, never wrap a connector word like "the" or "a".
- Field names ending in _html accept inline HTML (<em>, <strong>). All other fields are plain text only.
- Plain numeric fields (line, edge, confidence) are mono-rendered. Use the conventional sportsbook formatting: "-4.5", "+135", "+6.2%", "A-".
- Photo fields: if a template supports a photo_query, set it to a short concrete English query like "basketball arena empty" or "Boston Celtics court". Two or three words. The system resolves the photo before render.
- Voice rules (no em dashes, no arrows, no smart quotes, no hype words, vary sentence length) apply to ALL treatment fields the same way they apply to the standard groups.

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

YESTERDAY'S RESULT
{results_summary(results_data)}

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
