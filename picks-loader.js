// ============================================================
// SHARPLINE — Live Picks Loader
// ============================================================
// What this file does:
//   Every time someone opens your site, this code runs and
//   fetches the latest picks.json file, then builds the pick
//   cards on the page automatically.
//
//   You never touch this file unless you want to change
//   how the cards look. The picks themselves come from
//   picks.json which your Python script updates daily.
//
// HOW TO ADD THIS TO YOUR HTML:
//   Paste this line just before </body> in your HTML file:
//   <script src="picks-loader.js"></script>
//
//   Also add these IDs to your HTML (already done in the
//   updated landing page — see README):
//   - id="slate-date" on the section tag line
//   - id="slate-summary" on the subtitle
//   - id="picks-grid" on the picks container div
// ============================================================


// ── MAIN FUNCTION: load and render picks ──────────────────
async function loadPicks() {
  try {
    // Fetch the picks.json file from your own site
    // The ?v= part forces the browser to always get the fresh version
    const response = await fetch(`picks.json?v=${Date.now()}`);

    if (!response.ok) {
      throw new Error(`Could not load picks.json (status ${response.status})`);
    }

    const data = await response.json();

    // Update the date shown on the page
    const dateEl = document.getElementById("slate-date");
    if (dateEl) dateEl.textContent = data.date || getTodayString();

    // Update the slate summary line under the heading
    const summaryEl = document.getElementById("slate-summary");
    if (summaryEl && data.sport_summary) {
      summaryEl.textContent = data.sport_summary;
    }

    // Update the "last updated" line if it exists
    const updatedEl = document.getElementById("last-updated");
    if (updatedEl && data.generated_at_readable) {
      updatedEl.textContent = `Updated: ${data.generated_at_readable}`;
    }

    // Count free vs premium picks for the header
    const freePicks    = data.picks.filter(p => !p.is_premium).length;
    const premiumPicks = data.picks.filter(p =>  p.is_premium).length;

    const countEl = document.getElementById("pick-count");
    if (countEl) {
      countEl.textContent = `${freePicks} free · ${premiumPicks} premium · updates until tip-off`;
    }

    // Build and inject all the pick cards
    const grid = document.getElementById("picks-grid");
    if (grid && data.picks && data.picks.length > 0) {
      grid.innerHTML = data.picks.map(pick => renderPickCard(pick)).join("");
    }

  } catch (err) {
    console.warn("Picks loader:", err.message);
    // If picks.json fails to load, show a friendly message
    // instead of breaking the page
    const grid = document.getElementById("picks-grid");
    if (grid) {
      grid.innerHTML = renderLoadingCard();
    }
  }
}


// ── RENDER: a single pick card ────────────────────────────
function renderPickCard(pick) {
  // LOCKED / PREMIUM pick card
  if (pick.is_premium) {
    return `
      <div class="pick-card locked">
        <span class="lock-badge">Premium</span>

        <!-- Blurred preview — visible but unreadable -->
        <div class="pick-blur">
          <div class="pick-header">
            <span class="pick-league">${escHtml(pick.league)}</span>
            <span class="pick-time">${escHtml(pick.time)}</span>
          </div>
          <div class="pick-matchup">
            ${escHtml(pick.away_team)} <span class="vs">at</span> ${escHtml(pick.home_team)}
          </div>
          <div class="pick-detail">${escHtml(pick.game_detail)}</div>
          <div class="pick-call">
            <div class="pick-call-label">The Play</div>
            <div class="pick-call-value">${escHtml(pick.pick)}</div>
            <div class="pick-call-odds">
              ${escHtml(pick.odds)} · ${escHtml(pick.book)} · Stake ${escHtml(pick.stake)}
            </div>
          </div>
          <p class="pick-reasoning">${escHtml(pick.reasoning)}</p>
        </div>

        <!-- Lock overlay shown on top -->
        <div class="lock-overlay">
          <div class="lock-icon">🔒</div>
          <div class="lock-title">Premium Pick</div>
          <div class="lock-sub">
            Unlock the full card — ${getLockedCount()} more plays with full reasoning
          </div>
          <a href="#pricing" class="lock-btn">Unlock for $29/mo →</a>
        </div>
      </div>
    `;
  }

  // FREE pick card — fully visible
  const tagsHtml = (pick.tags || [])
    .map(tag => `<span class="tag">${escHtml(tag)}</span>`)
    .join("");

  return `
    <div class="pick-card">
      <div class="pick-header">
        <span class="pick-league">${escHtml(pick.league)}</span>
        <span class="pick-time">${escHtml(pick.time)}</span>
      </div>
      <div class="pick-matchup">
        ${escHtml(pick.away_team)} <span class="vs">at</span> ${escHtml(pick.home_team)}
      </div>
      <div class="pick-detail">${escHtml(pick.game_detail)}</div>

      <div class="pick-call">
        <div class="pick-call-label">The Play</div>
        <div class="pick-call-value">${escHtml(pick.pick)}</div>
        <div class="pick-call-odds">
          ${escHtml(pick.odds)} · ${escHtml(pick.book)} · Stake ${escHtml(pick.stake)}
        </div>
      </div>

      <p class="pick-reasoning">${escHtml(pick.reasoning)}</p>

      <div class="pick-tags">${tagsHtml}</div>
    </div>
  `;
}


// ── RENDER: shown when picks haven't loaded yet ───────────
function renderLoadingCard() {
  return `
    <div class="pick-card" style="grid-column:1/-1; text-align:center; padding:60px 32px;">
      <div style="font-family:var(--mono); font-size:11px; letter-spacing:.12em;
                  text-transform:uppercase; color:var(--accent); margin-bottom:16px;">
        Updating
      </div>
      <div style="font-family:var(--display); font-size:28px; font-weight:400;
                  margin-bottom:12px;">
        Today's picks are loading
      </div>
      <div style="color:var(--text-muted); font-size:14px; margin-bottom:28px;">
        The daily card generates at 9am ET. Check back shortly or refresh the page.
      </div>
      <a href="#pricing" class="lock-btn" style="display:inline-block;">
        Subscribe for instant access →
      </a>
    </div>
  `;
}


// ── HELPERS ───────────────────────────────────────────────

// Count how many picks are locked (for the overlay message)
let _lockedCount = 6;
function getLockedCount() { return _lockedCount; }

// Security: prevent any weird characters in pick data
// from breaking the HTML or running rogue code
function escHtml(str) {
  if (typeof str !== "string") return String(str || "");
  return str
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#039;");
}

// Fallback date if picks.json has no date
function getTodayString() {
  return new Date().toLocaleDateString("en-US", {
    weekday: "long", year: "numeric", month: "long", day: "numeric"
  });
}


// ── RUN ON PAGE LOAD ──────────────────────────────────────
// Wait for the page to fully load, then fetch picks
document.addEventListener("DOMContentLoaded", loadPicks);
