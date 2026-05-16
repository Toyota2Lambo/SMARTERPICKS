"""
SMARTERPICKS — Instagram Graph API publisher
============================================
Reads a directory containing:
    content.json    (from social_generator.py — captions + hashtags)
    manifest.json   (from renderer.js     — which PNG belongs to which post)
    *.png           (the rendered images)

…and publishes each post type to Instagram via the Graph API:
    1. ig_pick_post       — 3-image carousel
    2. ig_results_post    — single image
    3. ig_carousel_topic  — 5-image carousel
    4. story_sequence     — 5 stories
    5. meme_post          — single image

The Graph API requires PUBLIC URLs for images. Once the workflow has
committed and Vercel has deployed, every PNG is reachable at:
    https://smarterpicks.io/social/YYYY-MM-DD/<file>.png

We wait for those URLs to return 200 before submitting to Instagram —
stops the IG API from rejecting a fresh push that hasn't deployed yet.

Required env vars:
    IG_ACCESS_TOKEN          — long-lived Instagram Graph API token
    IG_BUSINESS_ACCOUNT_ID   — your Instagram Business account ID

Optional env vars:
    IG_PUBLIC_BASE_URL       — defaults to "https://smarterpicks.io"
    DISCORD_WEBHOOK_URL      — if set, post a summary to Discord on success/fail
    IG_DRY_RUN               — set to "1" to skip the actual publish step
                                (useful for local debugging)
    IG_SKIP_STORIES          — set to "1" to skip story posts (e.g. if your
                                account isn't enabled for them)

Usage:
    python social/ig_publisher.py social/2026-05-09
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import requests


# ── CONFIG ────────────────────────────────────────────────
GRAPH_API_VERSION = "v21.0"
# Use graph.instagram.com (not graph.facebook.com) because we're auth'd
# via Instagram Business Login — tokens look like "IGAAOK..." and only
# work against the Instagram host. If you re-do the Meta app setup with
# Facebook Login for Business and get a "EAAB..." token, swap this to
# `https://graph.facebook.com/{GRAPH_API_VERSION}`. The Graph endpoints
# (/media, /media_publish, etc.) exist on both hosts with identical
# request shapes; only the hostname differs.
GRAPH_BASE        = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

# How long to wait for the deployed image URL to be reachable before
# submitting to IG. Vercel deploys are usually <30s; we give 3 min total.
IMAGE_DEPLOY_TIMEOUT_S = 180
IMAGE_DEPLOY_POLL_S    = 5

# Container creation → "FINISHED" status polling. IG can take a moment to
# fetch and process the image.
CONTAINER_POLL_TIMEOUT_S = 120
CONTAINER_POLL_INITIAL_S = 2
CONTAINER_POLL_MAX_S     = 8

# Spacing between separate posts so we don't trip rate limits with
# back-to-back submissions on the same account.
DELAY_BETWEEN_POSTS_S = 6


# ── ERRORS ────────────────────────────────────────────────
class IGPublishError(RuntimeError):
    """Wraps an Instagram Graph API failure with context."""


# ── GRAPH API HELPERS ─────────────────────────────────────
def _post(path: str, *, token: str, params: dict, timeout: int = 30) -> dict:
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    r = requests.post(url, data={**params, "access_token": token}, timeout=timeout)
    if r.status_code >= 400:
        raise IGPublishError(f"POST {path} → {r.status_code}: {r.text[:600]}")
    return r.json()


def _get(path: str, *, token: str, params: dict, timeout: int = 20) -> dict:
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    r = requests.get(url, params={**params, "access_token": token}, timeout=timeout)
    if r.status_code >= 400:
        raise IGPublishError(f"GET {path} → {r.status_code}: {r.text[:600]}")
    return r.json()


def _wait_for_container(container_id: str, *, token: str) -> None:
    """Poll the container's status_code until it's FINISHED. Backoff is
    exponential up to CONTAINER_POLL_MAX_S so we don't hammer the API on
    a slow image."""
    deadline = time.time() + CONTAINER_POLL_TIMEOUT_S
    delay = CONTAINER_POLL_INITIAL_S
    while time.time() < deadline:
        info = _get(container_id, token=token, params={"fields": "status_code,status"})
        status = info.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise IGPublishError(f"Container {container_id} failed: {info}")
        # IN_PROGRESS / EXPIRED / PUBLISHED also fall through; sleep + retry
        time.sleep(delay)
        delay = min(delay * 1.5, CONTAINER_POLL_MAX_S)
    raise IGPublishError(f"Container {container_id} did not reach FINISHED within {CONTAINER_POLL_TIMEOUT_S}s")


def _wait_for_image_url(image_url: str) -> None:
    """Block until the image URL returns 200. Vercel takes a moment to
    deploy after a push; we don't want to submit to IG before the asset
    is live."""
    deadline = time.time() + IMAGE_DEPLOY_TIMEOUT_S
    while time.time() < deadline:
        try:
            r = requests.head(image_url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(IMAGE_DEPLOY_POLL_S)
    raise IGPublishError(f"Image URL not reachable within {IMAGE_DEPLOY_TIMEOUT_S}s: {image_url}")


# ── HIGH-LEVEL PUBLISH OPERATIONS ─────────────────────────
def publish_single_image(
    *,
    image_url: str,
    caption: str,
    token: str,
    account_id: str,
    is_story: bool = False,
) -> str:
    """Create a single-image container, wait for it to be ready, publish it."""
    _wait_for_image_url(image_url)
    params = {"image_url": image_url, "caption": caption}
    if is_story:
        params["media_type"] = "STORIES"
        # Stories don't take captions in the legacy sense — the value
        # is ignored by IG but harmless to send.
    container = _post(f"{account_id}/media", token=token, params=params)
    container_id = container["id"]
    _wait_for_container(container_id, token=token)
    pub = _post(f"{account_id}/media_publish",
                token=token,
                params={"creation_id": container_id})
    return pub["id"]


def publish_carousel(
    *,
    image_urls: List[str],
    caption: str,
    token: str,
    account_id: str,
) -> str:
    """Create child containers for each image, wait for all, then create
    + publish a parent CAROUSEL container that references them."""
    if not 2 <= len(image_urls) <= 10:
        raise IGPublishError(f"Carousel needs 2-10 images, got {len(image_urls)}")

    # Wait for every URL to deploy first — fail fast before spending
    # IG container quota on broken inputs.
    for url in image_urls:
        _wait_for_image_url(url)

    child_ids: List[str] = []
    for url in image_urls:
        c = _post(f"{account_id}/media",
                  token=token,
                  params={"image_url": url, "is_carousel_item": "true"})
        cid = c["id"]
        _wait_for_container(cid, token=token)
        child_ids.append(cid)

    parent = _post(f"{account_id}/media",
                   token=token,
                   params={
                       "media_type": "CAROUSEL",
                       "children":   ",".join(child_ids),
                       "caption":    caption,
                   })
    parent_id = parent["id"]
    _wait_for_container(parent_id, token=token)
    pub = _post(f"{account_id}/media_publish",
                token=token,
                params={"creation_id": parent_id})
    return pub["id"]


# ── CAPTION ASSEMBLY ──────────────────────────────────────
def build_caption(text: str, hashtags: Optional[Iterable[str]] = None) -> str:
    """Append hashtags to the caption with a blank line of separation —
    looks cleaner in the IG feed than tags inline."""
    body = (text or "").rstrip()
    tags = hashtags or []
    tag_line = " ".join(f"#{t.lstrip('#').strip()}" for t in tags if t and str(t).strip())
    return f"{body}\n\n{tag_line}".strip() if tag_line else body


# ── DISCORD NOTIFICATION ──────────────────────────────────
def notify_discord(message: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:1900]}, timeout=8)
    except Exception as e:
        print(f"   ⚠ Discord notify failed (non-fatal): {e}", file=sys.stderr)


# ── DRIVER ────────────────────────────────────────────────
def files_for_group(manifest: dict, group: str) -> List[str]:
    """Return the rendered filenames for a given content group, in
    slide order (so carousels post in the right sequence)."""
    items = [r for r in manifest.get("renders", []) if r.get("group") == group]
    items.sort(key=lambda r: (r.get("slide_index") or 1))
    return [r["file"] for r in items]


VALID_ONLY_TOKENS = {
    "ig_pick_post", "ig_results_post", "ig_carousel_topic", "meme_post",
    "story:1", "story:2", "story:3", "story:4", "story:5", "stories",
}


def parse_only_arg(argv: list) -> set:
    """Parses `--only=<token>[,<token>...]` from argv. Returns the set of
    selected tokens (empty set = publish everything, default behaviour).

    Valid tokens (case-insensitive, dashes/colons accepted):
      ig_pick_post · ig_results_post · ig_carousel_topic · meme_post
      stories · story:1 · story:2 · story:3 · story:4 · story:5

    Designed to be called by the staggered-posting workflows so each
    cron slot publishes exactly one piece from the morning-rendered
    batch.
    """
    selected: set = set()
    for arg in argv:
        if not arg.startswith("--only="):
            continue
        for tok in arg.split("=", 1)[1].split(","):
            tok = tok.strip().lower()
            if not tok:
                continue
            if tok not in VALID_ONLY_TOKENS:
                print(f"⚠ Unknown --only token '{tok}' — ignored. Valid: {sorted(VALID_ONLY_TOKENS)}", file=sys.stderr)
                continue
            selected.add(tok)
    return selected


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not positional:
        print("Usage: python social/ig_publisher.py <social/YYYY-MM-DD> [--only=<token>,...]", file=sys.stderr)
        return 1

    only = parse_only_arg(sys.argv[1:])
    def should_run(token: str) -> bool:
        # Empty set = publish everything (back-compat with old usage)
        return not only or token in only

    day_dir = Path(positional[0]).resolve()
    content_path  = day_dir / "content.json"
    manifest_path = day_dir / "manifest.json"
    if not content_path.exists() or not manifest_path.exists():
        print(f"❌ Missing {content_path.name} or {manifest_path.name} in {day_dir}", file=sys.stderr)
        return 1

    token      = os.environ.get("IG_ACCESS_TOKEN")
    account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID")
    if not token or not account_id:
        print("❌ IG_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID must both be set.", file=sys.stderr)
        return 1

    # `or` (not just `.get(..., default)`) because the workflow passes
    # IG_PUBLIC_BASE_URL through as a step-level env var even when the
    # GitHub secret/variable is unset — Python then sees an empty string,
    # not None, so .get() returns "" and we end up posting relative URLs
    # that Instagram can't fetch ("/social/...png").
    base_url     = (os.environ.get("IG_PUBLIC_BASE_URL") or "https://smarterpicks.io").rstrip("/")
    skip_stories = os.environ.get("IG_SKIP_STORIES") == "1"
    dry_run      = os.environ.get("IG_DRY_RUN") == "1"

    payload  = json.loads(content_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    content  = payload.get("content") or {}

    rel_dir = day_dir.relative_to(day_dir.parent.parent)  # e.g. "social/2026-05-09"
    def url_for(filename: str) -> str:
        return f"{base_url}/{rel_dir}/{filename}"

    summary: List[str] = []
    failures: List[str] = []

    def run(label: str, fn):
        if dry_run:
            print(f"   ↻ [dry-run] would publish {label}")
            summary.append(f"[dry-run] {label}")
            return
        try:
            mid = fn()
            summary.append(f"✓ {label} → media_id={mid}")
            print(f"   ✓ {label}: {mid}")
        except IGPublishError as e:
            failures.append(f"✗ {label}: {e}")
            print(f"   ✗ {label}: {e}", file=sys.stderr)

    print(f"📤 Publishing {rel_dir} to Instagram (account {account_id})")

    if only:
        print(f"   (selective publish — --only={sorted(only)})")

    # ── 1) Daily pick post — 2-slide carousel ──
    # We render 3 slides but only publish slides 2 + 3 in order:
    #   slide 2 (pick-post-2.png) — the gold-bordered live site card
    #   slide 3 (pick-post-3.png) — the FREE30 / full-card CTA closer
    # Slide 1 (templated headline) is skipped — reads as tacky ad copy.
    pick_files = files_for_group(manifest, "ig_pick_post")
    if should_run("ig_pick_post") and pick_files and content.get("ig_pick_post"):
        keep = [f for f in pick_files if "pick-post-2" in f or "pick-post-3" in f]
        # Ensure ordering: card first, then CTA
        keep.sort(key=lambda f: 0 if "pick-post-2" in f else 1)
        if not keep:                       # extreme fallback if renderer layout changed
            keep = pick_files[:2]
        pp  = content["ig_pick_post"]
        cap = build_caption(pp.get("caption"), pp.get("hashtags"))
        run(f"ig_pick_post (carousel — {len(keep)} slides)",
            lambda: publish_carousel(image_urls=[url_for(f) for f in keep],
                                     caption=cap, token=token, account_id=account_id))
        time.sleep(DELAY_BETWEEN_POSTS_S)

    # ── 2) Yesterday's results (single image) ──
    res_files = files_for_group(manifest, "ig_results_post")
    if should_run("ig_results_post") and res_files and content.get("ig_results_post"):
        rp = content["ig_results_post"]
        cap = build_caption(rp.get("caption"), rp.get("hashtags"))
        run("ig_results_post (image)",
            lambda: publish_single_image(image_url=url_for(res_files[0]),
                                         caption=cap, token=token, account_id=account_id))
        time.sleep(DELAY_BETWEEN_POSTS_S)

    # ── 3) Educational carousel (5-slide carousel) ──
    edu_files = files_for_group(manifest, "ig_carousel_topic")
    if should_run("ig_carousel_topic") and edu_files and content.get("ig_carousel_topic"):
        ep = content["ig_carousel_topic"]
        cap = build_caption(ep.get("caption"), ep.get("hashtags"))
        run("ig_carousel_topic (carousel)",
            lambda: publish_carousel(image_urls=[url_for(f) for f in edu_files],
                                     caption=cap, token=token, account_id=account_id))
        time.sleep(DELAY_BETWEEN_POSTS_S)

    # ── 4) Meme post (single image) ──
    meme_files = files_for_group(manifest, "meme_post")
    if should_run("meme_post") and meme_files and content.get("meme_post"):
        mp = content["meme_post"]
        # Memes have no hashtags field — use a small fixed set for discoverability.
        cap = build_caption(mp.get("image_concept", ""), ["sportsbetting", "bettorsbelike", "smarterpicks"])
        run("meme_post (image)",
            lambda: publish_single_image(image_url=url_for(meme_files[0]),
                                         caption=cap, token=token, account_id=account_id))
        time.sleep(DELAY_BETWEEN_POSTS_S)

    # ── 5) Stories (5 vertical posts) ──
    # --only=stories selects all five; --only=story:N selects exactly one.
    if skip_stories:
        summary.append("(stories skipped via IG_SKIP_STORIES=1)")
    else:
        story_files = files_for_group(manifest, "story_sequence")
        for i, f in enumerate(story_files, 1):
            tok = f"story:{i}"
            if not (should_run("stories") or should_run(tok)):
                continue
            run(f"story {i}/{len(story_files)}",
                lambda f=f: publish_single_image(image_url=url_for(f),
                                                 caption="", token=token,
                                                 account_id=account_id,
                                                 is_story=True))
            time.sleep(DELAY_BETWEEN_POSTS_S)

    # ── Summary + Discord notification ──
    print("\n📊 Summary:")
    for s in summary: print(f"   {s}")
    if failures:
        print("\n⚠ Failures:")
        for f in failures: print(f"   {f}")

    notify_lines = [
        f"**SmarterPicks IG run · {rel_dir}**",
        *(summary + [""] + failures if failures else summary),
    ]
    notify_discord("\n".join(notify_lines))

    # Non-zero exit only if EVERY post failed — partial success is still
    # useful and shouldn't block the workflow's commit step.
    if failures and not [s for s in summary if s.startswith("✓")]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
