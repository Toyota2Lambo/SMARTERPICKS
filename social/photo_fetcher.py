"""
SMARTERPICKS — Stock photo fetcher
==================================
Topic-relevant stock photography for the photo-cover-card treatment.
Hits Unsplash's search API with a query (sport, team, concept), picks
the top editorial-quality result, downloads it locally so the renderer
can ingest it via file:// without a runtime network round-trip.

Behavior tiers:
  - With UNSPLASH_ACCESS_KEY set     → real topic-relevant search
  - Without key                       → Picsum random photo fallback
                                        (renderer still works end-to-end;
                                         relevance just isn't there)

Caches results both as JSON metadata (URL + credit) AND as the downloaded
JPEG, keyed by query+orientation hash. Repeated calls during a single
publish run never burn API quota or refetch from the network.

Setup for real relevance:
  1. Free dev account at https://unsplash.com/developers
  2. Register an app, copy the Access Key
  3. export UNSPLASH_ACCESS_KEY=your_key

Free tier is 50 requests/hr — far above the cadence of a daily IG pipeline.

Attribution: Unsplash requires "Photo by NAME on Unsplash" credit when
their images are used. fetch_photo() returns the formatted credit string
ready to drop into the {{photo_credit}} placeholder. The accompanying
template (photo-cover-card.html) places it in the bottom-right corner
in tiny dim mono type — present but not loud.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(os.environ.get("PHOTO_CACHE_DIR", "/tmp/spx_photos"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.unsplash.com"
TIMEOUT  = 15  # seconds


def _cache_key(query: str, orientation: str) -> str:
    """Stable filesystem-safe key from query+orientation."""
    h = hashlib.sha1(f"{query}|{orientation}".encode()).hexdigest()[:12]
    # Keep a human-readable prefix so /tmp/spx_photos/ is easy to scan.
    safe = urllib.parse.quote(query[:32], safe="").replace("%", "_")
    return f"{safe}_{h}"


def _download(url: str, dest: Path) -> Path:
    """Idempotent download — skips if file already exists and is non-empty."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SmarterPicks-Social/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        dest.write_bytes(r.read())
    return dest


def _picsum_fallback(query: str) -> dict:
    """Random Picsum photo seeded by query hash — same query always returns
    the same image, so caching stays stable. Topic-irrelevant but valid."""
    seed = int(hashlib.sha1(query.encode()).hexdigest()[:8], 16) % 10000
    url  = f"https://picsum.photos/seed/{seed}/1600/1600"
    return {
        "url":              url,
        "credit":           "Photo: Picsum (fallback — set UNSPLASH_ACCESS_KEY for relevant)",
        "photographer":     "Picsum",
        "photographer_url": "https://picsum.photos",
        "source":           "picsum",
    }


def _unsplash_search(query: str, orientation: str, token: str) -> Optional[dict]:
    """Hit Unsplash's /search/photos endpoint. Returns metadata for the top
    editorial-quality result, or None on failure / no results."""
    params = urllib.parse.urlencode({
        "query":          query,
        "orientation":    orientation,
        "per_page":       3,           # fetch a few; pick best by likes
        "content_filter": "high",
        "order_by":       "relevant",
    })
    req = urllib.request.Request(
        f"{API_BASE}/search/photos?{params}",
        headers={
            "Authorization":   f"Client-ID {token}",
            "Accept-Version":  "v1",
            "User-Agent":      "SmarterPicks-Social/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  ⚠ Unsplash search failed for '{query}': {e}")
        return None

    results = data.get("results") or []
    if not results:
        return None

    # Pick the highest-liked from the top 3 — Unsplash's "relevant" order
    # sometimes surfaces low-quality matches; likes is a decent proxy.
    p = max(results, key=lambda r: r.get("likes", 0))

    # Unsplash mandates: trigger a download tracking ping when their photo
    # is actually used. Required by their API terms even on the free tier.
    try:
        track_url = (p.get("links") or {}).get("download_location")
        if track_url:
            track_req = urllib.request.Request(
                track_url,
                headers={"Authorization": f"Client-ID {token}"},
            )
            urllib.request.urlopen(track_req, timeout=5).read()
    except Exception:
        pass  # tracking is best-effort

    return {
        "url":              p["urls"]["regular"],  # ~1080px wide, plenty
        "credit":           f"Photo: {p['user']['name']} / Unsplash",
        "photographer":     p["user"]["name"],
        "photographer_url": p["user"]["links"]["html"],
        "source":           "unsplash",
    }


def fetch_photo(query: str, orientation: str = "squarish") -> dict:
    """Fetch a stock photo matching `query`. Returns dict with:
        local_path        — absolute path to the downloaded JPEG
        url               — original CDN URL
        credit            — formatted attribution string
        photographer      — name
        photographer_url  — link to their Unsplash profile
        source            — "unsplash" or "picsum"

    Caches both metadata and the JPEG. Idempotent — safe to call repeatedly
    with the same query during a single render pass.

    `orientation` values: "landscape", "portrait", "squarish". Use "squarish"
    for feed posts (1080×1080) and "portrait" for stories (1080×1920).
    """
    key      = _cache_key(query, orientation)
    meta_pth = CACHE_DIR / f"{key}.json"
    img_pth  = CACHE_DIR / f"{key}.jpg"

    if meta_pth.exists() and img_pth.exists() and img_pth.stat().st_size > 0:
        meta = json.loads(meta_pth.read_text())
        meta["local_path"] = str(img_pth)
        return meta

    token = os.environ.get("UNSPLASH_ACCESS_KEY")
    meta  = None
    if token:
        meta = _unsplash_search(query, orientation, token)
    if meta is None:
        meta = _picsum_fallback(query)

    _download(meta["url"], img_pth)
    meta["local_path"] = str(img_pth)

    # Persist metadata (without local_path — that's recomputed each call so
    # cache survives directory moves).
    persisted = {k: v for k, v in meta.items() if k != "local_path"}
    meta_pth.write_text(json.dumps(persisted, indent=2))

    return meta


if __name__ == "__main__":
    # Quick CLI smoke test:
    #   python photo_fetcher.py "basketball arena"
    import sys
    q = " ".join(sys.argv[1:]) or "basketball court"
    result = fetch_photo(q)
    print(json.dumps(result, indent=2))
