// ============================================================
// SMARTERPICKS — Picks Loader with Whop Membership Gating
// ============================================================
// Reads whop_token + whop_user_id set by login.html, then asks
// Whop "does this user have access to ACCESS_PASS_ID?". If yes,
// premium picks unlock. If no token or no access, only the free
// pick is shown — every other card is rendered as a locked teaser.
//
// THE ONE THING TO KEEP IN SYNC WITH login.html:
//   ACCESS_PASS_ID — must match what login.html checks
// ============================================================

const WHOP_CONFIG = {
  ACCESS_PASS_ID: "biz_wDYY7HRvnDbvw2",
  ACCESS_BASE:    "https://api.whop.com/api/v1/users",
};

// ── STATE ──────────────────────────────────────────────────
let isMember = false;
let currentUser = null;

// ── ENTRY POINT ────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await checkMembership();
  await loadPicks();
  updateNavState();
});

// ── MEMBERSHIP CHECK ───────────────────────────────────────
async function checkMembership() {
  const token  = localStorage.getItem("whop_token");
  const userId = localStorage.getItem("whop_user_id");
  if (!token || !userId) { isMember = false; return; }

  try {
    const res = await fetch(
      `${WHOP_CONFIG.ACCESS_BASE}/${userId}/access/${WHOP_CONFIG.ACCESS_PASS_ID}`,
      { headers: { Authorization: `Bearer ${token}` } }
    );

    if (res.status === 401) {
      // Token expired or revoked — clear so they re-auth next visit
      localStorage.removeItem("whop_token");
      localStorage.removeItem("whop_user_id");
      localStorage.removeItem("whop_refresh");
      isMember = false;
      return;
    }
    if (!res.ok) { isMember = false; return; }

    const data = await res.json();
    isMember = !!data.has_access;
    currentUser = { id: userId };
  } catch (err) {
    console.warn("Membership check failed:", err.message);
    isMember = false;
  }
}

// ── UPDATE NAV BASED ON LOGIN STATE ───────────────────────
function updateNavState() {
  const loginBtn = document.getElementById("nav-login-btn");
  if (!loginBtn) return;

  if (isMember) {
    loginBtn.textContent = "Member";
    loginBtn.href = "https://whop.com/smarterpicks/hub";
  } else {
    loginBtn.textContent = "Login";
    loginBtn.href = "login.html";
  }
}

// ── LOAD AND RENDER PICKS ──────────────────────────────────
async function loadPicks() {
  try {
    const response = await fetch(`picks.json?v=${Date.now()}`);
    if (!response.ok) throw new Error(`Could not load picks.json`);
    const data = await response.json();

    // Update date display
    const dateEl = document.getElementById("slate-date");
    if (dateEl) dateEl.textContent = data.date || getTodayString();

    // Update summary line
    const summaryEl = document.getElementById("slate-summary");
    if (summaryEl && data.sport_summary) summaryEl.textContent = data.sport_summary;

    // Count picks
    const freePicks    = data.picks.filter(p => !p.is_premium).length;
    const premiumPicks = data.picks.filter(p =>  p.is_premium).length;

    const countEl = document.getElementById("pick-count");
    if (countEl) {
      if (isMember) {
        countEl.textContent = `Full card unlocked · ${freePicks + premiumPicks} picks today`;
      } else {
        countEl.textContent = `${freePicks} free · ${premiumPicks} premium · subscribe to unlock`;
      }
    }

    // Render pick cards
    const grid = document.getElementById("picks-grid");
    if (grid && data.picks && data.picks.length > 0) {
      grid.innerHTML = data.picks.map(pick => renderPickCard(pick)).join("");
    }

    // Show member banner if logged in
    if (isMember) showMemberBanner();

  } catch (err) {
    console.warn("Picks loader:", err.message);
    const grid = document.getElementById("picks-grid");
    if (grid) grid.innerHTML = renderLoadingCard();
  }
}

// ── RENDER PICK CARD ───────────────────────────────────────
function renderPickCard(pick) {
  const tags = (pick.tags || []).map(t => `<span class="tag">${escHtml(t)}</span>`).join("");

  // Members see ALL picks fully
  if (isMember || !pick.is_premium) {
    return `
      <div class="pick-card${isMember && pick.is_premium ? " member-pick" : ""}">
        ${isMember && pick.is_premium ? '<span class="lock-badge" style="background:var(--win);color:#000;">Members Only</span>' : ""}
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
        <div class="pick-tags">${tags}</div>
      </div>
    `;
  }

  // Non-members see locked cards for premium picks
  return `
    <div class="pick-card locked">
      <span class="lock-badge">Premium</span>
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
          <div class="pick-call-odds">${escHtml(pick.odds)} · ${escHtml(pick.book)}</div>
        </div>
        <p class="pick-reasoning">${escHtml(pick.reasoning)}</p>
      </div>
      <div class="lock-overlay">
        <div class="lock-icon">🔒</div>
        <div class="lock-title">Members Only</div>
        <div class="lock-sub">Subscribe to unlock all ${getPremiumCount()} premium picks today</div>
        <a href="login.html" class="lock-btn">Unlock for $29/mo →</a>
      </div>
    </div>
  `;
}

// ── MEMBER BANNER ──────────────────────────────────────────
function showMemberBanner() {
  const slate = document.querySelector(".slate .container");
  if (!slate) return;

  const banner = document.createElement("div");
  banner.style.cssText = `
    background: rgba(109,190,122,0.08);
    border: 1px solid rgba(109,190,122,0.25);
    padding: 14px 20px;
    margin-bottom: 28px;
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: .06em;
    text-transform: uppercase;
    color: var(--win);
    display: flex;
    align-items: center;
    gap: 10px;
  `;
  banner.innerHTML = `
    <span style="width:8px;height:8px;background:var(--win);border-radius:50%;flex-shrink:0;display:inline-block;"></span>
    Premium member — full card unlocked · 
    <a href="https://whop.com/smarterpicks/hub" style="color:var(--win);text-decoration:underline;margin-left:4px;">
      View in Discord Hub →
    </a>
  `;

  const header = slate.querySelector(".section-header");
  if (header) header.after(banner);
}

// ── HELPERS ────────────────────────────────────────────────
let _cachedPicks = null;

function getPremiumCount() {
  return 6; // fallback count shown in lock overlay
}

function renderLoadingCard() {
  return `
    <div class="pick-card" style="grid-column:1/-1;text-align:center;padding:60px 32px;">
      <div style="font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin-bottom:16px;">Loading</div>
      <div style="font-family:var(--display);font-size:28px;font-weight:400;margin-bottom:12px;">Fetching today's picks...</div>
      <div style="color:var(--text-muted);font-size:14px;">The daily card generates at 9am ET. Check back shortly.</div>
    </div>
  `;
}

function escHtml(str) {
  if (typeof str !== "string") return String(str || "");
  return str
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#039;");
}

function getTodayString() {
  return new Date().toLocaleDateString("en-US", {
    weekday: "long", year: "numeric", month: "long", day: "numeric"
  });
}

