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

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import anthropic
from pydantic import BaseModel, Field


# ── CONFIG ────────────────────────────────────────────────
MODEL_ID    = "claude-sonnet-4-6"
MAX_TOKENS  = 4000
TEMPERATURE = 0.7      # voice has personality; we want some variance

REPO_ROOT   = Path(__file__).resolve().parent.parent
PICKS_FILE  = REPO_ROOT / "picks.json"
RESULTS_FILE = REPO_ROOT / "results.json"
HISTORY_FILE = REPO_ROOT / "history.json"
SOCIAL_ROOT = REPO_ROOT / "social"

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


class DailyContent(BaseModel):
    """Top-level container for everything a day's IG run needs."""
    ig_pick_post: IGPickPost
    ig_results_post: IGResultsPost
    ig_carousel_topic: IGCarouselTopic
    story_sequence: List[str] = Field(min_length=5, max_length=5, description="Five short story texts: morning teaser, midday poll, pre-lock reminder, live tracker, night recap.")
    meme_post: MemePost


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

You will be given today's picks, yesterday's result, and the cumulative track record. Use those numbers and be specific, not vague."""


def build_user_prompt(picks_data, results_data, history_data, carousel_topic) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
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

Be specific. Use the actual numbers. Don't say "we had a good run" — say "5-2 with +3.7u". Don't say "good odds" — say "+135"."""


# ── MAIN ──────────────────────────────────────────────────
def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY not set — refusing to run.", file=sys.stderr)
        return 1

    print("📡 Loading picks/results/history…")
    picks_data   = safe_load_json(PICKS_FILE,   {})
    results_data = safe_load_json(RESULTS_FILE, {})
    history_data = safe_load_json(HISTORY_FILE, {})

    carousel_topic = pick_carousel_topic()
    print(f"   Carousel topic of the day: {carousel_topic}")

    user_prompt = build_user_prompt(picks_data, results_data, history_data, carousel_topic)

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
    print(f"   ✓ Got valid content (in: {response.usage.input_tokens} tok, out: {response.usage.output_tokens} tok)")

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

    payload = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "model":             MODEL_ID,
        "carousel_topic":    carousel_topic,
        "source": {
            "picks_date":      picks_data.get("date"),
            "results_date":    results_data.get("date"),
            "history_stats":   (history_data or {}).get("stats") or {},
            "today_free_pick": today_free_pick,
            "results_picks":   results_data.get("picks") or [],
        },
        # Scrub em dashes, arrows, smart quotes etc. one last time before
        # this content gets baked into image renders or IG captions.
        "content": strip_ai_punct(content.model_dump()),
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
