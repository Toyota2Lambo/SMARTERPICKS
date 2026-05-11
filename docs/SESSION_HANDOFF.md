# SMARTERPICKS — Session handoff

Comprehensive context dump from the long session that ended at commit
`ceb98be`. Read this before touching the codebase if you're a new
agent (or operator coming back after a break).

Two related docs live alongside this one:
- `docs/CODEX_HANDOFF.md` — a prompt-style instruction for an agent that
  audits integrations and produces a manual setup checklist.
- `social/README.md` — operator-facing setup for the Instagram pipeline.

---

## 1. What this project is

**SMARTERPICKS** is an AI-powered sports betting analysis service.

- **Domain:** `smarterpicks.io`, deployed on **Vercel**.
- **Stack:** Static HTML/CSS/vanilla JS (no build step), Python 3.11 for
  the daily generator (runs in GitHub Actions), Node 20 + Puppeteer
  for social-image rendering, two CommonJS Vercel serverless functions
  in `api/`.
- **Auth + payments:** Whop (OAuth with PKCE, subscription managed by
  Whop's checkout + customer portal). The site never touches money.
- **The product loop:** Picks generated every morning before lock,
  scored against final game data the next morning, results published
  publicly, full per-pick archive grows daily.
- **The marketing loop:** Public landing page leads with aggregate
  trust numbers; archive is the receipts; members area is the full
  day-by-day picture; Instagram pipeline drips content through the day.

GitHub repo: **github.com/Toyota2Lambo/SMARTERPICKS**, main branch.

---

## 2. File map (annotated)

### Top-level HTML pages (clean URLs via `vercel.json`)

| File | URL | Purpose | Notes |
|---|---|---|---|
| `index.html` | `/` | Marketing landing | Hero stats, slate preview (3-pick free cap), how it works, pricing, simplified track-record, FAQ, newsletter, final CTA |
| `members.html` | `/members` | Members area (gated) | Yesterday's banner, today's full slate, recent days strip, interactive chart |
| `billing.html` | `/billing` | Subscription manager | Status card + button to Whop customer portal |
| `archive.html` | `/archive` | Public pick archive | Trust headlines, $-per-unit calculator, sport/outcome/range filters, expandable day cards. Defaults to "Wins" filter |
| `discord.html` | `/discord` | Discord community page | Free + premium tier descriptions, channel preview |
| `login.html` | `/login` | Whop OAuth entry + plan picker | Subscribes through Whop checkout; existing members sign in |
| `callback.html` | `/callback.html` | OAuth callback | **MUST KEEP `.html` extension** — registered with Whop's app dashboard as redirect URI |
| `terms.html` | `/terms` | Terms of Service | Real legal copy |
| `privacy.html` | `/privacy` | Privacy Policy | Real legal copy; required URL in Meta app config |
| `disclaimer.html` | `/disclaimer` | Risk disclaimer + responsible-gambling resources | Real legal copy |
| `404.html` | `/404` | Static fallback | Linked from Vercel's default |

### JS

| File | Loaded on | Purpose |
|---|---|---|
| `whop-config.js` | `index`, `login`, `callback`, `members` | Single source of truth for Whop CLIENT_ID, PRODUCT_ID, checkout URLs, OAuth endpoints |
| `auth-chrome.js` | `index`, `archive`, `discord`, `billing` | Detects member / no-access / guest state, sets `body[data-auth-state]`, injects green/gold status bar for signed-in users, wires `[data-action="logout"]` |
| `picks-loader.js` | `index`, `members` | Whop API membership check, picks rendering, lock overlays, 3-card free-page cap, "more behind paywall" tile, AI chat panel, 3D card tilt, confidence meter, checkout-button wire-up (`wireCheckoutLinks()`) |

### Data files (live, written by the daily workflow — DO NOT regenerate by hand)

| File | Schema (high-level) | Written by |
|---|---|---|
| `picks.json` | `{ date, picks: [{ league, pick, odds, stake, reasoning, is_premium, ... }] }` | `picks_generator.py` Step 1 |
| `results.json` | `{ date, wins, losses, pushes, net_units, picks: [{ ..., result, units }] }` | `picks_generator.py` scoring step |
| `history.json` | `{ stats, daily: [{ date, units, cumulative }] }` | `picks_generator.py` (appends) |
| `archive.json` | `{ stats, days: [{ iso_date, wins, losses, net_units, picks }] }` | `picks_generator.py` `append_to_archive()` |

### Python (generator + social)

| File | Purpose |
|---|---|
| `picks_generator.py` | Daily pipeline: fetch odds → compute per-market consensus → fetch ESPN context (MLB starters, NBA/NHL injuries) → Claude generates picks → score yesterday's picks → write `picks.json`, `results.json`, `history.json`, `archive.json` |
| `social/social_generator.py` | Reads picks/results/history → Claude generates structured JSON content (caption + slide text + hashtags) for 5 post types |
| `social/renderer.js` | Puppeteer renders the templates × content → 1080×1080 grid PNGs + 1080×1920 story PNGs |
| `social/ig_publisher.py` | Posts to Instagram Graph API via Instagram Login flow (`graph.instagram.com`). Supports `--only=<token>[,<token>,...]` for selective publish |
| `social/backfill_generator.py` | One-off: 30 evergreen posts (educational, memes, myth-vs-reality, testimonial-style) for pre-launch grid filling |
| `api/claude-chat.js` | Vercel serverless: in-card "Ask Claude" chat proxy. Has per-IP rate limiting (in-memory, 30/min/IP) |
| `api/newsletter.js` | Vercel serverless: forwards newsletter signups to `NEWSLETTER_WEBHOOK_URL`. **HALF-WIRED** — operator needs to point at a real ESP |

### Workflows

| File | Trigger | What it does |
|---|---|---|
| `.github/workflows/daily-picks.yml` | Cron `5 13 * * *` (~9 AM ET in EDT) + manual | Runs `picks_generator.py`, commits data files with retry-on-rebase, files an issue on failure |
| `.github/workflows/social-daily.yml` | `workflow_run` after daily-picks succeeds + manual | Generates social content, renders PNGs, commits assets, publishes **only the morning hero post** (`--only=ig_pick_post,story:1`) |
| `.github/workflows/social-stagger.yml` | 4 daily cron times (16:00 / 21:00 / 00:00 / 03:30 UTC) + manual | Each cron slot publishes one piece of the morning's pre-rendered content via `--only=`. Stories 2-5 + results recap + educational carousel + meme spread through the day |
| `.github/workflows/social-backfill.yml` | Manual only | Publishes one pre-generated evergreen post per run, for pre-launch ramp |

### Docs

| File | For |
|---|---|
| `docs/SESSION_HANDOFF.md` | **This file.** Context for incoming agent/operator |
| `docs/CODEX_HANDOFF.md` | Prompt for an audit agent (Codex / Claude) to finish integration wiring |
| `social/README.md` | Operator setup for Instagram (Meta app, tokens, secrets, troubleshooting) |

---

## 3. The daily pipeline, end-to-end

Every morning, in order:

1. **`daily-picks.yml` fires at 13:05 UTC** (workflow_dispatch also supported).
2. **`picks_generator.py` runs Step 0** — score yesterday's picks:
   - Reads existing `picks.json` (yesterday's picks).
   - Pulls last 2 days of completed scores from The Odds API.
   - Scores each pick with `score_pick_string()` (handles spreads, totals, MLs; strips parenthetical commentary before regex; player props fall through to PENDING).
   - Writes fresh `results.json`.
   - Appends to `history.json`.
   - Appends day to `archive.json` (via `append_to_archive()`).
3. **`picks_generator.py` Step 1** — generate today's picks:
   - Fetches today's games from The Odds API across NBA/MLB/NHL/NFL.
   - For each game, computes `_consensus` per market (best price, best book, median, edge in bps, n_books).
   - Fetches ESPN context: MLB probable starters per game (with ERA), NBA/NHL injury feeds. Best-effort, non-fatal.
   - Calls Claude with: enriched games JSON + ESPN context block + adaptive pick target (`target_pick_count(num_games)` → 3-7 picks based on slate size).
   - Prompt enforces: `edge_bps >= 100`, max 1 heavy ML favorite (<-150), no parentheticals in pick strings, "fewer high-quality > more filler".
   - Writes `picks.json` with `generated_at` timestamp.
4. **Commit** all four JSON files with retry-on-rebase.
5. **`social-daily.yml` triggers via `workflow_run`** (only if daily-picks succeeded).
6. **`social_generator.py`** reads the fresh JSONs, calls Claude for the social content package, writes `social/YYYY-MM-DD/content.json`.
7. **`renderer.js`** Puppeteer-renders the templates into PNGs in `social/YYYY-MM-DD/`. Writes `manifest.json`.
8. **Commit + push** the asset directory (retry-on-rebase).
9. **`ig_publisher.py --only=ig_pick_post,story:1`** publishes the headline pick carousel and morning Story.
10. **Through the day, `social-stagger.yml`** fires at 16:00 / 21:00 / 00:00 / 03:30 UTC, each running `ig_publisher.py --only=<token>` to drop one piece. See §6 for the schedule table.

Both workflows file deduped GitHub Issues on failure (label: `social-failure`).

---

## 4. External integrations status

| Service | Status | Env / config | Notes |
|---|---|---|---|
| **Anthropic API** | ✅ Wired | `ANTHROPIC_API_KEY` (GitHub Secret + Vercel env) | Used by picks_generator.py, api/claude-chat.js, social_generator.py |
| **The Odds API** | ✅ Wired | `ODDS_API_KEY` (GitHub Secret) | Daily generator + score fetching |
| **ESPN public JSON** | ✅ Wired, best-effort | None (no key) | MLB starters + NBA/NHL injuries; failures are non-fatal |
| **Whop OAuth** | ✅ Wired | `whop-config.js` (CLIENT_ID, PRODUCT_ID, plan URLs) | PKCE flow; tokens in localStorage; long-lived token refresh via `/oauth/token` |
| **Instagram Graph API** | ✅ Wired and tested | `IG_ACCESS_TOKEN`, `IG_BUSINESS_ACCOUNT_ID` (GitHub Secrets) | Uses **`graph.instagram.com`** (Instagram Business Login), NOT `graph.facebook.com`. Token expires every ~60 days |
| **Discord webhook** | Optional, no-op if unset | `DISCORD_WEBHOOK_URL` (GitHub Secret) | Run summaries from social workflow |
| **Newsletter ESP** | ❌ HALF-WIRED | `NEWSLETTER_WEBHOOK_URL` env unset | `api/newsletter.js` exists and forwards POSTs; operator needs to point it at Beehiiv / ConvertKit / Resend / etc. |
| **Twitter (X)** | ❌ Not wired | n/a | Operator declined — requires $100/mo Basic tier on X API. Architecture in place to add later (mirror `ig_publisher.py` + cron slots) |
| **Analytics / error monitoring** | ❌ Not installed | n/a | Plausible / Vercel Analytics / Sentry recommended; operator hasn't consented to any |

---

## 5. Site architecture decisions (the why behind things)

### Marketing page vs members area split
- **`index.html` is sales-focused.** No daily-detail content (yesterday banner removed, chart removed, daily-picks table removed). Only aggregate stats from `history.json`. A "What you get every morning" feature grid replaced the old daily-results cards.
- **`members.html` is transparency-focused.** Full slate, yesterday's banner, recent days strip, interactive chart with range tabs / hover crosshair / per-day point markers.
- **`archive.html` is the public verification page.** Defaults to "Wins" outcome filter (with visible disclosure) for marketing punch, but trust headlines stay aggregate (all-time, never filtered) and the calculator label says "winners only" when the filter is on so it can't deceive.

### Free landing slate caps at 3 picks
In `picks-loader.js`'s `loadPicks()`: non-members on index.html see at most 3 cards (1 free unlocked + 2 locked premium teasers), followed by a `renderMoreTile()` that says "+N more picks behind the paywall · Unlock $29/mo". Spamming 6 blurred cards reads desperate; showing just enough to prove there's more converts better.

### Auth chrome
`auth-chrome.js` is a single script that tags `<body data-auth-state="member|no-access|guest">` and injects a thin colored status bar on signed-in visits. Pages style elements off `[data-auth-show="member|guest|no-access"]` for show/hide. Members-only pages (members.html, billing.html) have their own `.member-bar` and the chrome script dedupes to avoid double bars.

### Clean URLs
`vercel.json` has `cleanUrls: true` and `trailingSlash: false`. All internal hrefs use the no-`.html` form. **Exception:** `/callback.html` keeps its extension because Whop has it registered as the OAuth redirect URI — renaming the file would break sign-in.

### Whop checkout buttons
Every checkout button on `index.html` is tagged `data-checkout="monthly|annual|free"` and has the correct per-plan URL hardcoded. `picks-loader.js`'s `wireCheckoutLinks()` reads `window.WHOP_CONFIG.CHECKOUT_URL_*` on load and overrides hrefs — so plan ID changes only require editing `whop-config.js`.

---

## 6. Social / Instagram automation specifics

### Token + account
- App: **SmarterPicks Publisher** in Meta dashboard. Currently **Live** mode.
- Instagram account: `@smarterpicks.io`, Business account type.
- Token format: **starts with `IGAAOK...`** (Instagram Business Login flow — NOT Facebook Login).
- **IG_BUSINESS_ACCOUNT_ID** = the value from `GET /me?fields=user_id` (the `user_id` field, NOT the `id` field — those differ in Instagram Business Login).
- Token expires every ~60 days. **Set a calendar reminder for ~July 2026** to refresh. Workflow files a `social-failure` issue when it expires.

### Token refresh procedure
1. Graph API Explorer with the app selected → generate fresh short-lived token (request scopes: `instagram_business_basic`, `instagram_business_content_publish`).
2. Exchange via `https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=<APP_SECRET>&access_token=<SHORT_LIVED>`.
3. Update `IG_ACCESS_TOKEN` in GitHub Secrets. **Do not paste tokens into chat or PRs.**

### Daily posting cadence

| Time (UTC) | Time (ET in EDT) | Source | Published |
|---|---|---|---|
| ~13:10 UTC | ~9:10 AM ET | `social-daily.yml` (after daily-picks) | Headline pick carousel + Story 1 (morning teaser) |
| 16:00 UTC | 12:00 PM ET | `social-stagger.yml` cron | Story 2 (midday poll) |
| 21:00 UTC | 5:00 PM ET | `social-stagger.yml` cron | Story 3 (pre-lock) + educational carousel |
| 00:00 UTC | 8:00 PM ET | `social-stagger.yml` cron | Story 4 (live tracker) + yesterday's results recap |
| 03:30 UTC | 11:30 PM ET | `social-stagger.yml` cron | Story 5 (night recap) + meme |

Times drift one hour later during EST (Nov–Mar) — cron has no DST awareness. Acceptable for marketing automation.

### The `--only=` flag (publisher API contract)

```
python social/ig_publisher.py <social/YYYY-MM-DD> [--only=<token>[,<token>,...]]
```

Valid tokens: `ig_pick_post`, `ig_results_post`, `ig_carousel_topic`, `meme_post`, `stories`, `story:1`, `story:2`, `story:3`, `story:4`, `story:5`.

Empty (no flag) = publish everything (legacy behavior).

---

## 7. Generator algorithm specifics

### `target_pick_count(num_games)`
`min(MAX_PICKS=7, max(MIN_PICKS=3, num_games // 4 + 2))`. A 5-game Tuesday caps at 3 picks; a 28+ game Saturday caps at 7.

### EV consensus
For every game, `compute_market_consensus(game)` walks every bookmaker × market × outcome, then per (market, outcome) records `{best, best_book, median, edge_bps, n_books}`. The prompt requires `edge_bps >= 100` (1% of implied probability) for any pick to be worth posting.

### Heavy-favorite cap
The prompt explicitly forbids more than ONE pick with odds shorter than -150 per day.

### Score parser robustness
`score_pick_string()` strips `(...)` from pick strings before regex matching, so picks like "Under 7.5 (Yamamoto pitching)" still score automatically. The prompt also instructs Claude not to write parentheticals.

### ESPN context
`fetch_espn_context(games)` pulls:
- **MLB**: probable starting pitchers per team for today's slate (with ERA if available)
- **NBA / NHL**: league-wide injury news feeds, mapped to team names

Annotates each game in the prompt's separate "context" block. Best-effort — if ESPN's undocumented endpoints change shape, the generator logs `⚠ ESPN context fetch failed (non-fatal)` and continues with odds-only picks.

---

## 8. Hard "do not touch" list

- **`callback.html` filename + `CONFIG.REDIRECT_URI`** in `callback.html` and `login.html` — `/callback.html` is registered with Whop. Renaming breaks OAuth.
- **`whop-config.js` `CLIENT_ID` and `PRODUCT_ID`** — operator-controlled, real IDs.
- **`picks.json`, `results.json`, `history.json`, `archive.json`** — live data files; never regenerate by hand. The workflow writes them daily.
- **`vercel.json`** — must not contain `// key`-style pseudo-comments. JSON has no comment syntax and Vercel's schema rejects unknown properties (this broke deploys for several days; commit `f95bffc` fixed it).
- **Social PNGs in `social/YYYY-MM-DD/`** — written by the workflow, served from Vercel; don't edit by hand.

---

## 9. Known gotchas (from hard-won experience)

1. **Instagram tokens are Instagram-scoped** (`IGAAOK...`), not Facebook-scoped (`EAAB...`). The publisher hits **`graph.instagram.com`**, not `graph.facebook.com`. If the token ever gets re-issued via Facebook Login for Business, swap the base URL constant (`GRAPH_BASE`) in `social/ig_publisher.py`.
2. **`me/accounts` returns `Tried accessing nonexisting field (accounts)` on `graph.instagram.com`** — that endpoint is Facebook-only. To find the IG account ID for Instagram Business Login tokens, use `GET /me?fields=user_id` and take the `user_id` (NOT `id`).
3. **`os.environ.get("KEY", default)` returns `""` if the env var is set-but-empty**, not the default. The workflow passes secrets through as env vars even when unset, so empty strings are common. Use `os.environ.get("KEY") or "default"`. Bit us once (commit `ba88e73`).
4. **Vercel `cleanUrls: true` does not strip `.html` for files that still need to be referenced by filename** — `/callback.html` continues working because the file is served at its literal path AND at `/callback` (Vercel handles both).
5. **GitHub Actions cron strings don't know about DST** — schedules drift one hour twice a year. Accept it or maintain two cron entries.
6. **Push races between workflows and operator pushes** — Both daily workflows now do `git push || (fetch + rebase + retry)` up to 5 times. If a workflow ever fails its commit step again, this is the protection.
7. **Tokens pasted in chat must be treated as compromised.** Happened once; rotated immediately. Never paste credentials into a chat transcript — secrets go directly to GitHub Secrets / Vercel env vars.
8. **The privacy.html URL is registered with Meta** for the app's Privacy Policy URL. Don't rename or delete the page or the Meta app's Live mode breaks.

---

## 10. Open items / what's NOT done

- **Twitter / X publishing** — operator declined (the $100/mo X API Basic tier is the blocker). Architecture is in place to mirror `ig_publisher.py` + `social-stagger.yml` slots when the operator wants to revisit.
- **Newsletter ESP destination** — `api/newsletter.js` forwards to `NEWSLETTER_WEBHOOK_URL`. Operator hasn't pointed it at a real ESP yet. When they do: set the env var in Vercel; the forwarder POSTs JSON `{email, ts}`.
- **`FREE30` promo code** — referenced in social templates and footer copy as a 1-month-free code. Operator confirmed it exists in Whop, but verify before any campaign.
- **Real testimonials** — placeholder testimonials section was removed earlier; replaced with a feature grid. If operator wants real testimonials, structure should be: card → quote → real handle → real claim (with link).
- **Vercel Analytics / Sentry** — not installed. Operator hasn't consented. Recommended in the Codex handoff doc as opt-in.
- **`support@smarterpicks.io`** — referenced in `billing.html`, `terms.html`, `privacy.html`, `disclaimer.html`. Confirm an actual mailbox exists.
- **`history.json` seed data** — the file was seeded with realistic-looking placeholder daily numbers so the chart wouldn't be empty on day 1. Real generator runs (one per day since launch) progressively overwrite the data with real results. Operator should review and decide whether to delete the seed days when comfortable.

---

## 11. Recent commit pointers (most relevant to inherit)

(Newest first; `git show <hash>` to see the diff)

| Hash | What |
|---|---|
| `ceb98be` | Per-plan Whop checkout URLs on every "Get Premium / Annual / Free" button |
| `1eca699` | Fixed 5 dead `href="#"` links found in full audit (logo, footer T&P, login T&P) |
| `eeef271` | Split social into morning hero post + 4 staggered cron slots through the day |
| `79ec7bd` | Both workflows now retry-with-rebase on push |
| `116984a` | Publisher base URL → `graph.instagram.com` (matches Instagram-issued tokens) |
| `f95bffc` | Fixed broken `vercel.json` — removed `// key` pseudo-comments that blocked all deploys |
| `ba88e73` | Fixed empty-env-var fallback in publisher (was posting relative URLs) |
| `cf5b362` | Added `docs/CODEX_HANDOFF.md` (audit prompt for next agent) |
| `daf0a93` | Clean URLs site-wide via Vercel + bulk href update across all pages |
| `3daee25` | Archive default outcome filter → "Wins" with visible disclosure |
| `c3950b0` | Big site restructure (sales-focused index, transparent members) + generator hardening (adaptive count, EV, ESPN, parser fix, heavy-fav cap) |
| `82ebb4c` | Instagram pipeline added end-to-end |
| `9a687ab` | Backend hardening pass: rate limits, real-data wiring, legal pages |
| `4fa00dd` | Auth chrome (member / no-access / guest) across every page |
| `97f1d8a` | Public pick archive (`/archive`) with filters + calculator |
| `aff56a1` | Interactive members chart (range tabs, hover, per-day markers) |
| `3277fbb` | Members area + billing page + post-login redirect to `/members` |
| `45274a0` | Original fix: workflow now commits results.json + history.json (was the very first issue diagnosed in this session) |

---

## 12. Quick smoke test checklist (run this if production looks broken)

1. `https://smarterpicks.io/` → loads, hero stats not zero, slate shows 3 cards + "more behind paywall" tile
2. `https://smarterpicks.io/archive` → trust headlines populated, calculator shows positive dollar number with "winners only" caveat
3. `https://smarterpicks.io/members` → if not signed in, bounces to `/login` (this is correct)
4. `https://smarterpicks.io/login` → "Sign in with Whop" button works, plan toggle changes the checkout URL on the Subscribe button
5. `https://smarterpicks.io/social/<today>/pick-post-1.png` → returns the actual PNG (not 404). If this fails, Vercel hasn't deployed today's morning workflow output.
6. Click any "Get Premium" button on `/` → goes directly to `https://whop.com/checkout/plan_VIlKaPMvfDPoj` (not the Whop homepage)
7. GitHub Actions tab → both `Daily Picks` and `Social — Daily IG Run` show green most recent runs; no open issues with label `social-failure`

If any of these break, start with `git log -10` to see what shipped recently, then `git show <hash>` on anything suspicious.

---

## 13. The next 24 hours

If you're picking up cold:
1. Read this file end-to-end (you're doing that).
2. Read `social/README.md` if anything Instagram-related is on your plate.
3. Check the GitHub Actions tab for any open `social-failure` issues.
4. Smoke-test the production URLs above.
5. If a daily-picks or social-daily run is currently in progress, watch it complete before pushing changes (push races are mostly handled now but mid-workflow pushes are still annoying).

Reach out to the operator if any of these aren't true and you can't see why from the recent commits.
