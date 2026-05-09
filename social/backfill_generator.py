"""
SMARTERPICKS — Backfill content generator
=========================================
One-time script that drafts 30 evergreen Instagram posts for grid-filling
before creator outreach. Output structure mirrors the daily generator so
the same renderer.js can produce the images.

Output:
    social/backfill/queue.json     — ordered queue of every backfill post
    social/backfill/<slot>/        — one directory per post, each with
                                     content.json + (after rendering)
                                     PNGs + manifest.json

The mix is per the spec:
    12 educational carousels  (slot kind: "educational")
    10 memes                  (slot kind: "meme")
     5 myth-vs-reality posts  (slot kind: "myth")        — single-image
     3 testimonial-style      (slot kind: "testimonial") — single-image

The "publish one per run" workflow consumes the queue head, renders +
publishes that one slot, and rewrites queue.json minus the consumed entry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal

import anthropic
from pydantic import BaseModel, Field


MODEL_ID    = "claude-sonnet-4-6"
MAX_TOKENS  = 4500
TEMPERATURE = 0.8     # slightly higher — we want variety across 30 posts

REPO_ROOT  = Path(__file__).resolve().parent.parent
BACKFILL_ROOT = REPO_ROOT / "social" / "backfill"

# 30-slot mix
EDUCATIONAL_TOPICS = [
    "Win rate vs ROI — which one actually pays your rent",
    "What 'units' mean and why we don't post in dollars",
    "How to read a line move (and when to ignore it)",
    "The math of -110 — what 'juice' actually costs you",
    "CLV explained without a stats degree",
    "Bankroll: the only stat that keeps you in the game",
    "Public money vs sharp money — how to spot the difference",
    "Parlays: where the math goes to die",
    "Player props vs game lines — different markets, different edges",
    "Hedging — three good reasons, four bad ones",
    "Variance: why even great cappers have losing months",
    "The Kelly criterion (the simplest version)",
]

MYTH_TOPICS = [
    "Myth: '80% lock' picks. Reality: 53-58% is elite.",
    "Myth: more picks = more profit. Reality: edge per pick matters.",
    "Myth: betting favorites is safer. Reality: -300 isn't 'safe' — it's expensive.",
    "Myth: hot streaks are real. Reality: variance fools everyone.",
    "Myth: chasing losses works if you 'just hit one'. Reality: no.",
]


class CarouselSlide(BaseModel):
    role: Literal["title", "setup", "insight", "example", "payoff"]
    text: str = Field(description="1-3 short lines, optimized for an image card.")


class BackfillEducational(BaseModel):
    kind:        Literal["educational"]
    topic:       str
    slides:      List[CarouselSlide] = Field(min_length=5, max_length=5)
    caption:     str
    hashtags:    List[str] = Field(min_length=8, max_length=18)


class BackfillMeme(BaseModel):
    kind:          Literal["meme"]
    top_text:      str = Field(max_length=110)
    bottom_text:   str = Field(max_length=110)
    image_concept: str
    caption:       str
    hashtags:      List[str] = Field(min_length=6, max_length=14)


class BackfillMyth(BaseModel):
    kind:        Literal["myth"]
    headline:    str = Field(description="The big-text overlay — 1 line, the punch.")
    subhead:     str = Field(description="2-3 line subhead expanding the reality.")
    caption:     str
    hashtags:    List[str] = Field(min_length=8, max_length=16)


class BackfillTestimonial(BaseModel):
    kind:        Literal["testimonial"]
    quote:       str = Field(description="Member quote — 1-3 sentences. Realistic, NOT hype.")
    handle:      str = Field(description="Plausible-looking handle, e.g. @matty_bets — operator must replace with a real one.")
    tier:        str = Field(description="e.g. 'Premium · 4 months'")
    result:      str = Field(description="Big number, e.g. '+18u'")
    result_note: str = Field(description="Tiny label under the number, e.g. 'since joining'")
    caption:     str
    hashtags:    List[str] = Field(min_length=8, max_length=16)


# ── PROMPTS ───────────────────────────────────────────────
SYSTEM_PROMPT = """You write Instagram content for SmarterPicks — a daily AI-powered sports-betting analysis service. You're producing EVERGREEN posts (not tied to today's slate) for a one-time backfill.

VOICE — non-negotiable:
- Confident but humble. Direct. Plain English. No hype.
- We post wins AND losses publicly. We talk about variance. We never claim "lock" / "guaranteed".
- 53-58% win rate is the honest target — anyone claiming 80% is selling something.
- Subscription is $29/mo or $199/yr through Whop. Code FREE30 = first month free.
- 21+ only. Bet responsibly. Soft RG mention any time we talk real money.

FORMAT RULES:
- Slide texts are tight. They render onto image cards — 1-3 short lines each.
- Captions are 80-260 chars. Hashtags go in the dedicated array, never in caption.
- For testimonials: the quote should sound like a REAL person talking, not marketing copy. Specific, conversational. The operator will replace the handle with a real member who's given permission — your @handle is a placeholder.
- For memes: self-deprecating bettor humor only. Never punching down. Never women / specific ethnic groups / political punchlines.
- For "myth vs reality": the headline is the myth, the subhead is the honest correction with a number when possible."""


def carousel_prompt(topic: str) -> str:
    return f"""Write a 5-slide educational carousel.

TOPIC: {topic}

The slides build on each other:
1. title — single bold question or claim. Hook.
2. setup — name the misconception or starting point.
3. insight — the key correction. The "actually…".
4. example — a concrete worked number, not abstract advice.
5. payoff — wrap-up + soft "this is how we think about picks at SmarterPicks" (NOT a hard sell).

Use the actual topic words. Don't drift. Each slide is 1-3 short lines max."""


def meme_prompt(slot_index: int) -> str:
    return f"""Write a single bettor-humor meme post (#{slot_index} of 10).

Format: top_text = setup, bottom_text = punchline.
Subject: variance, bad beats, parlays, sweating live games, the bartender's pick, the guy at the bar with the "lock", chasing one bet, etc.
Tone: self-deprecating. We've all been there. NEVER punching down at users.
image_concept: one sentence describing the visual (the renderer ignores it; it's used for alt text + Discord summary)."""


def myth_prompt(myth: str) -> str:
    return f"""Write a "myth vs reality" post.

MYTH: {myth}

Output:
- headline: the myth as the big-text overlay (1 line, ≤ 70 chars)
- subhead: the honest correction (2-3 short lines, with a number when you can)
- caption: 100-220 chars, conversational, ends with a soft CTA (no "DM me")."""


def testimonial_prompt(slot_index: int) -> str:
    return f"""Write a placeholder member testimonial post (#{slot_index} of 3).

The OPERATOR will swap the handle with a REAL member who has given written permission before posting. Until then the @handle is a placeholder.

The quote must sound like a real person, not marketing copy:
- Specific (mention a sport / type of play / how long they've been a member).
- Acknowledge variance honestly ("not every day is green, but…").
- Avoid superlatives ("best service ever" / "life-changing").
- 1-3 sentences max.

Result number should be plausible (e.g. +12u, +24u — NOT +500u). result_note is small ("since joining", "in 3 months", etc.)."""


# ── DRIVER ────────────────────────────────────────────────
def slot_id(kind: str, idx: int) -> str:
    return f"{idx:02d}-{kind}"


def call_model(client, schema, prompt: str):
    return client.messages.parse(
        model=MODEL_ID,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )


def build_render_payload(slot_kind: str, draft) -> dict:
    """Convert one of our backfill draft objects into the same payload
    shape that renderer.js expects (it keys off `content.<group>`).
    Each backfill slot maps to ONE renderable group so we can publish
    one slot at a time."""
    if slot_kind == "educational":
        return {
            "ig_carousel_topic": {
                "topic":       draft.topic,
                "slide1_text": draft.slides[0].text,
                "slide2_text": draft.slides[1].text,
                "slide3_text": draft.slides[2].text,
                "slide4_text": draft.slides[3].text,
                "slide5_text": draft.slides[4].text,
                "caption":     draft.caption,
                "hashtags":    draft.hashtags,
            },
        }
    if slot_kind == "meme":
        return {
            "meme_post": {
                "top_text":      draft.top_text,
                "bottom_text":   draft.bottom_text,
                "image_concept": draft.image_concept,
                # caption/hashtags read by publisher from same group
                "caption":       draft.caption,
                "hashtags":      draft.hashtags,
            },
        }
    if slot_kind == "myth":
        # We piggyback on the results-recap template visually — same big
        # headline + subhead structure. The renderer's results-recap
        # template doesn't know about myth posts; the daily workflow
        # never uses this group. The backfill workflow renders these
        # via a small results-recap-style adapter (see backfill_render
        # helper in workflow), or you can swap to a dedicated myth
        # template later if you build one.
        return {
            "myth_post": {
                "headline":  draft.headline,
                "subhead":   draft.subhead,
                "caption":   draft.caption,
                "hashtags":  draft.hashtags,
            },
        }
    if slot_kind == "testimonial":
        return {
            "member_win": {
                "quote":       draft.quote,
                "handle":      draft.handle,
                "tier":        draft.tier,
                "result":      draft.result,
                "result_note": draft.result_note,
                "caption":     draft.caption,
                "hashtags":    draft.hashtags,
            },
        }
    raise ValueError(f"Unknown slot kind: {slot_kind}")


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    BACKFILL_ROOT.mkdir(parents=True, exist_ok=True)
    queue_path = BACKFILL_ROOT / "queue.json"
    if queue_path.exists():
        print(f"⚠ {queue_path} already exists. Refusing to overwrite — delete it manually if you want to regenerate.", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    queue: List[dict] = []
    slot_index = 1

    # ── Educational carousels (12) ──
    for topic in EDUCATIONAL_TOPICS:
        sid = slot_id("educational", slot_index)
        print(f"📝 [{sid}] educational — {topic[:60]}…")
        try:
            resp = call_model(client, BackfillEducational, carousel_prompt(topic))
        except Exception as e:
            print(f"   ✗ {sid} failed: {e}", file=sys.stderr); continue
        write_slot(sid, "educational", resp.parsed_output)
        queue.append({"slot_id": sid, "kind": "educational", "topic": topic})
        slot_index += 1

    # ── Memes (10) ──
    for _ in range(10):
        sid = slot_id("meme", slot_index)
        print(f"📝 [{sid}] meme")
        try:
            resp = call_model(client, BackfillMeme, meme_prompt(slot_index))
        except Exception as e:
            print(f"   ✗ {sid} failed: {e}", file=sys.stderr); continue
        write_slot(sid, "meme", resp.parsed_output)
        queue.append({"slot_id": sid, "kind": "meme"})
        slot_index += 1

    # ── Myth vs reality (5) ──
    for myth in MYTH_TOPICS:
        sid = slot_id("myth", slot_index)
        print(f"📝 [{sid}] myth — {myth[:60]}…")
        try:
            resp = call_model(client, BackfillMyth, myth_prompt(myth))
        except Exception as e:
            print(f"   ✗ {sid} failed: {e}", file=sys.stderr); continue
        write_slot(sid, "myth", resp.parsed_output)
        queue.append({"slot_id": sid, "kind": "myth", "myth": myth})
        slot_index += 1

    # ── Testimonial-style (3) ──
    for _ in range(3):
        sid = slot_id("testimonial", slot_index)
        print(f"📝 [{sid}] testimonial (PLACEHOLDER — operator must replace handle)")
        try:
            resp = call_model(client, BackfillTestimonial, testimonial_prompt(slot_index))
        except Exception as e:
            print(f"   ✗ {sid} failed: {e}", file=sys.stderr); continue
        write_slot(sid, "testimonial", resp.parsed_output)
        queue.append({"slot_id": sid, "kind": "testimonial", "needs_human_swap": True})
        slot_index += 1

    queue_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model":        MODEL_ID,
        "remaining":    queue,
        "published":    [],
    }, indent=2))
    print(f"\n✅ {len(queue)} backfill slots written. Queue: {queue_path.relative_to(REPO_ROOT)}")
    print("   Run social-backfill.yml manually to publish one slot per workflow run.")
    return 0


def write_slot(sid: str, kind: str, draft) -> None:
    slot_dir = BACKFILL_ROOT / sid
    slot_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model":        MODEL_ID,
        "kind":         kind,
        "content":      build_render_payload(kind, draft),
    }
    (slot_dir / "content.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
