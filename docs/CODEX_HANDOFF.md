# Codex handoff prompt

Paste everything below the `---` into Codex (or any other coding agent
with repo access). It tells the agent how to audit the codebase for
half-wired external integrations, finish what it can, and produce a
manual checklist for what the operator still has to do out-of-band.

Iterate on this file in place — keeping it versioned means future runs
of Codex (or Claude, or anyone else) start from the same baseline.

---

You're working in the SMARTERPICKS repository — a static HTML site (vanilla HTML/CSS/JS, no build step) deployed on Vercel, with a Python daily-picks generator running via GitHub Actions, two Vercel serverless functions in `api/`, and an Instagram social-publishing pipeline under `social/`. Read `social/README.md` and the workflow files in `.github/workflows/` first to understand the data flow.

## Your job

Audit the codebase for any external integration that's referenced but not fully wired, finish the wiring you can finish from inside the code, and produce a manual-setup checklist for everything the operator still needs to do out-of-band (Meta app setup, env vars, ESP signup, etc.).

## Known integrations and their current status

| Service | What it's for | Status |
|---|---|---|
| Anthropic API | Picks generation, in-card chat, social content | Wired (env: `ANTHROPIC_API_KEY`) |
| The Odds API | Game odds + final scores | Wired (env: `ODDS_API_KEY`) |
| ESPN (unofficial JSON) | MLB starters + NBA/NHL injuries | Wired, best-effort, no key |
| Whop OAuth | Membership auth | Wired (CLIENT_ID + PRODUCT_ID in `whop-config.js`) |
| Instagram Graph API | Posting daily content | Wired but needs Meta app setup (env: `IG_ACCESS_TOKEN`, `IG_BUSINESS_ACCOUNT_ID`) |
| Discord webhook | Run summaries | Wired, optional (env: `DISCORD_WEBHOOK_URL`) |
| Newsletter ESP | Signups from index.html form | **HALF-WIRED** — `api/newsletter.js` forwards to `NEWSLETTER_WEBHOOK_URL` but operator hasn't pointed it at a real ESP yet |

## What to do

1. **Grep audit.** Look for, at minimum:
   - `TODO`, `FIXME`, `XXX`, `placeholder`, `change this`, `swap`, `your-` strings
   - `href="#"` (dead links — confirm if intentional or unwired)
   - Hardcoded support email (`support@smarterpicks.io` — verify if a mailbox exists or if it should be a different address)
   - Whop billing portal URL in `billing.html` (currently `whop.com/orders/` — is that the correct customer portal for this account?)
   - Promo code `FREE30` — referenced everywhere in marketing copy and social templates but may not exist in Whop yet
   - `process.env.X` and `os.environ.get("X")` — every env var should be documented
   - Any `fetch()` / `requests.get()` to a URL that's hardcoded and looks like a placeholder

2. **Wire what you can.** For each finding:
   - If it's a self-contained code fix (e.g., a button with no handler, a missing fetch, a malformed env var lookup) — make the change.
   - If it requires an account/key/external setup — don't fabricate. Add a clearly-marked `TODO` comment in the relevant file and capture the action in the manual checklist.

3. **Produce a `.env.example`** at the repo root listing every env var used anywhere (Vercel functions, GitHub Actions workflows, Python scripts) with a one-line description and which file references it.

4. **Update or create `docs/INTEGRATIONS.md`** — one section per external service: what it does, where it's used in the code, what env vars it needs, what the operator must set up out-of-band.

## Hard constraints — DO NOT do these

- **Do NOT rename `callback.html` or change `CONFIG.REDIRECT_URI` in `callback.html` / `login.html`** — that path is registered as the OAuth redirect URI in the Whop app dashboard. Renaming breaks sign-in.
- **Do NOT modify `whop-config.js` CLIENT_ID or PRODUCT_ID** — operator-controlled, don't touch.
- **Do NOT delete or regenerate `picks.json`, `results.json`, `history.json`, `archive.json`** — those are the live data files the daily workflow updates.
- **Do NOT add npm dependencies** unless absolutely necessary. There's no `package.json` at root by design; `api/*` is plain CommonJS; only `social/` uses Puppeteer.
- **Do NOT add a build step, framework, or bundler.**
- **Do NOT fabricate content** — no fake testimonials, fake member quotes, fake stats. If a section needs real content, leave a clearly-marked placeholder.
- **Do NOT add analytics or error monitoring without explicit operator consent** — flag them in the checklist as recommended additions instead.
- **Do NOT refactor wholesale.** Match existing code style.
- **Keep the visual identity:** gold accent `#d4a843`, dark bg `#0a0a0a`, fonts Fraunces / Inter Tight / JetBrains Mono.

## If you're unsure

Don't guess. Add the question to the "Open questions" section of your output and let the operator decide.

## Output format

Reply in exactly this structure:

### Changes made
Numbered list. Each item: file path + line ref + one-sentence description of what changed and why.

### Manual setup checklist
Numbered, grouped by service. For each action item, give:
- **What:** plain-English description of what the operator needs to do
- **Where:** the URL or dashboard or file they need to touch
- **Env var(s):** names and (if relevant) paste-into-Vercel commands
- **Verifies:** how to confirm it worked (e.g., "trigger workflow `social-daily.yml` manually and watch the publish step report `media_id`")

### Open questions
Anything you couldn't decide without the operator weighing in.

### Recommended (not done)
Anything you would have done but didn't because of the constraints above (e.g., "would add Sentry but operator hasn't consented to analytics tooling").
