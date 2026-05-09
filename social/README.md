# SMARTERPICKS — Instagram Automation

Fully automated Instagram content generation and publishing, layered on
top of the existing `picks_generator.py` workflow. Every morning, after
the picks are scored and the new card is published, this system:

1. Reads `picks.json`, `results.json`, and `history.json`
2. Asks Claude to draft today's full Instagram content package (captions,
   carousel slides, story sequence, meme, hashtags)
3. Renders each post as a PNG using HTML/CSS templates that match the
   smarterpicks.io visual identity
4. Commits the rendered images so they deploy to the public site
5. Publishes everything to Instagram via the Graph API
6. (Optional) Pings a Discord webhook with the run summary

A separate one-time **backfill** script generates 30 evergreen posts for
grid-filling before creator outreach. A manual-trigger workflow drips
those out one at a time.

---

## Files

```
social/
├── social_generator.py          # Step 1 — Claude → content.json
├── renderer.js                  # Step 2 — content.json + templates → PNGs
├── ig_publisher.py              # Step 3 — PNGs + captions → Instagram
├── backfill_generator.py        # One-time — fills the evergreen queue
├── templates/
│   ├── _shared.css              # design tokens (matches the site exactly)
│   ├── daily-pick-card.html     # 3-slide pick announcement carousel
│   ├── results-recap.html       # yesterday's results, single image
│   ├── educational-carousel.html# 5-slide rotating-topic explainer
│   ├── meme-post.html           # bettor-humor meme
│   ├── line-movement.html       # sharp-money line move alert
│   └── member-win.html          # member testimonial card
├── 2026-05-09/                  # one directory per day, auto-created
│   ├── content.json
│   ├── manifest.json
│   ├── pick-post-1.png … pick-post-3.png
│   ├── results-recap.png
│   ├── educational-1.png … educational-5.png
│   ├── story-1.png … story-5.png
│   └── meme.png
└── backfill/                    # populated by backfill_generator.py
    ├── queue.json               # ordered queue of slots to publish
    └── 01-educational/ …        # one slot per evergreen post

.github/workflows/
├── social-daily.yml             # chains off Daily Picks via workflow_run
└── social-backfill.yml          # manual trigger — one post per run
```

---

## Data flow (daily)

```
.github/workflows/daily-picks.yml ─── (success) ───┐
                                                   ▼
.github/workflows/social-daily.yml
        │
        ├─ pip install anthropic pydantic requests
        ├─ npm install puppeteer
        │
        ├─ python social_generator.py
        │       reads picks.json + results.json + history.json
        │       calls Claude → social/YYYY-MM-DD/content.json
        │
        ├─ node renderer.js social/YYYY-MM-DD/content.json
        │       renders all PNGs into social/YYYY-MM-DD/
        │       writes manifest.json
        │
        ├─ git commit + push        ← Vercel auto-deploys
        │
        ├─ python ig_publisher.py social/YYYY-MM-DD
        │       polls https://smarterpicks.io/social/YYYY-MM-DD/*.png
        │       calls IG Graph API (container → poll → publish)
        │       posts pick carousel, results, educational carousel,
        │             meme, then 5 stories
        │       (optionally) Discord webhook summary
        │
        └─ on failure: opens a GitHub Issue tagged `social-failure`
```

---

## Required GitHub Secrets

Set under **Settings → Secrets and variables → Actions**:

| Secret                   | What it is                                                                                                   | How to get it                                                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`      | Already used by `picks_generator.py` — reused here                                                           | console.anthropic.com → API Keys                                                                                                           |
| `IG_ACCESS_TOKEN`        | Long-lived Instagram Graph API access token (~60-day TTL — refresh before it expires)                        | Meta for Developers → your app → Tools → Graph API Explorer → request `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` permissions, exchange short-lived for long-lived |
| `IG_BUSINESS_ACCOUNT_ID` | Your Instagram Business or Creator account ID (NOT username)                                                 | `GET /me/accounts?access_token=…` → grab the connected Page → `GET /{page_id}?fields=instagram_business_account&access_token=…`            |
| `DISCORD_WEBHOOK_URL`    | Optional — webhook URL for run summaries                                                                     | Discord → server → channel settings → Integrations → Webhooks → New                                                                         |
| `IG_PUBLIC_BASE_URL`     | Optional — defaults to `https://smarterpicks.io`. Override if assets live somewhere else (S3, Cloudflare, …) | —                                                                                                                                          |

---

## Voice + brand rules baked into the system prompt

The generator's system prompt enforces:

- **No hype.** Never "lock of the year", "easy money", "guaranteed".
- **Honest with losses.** Yesterday went 2-5 means we say 2-5, not "rough day, more incoming".
- **Plain English.** "Expected value", not "+EV".
- **Evidence over opinion.** Use the actual numbers from picks.json / results.json.
- **One emoji per caption max.** Usually none.
- **Hashtags in the dedicated array, never in the caption text.**
- **Always include the soft `Code FREE30 → first month free` mention** somewhere in pick + carousel CTAs.
- **21+ / responsible-gambling note** any time real money is mentioned.

If you want to change the voice, edit `SYSTEM_PROMPT` at the top of
`social_generator.py`.

---

## Visual identity

Templates pull design tokens directly from `index.html` so an Instagram
slide and a section of the site share the exact same palette + typography:

| Token             | Value                                  |
| ----------------- | -------------------------------------- |
| Background        | `#0a0a0a`                              |
| Background panels | `#131313` / `#1a1a1a`                  |
| Text              | `#f4efe4` (warm white)                 |
| Accent            | `#d4a843` (gold — site's actual accent) |
| Win               | `#6dbe7a`                              |
| Loss              | `#d96565`                              |
| Display font      | Fraunces (serif, italics for accents)  |
| Body font         | Inter Tight                            |
| Mono              | JetBrains Mono                         |

Every slide carries the same brand strip (gold square mark + SMARTERPICKS
wordmark) and the same footer (`smarterpicks.io · Code FREE30 · 1 month
free`), so the grid reads as one unit.

> **Note on FREE30:** the promo code referenced in the footer doesn't
> exist in Whop yet. Create it before posting publicly (see
> [Setup checklist](#one-time-setup-checklist) below).

---

## Daily content package (what gets posted)

| Post                  | Format            | Where it goes |
| --------------------- | ----------------- | ------------- |
| `ig_pick_post`        | 3-slide carousel  | Feed          |
| `ig_results_post`     | Single image      | Feed          |
| `ig_carousel_topic`   | 5-slide carousel  | Feed          |
| `meme_post`           | Single image      | Feed          |
| `story_sequence` (×5) | 5 vertical images | Stories       |

That's 4 grid posts + 5 stories per day. To dial down the cadence (e.g.
skip the meme on weekends), edit `ig_publisher.py`'s driver section.

The educational carousel topic rotates by day-of-month:

```python
CAROUSEL_TOPICS = [
    "What 'units' actually mean…",
    "Why your win rate matters less than your ROI",
    "Reading line movement…",
    # …12 topics total — see social_generator.py
]
```

A month of daily runs covers every angle 2–3 times.

---

## One-time setup checklist

Before this can run end-to-end, you need to do these manually:

### 1. Meta / Instagram setup

- [ ] **Convert your Instagram account to a Business or Creator account.**
      Personal accounts can't use the Content Publishing API.
- [ ] **Connect your Instagram account to a Facebook Page.**
      Required for the Graph API.
- [ ] **Create a Meta app** at developers.facebook.com → My Apps → Create App
      → "Other" → "Business" type.
- [ ] **Add the Instagram Graph API and Facebook Login products** to the app.
- [ ] **Generate a long-lived access token** with these scopes:
      `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
      `pages_read_engagement`, `business_management`.
      Exchange the short-lived one via the OAuth flow → store as `IG_ACCESS_TOKEN`.
- [ ] **Note: long-lived tokens last ~60 days.** Set a calendar reminder to
      refresh — the workflow opens an issue if publishing fails, but you
      want to refresh BEFORE it breaks.
- [ ] **Find your Instagram Business Account ID** (not username):
      ```bash
      curl "https://graph.facebook.com/v21.0/me/accounts?access_token=YOUR_TOKEN"
      # find the Page → grab its id
      curl "https://graph.facebook.com/v21.0/PAGE_ID?fields=instagram_business_account&access_token=YOUR_TOKEN"
      # the returned id is your IG_BUSINESS_ACCOUNT_ID
      ```

### 2. GitHub Secrets

- [ ] Add `ANTHROPIC_API_KEY` (already exists — reused).
- [ ] Add `IG_ACCESS_TOKEN`.
- [ ] Add `IG_BUSINESS_ACCOUNT_ID`.
- [ ] (Optional) Add `DISCORD_WEBHOOK_URL` for run summaries.
- [ ] (Optional) Add `IG_PUBLIC_BASE_URL` only if your deployed origin
      isn't `https://smarterpicks.io`.

### 3. Whop setup

- [ ] Create the **`FREE30` promo code** in your Whop product (one month
      free for first-time subscribers). Templates and captions reference
      it by name.

### 4. First test run

- [ ] In a terminal, locally:
      ```bash
      pip install anthropic pydantic requests
      npm install puppeteer
      ANTHROPIC_API_KEY=... python social/social_generator.py
      node social/renderer.js "social/$(date -u +%F)/content.json"
      open social/$(date -u +%F)/*.png   # eyeball the rendered cards
      ```
- [ ] Push to a feature branch and run **social-daily.yml** manually via
      `workflow_dispatch`. Watch the logs. Confirm the IG publish step
      reports `media_id` for each post.
- [ ] Once happy, let the chained `workflow_run` trigger handle it daily.

### 5. Backfill

- [ ] Run the backfill generator locally ONCE:
      ```bash
      ANTHROPIC_API_KEY=... python social/backfill_generator.py
      ```
      This creates 30 slot directories under `social/backfill/` and a
      `queue.json` listing them.
- [ ] Commit + push the resulting tree.
- [ ] **Manually review every testimonial-style slot** and replace the
      placeholder `@handle` with a REAL member who's given written
      permission to be quoted. Until then, dry-run those slots.
- [ ] Trigger `social-backfill.yml` manually whenever you want to publish
      one. Each run consumes the queue head and rewrites it minus that
      entry.

### 6. Account-type-specific gotchas

- [ ] If your account isn't enabled for Content Publishing API stories,
      set the workflow env var `IG_SKIP_STORIES=1` so the publisher
      skips the 5 vertical posts and only posts to the grid. (Stories
      via the Graph API require Business/Creator + a Page link.)

---

## Refreshing the long-lived IG token

Long-lived tokens expire ~60 days after issue. Refresh before the
expiry — the workflow can't refresh itself because we never store the
client secret in GitHub Actions:

```bash
curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
  --data-urlencode "grant_type=fb_exchange_token" \
  --data-urlencode "client_id=YOUR_APP_ID" \
  --data-urlencode "client_secret=YOUR_APP_SECRET" \
  --data-urlencode "fb_exchange_token=YOUR_CURRENT_TOKEN"
```

Update the `IG_ACCESS_TOKEN` secret with the new value.

---

## Local development

To iterate on a template visually without burning IG quota:

```bash
# Generate today's content.json (uses real picks.json / results.json data)
ANTHROPIC_API_KEY=... python social/social_generator.py

# Render to PNGs
node social/renderer.js "social/$(date -u +%F)/content.json"

# View
open social/$(date -u +%F)/*.png
```

To test the publisher without actually posting:

```bash
IG_DRY_RUN=1 \
IG_ACCESS_TOKEN=... \
IG_BUSINESS_ACCOUNT_ID=... \
python social/ig_publisher.py "social/$(date -u +%F)"
```

`IG_DRY_RUN=1` skips the actual `media_publish` call but still walks the
full code path, so you'll catch missing fields or bad URLs.

---

## What this system intentionally does NOT do

- **No third-party schedulers** (Buffer, Later, Metricool). Direct Graph
  API only.
- **No external image hosts.** Rendered PNGs go in the repo and serve
  via Vercel.
- **No analytics ingestion.** Track what works in IG's native Insights;
  this system is a publisher, not an analytics tool.
- **No automatic reply / DM handling.** Engagement is on you (or a
  community manager).
- **No facial / member-photo gathering.** Member-win cards use type
  treatments only — no faces, ever.

---

## Troubleshooting

| Symptom                                                | Likely cause                                                        |
| ------------------------------------------------------ | ------------------------------------------------------------------- |
| `IG_ACCESS_TOKEN` valid but every publish 400s         | Token's permissions don't include `instagram_content_publish` or the IG account isn't a Business/Creator |
| Container stuck `IN_PROGRESS` then times out            | IG can't fetch the image URL — confirm Vercel deployed and the URL 200s in a browser |
| First post succeeds, all subsequent ones rate-limited  | You're posting too fast — bump `DELAY_BETWEEN_POSTS_S` in `ig_publisher.py` |
| Renderer crashes with `Failed to launch browser`       | CI runner's missing libs — usually Puppeteer's bundled Chromium needs `--no-sandbox` (already set) |
| Captions render with literal `{{caption}}` text        | A template has a typo'd placeholder — check `social/templates/*.html` |
| Stories silently skipped                                 | `IG_SKIP_STORIES=1` is set, OR your account isn't enabled for stories via the Graph API |

For workflow failures, the GitHub Issue auto-filed with label
`social-failure` includes the run URL — check the logs there first.

---

## Cost ballpark

- **Claude (daily generator):** ~3,000-4,000 tokens per day = single-digit
  cents/day on Sonnet 4.6.
- **Claude (backfill, one-time):** ~30 calls × ~3K tokens = ~$0.50 total.
- **Instagram Graph API:** free.
- **Puppeteer / Chromium:** free (runs on the GitHub Actions runner).
- **Storage:** PNGs are small (~150-300 KB each); a year of daily output is
  well under 1 GB in the repo.

If costs ever feel high, the cheapest knob is to skip the meme on weekdays
or lower the carousel slide count from 5 to 3. Both edits are < 10 lines.
