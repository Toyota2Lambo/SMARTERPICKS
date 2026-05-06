// ============================================================
// SMARTERPICKS — Picks Loader with Whop Membership Gating
// ============================================================
// Reads whop_token + whop_user_id set by callback.html, then asks
// Whop "does this user have access to PRODUCT_ID?". On 401 we try
// to refresh the token once before treating them as logged out.
// Also gates any element with [data-premium-only] for non-members
// and swaps the nav login link for a logout when authenticated.
// ============================================================

const WHOP_CONFIG = {
  CLIENT_ID:   "app_5RPKKi5gvHwpvo",
  PRODUCT_ID:  "prod_JAuXj9K2RJUjd",   // SmarterPicks (main) — switch to prod_4yaKtjgti7F9m for Premium-only gating
  ACCESS_BASE: "https://api.whop.com/api/v1/users",
  TOKEN_URL:   "https://api.whop.com/oauth/token",
};

// ── STATE ──────────────────────────────────────────────────
let isMember = false;

// ── ENTRY POINT ────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await checkMembership();
  await loadPicks();
  updateNavState();
  applyPremiumGating();
});

// ── TOKEN REFRESH ──────────────────────────────────────────
async function tryRefreshToken() {
  const refresh = localStorage.getItem("whop_refresh");
  if (!refresh) return null;
  try {
    const res = await fetch(WHOP_CONFIG.TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        grant_type:    "refresh_token",
        refresh_token: refresh,
        client_id:     WHOP_CONFIG.CLIENT_ID,
      }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    localStorage.setItem("whop_token", data.access_token);
    if (data.refresh_token) localStorage.setItem("whop_refresh", data.refresh_token);
    return data.access_token;
  } catch (_) { return null; }
}

function logout() {
  ["whop_token","whop_refresh","whop_user_id","whop_user","whop_access"]
    .forEach(k => localStorage.removeItem(k));
  window.location.reload();
}

// ── MEMBERSHIP CHECK ───────────────────────────────────────
async function fetchAccess(token, userId) {
  return fetch(
    `${WHOP_CONFIG.ACCESS_BASE}/${userId}/access/${WHOP_CONFIG.PRODUCT_ID}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
}

async function checkMembership() {
  let token   = localStorage.getItem("whop_token");
  const userId = localStorage.getItem("whop_user_id");
  if (!token || !userId) { isMember = false; return; }

  try {
    let res = await fetchAccess(token, userId);

    if (res.status === 401) {
      const fresh = await tryRefreshToken();
      if (fresh) {
        token = fresh;
        res = await fetchAccess(token, userId);
      }
    }

    if (res.status === 401) {
      // Refresh failed — clear stale auth, treat as logged out
      ["whop_token","whop_refresh","whop_user_id","whop_user","whop_access"]
        .forEach(k => localStorage.removeItem(k));
      isMember = false;
      return;
    }
    if (!res.ok) { isMember = false; return; }

    const data = await res.json();
    isMember = !!data.has_access;
    localStorage.setItem("whop_access", JSON.stringify(data));
  } catch (err) {
    console.warn("Membership check failed:", err.message);
    isMember = false;
  }
}

// ── UPDATE NAV BASED ON LOGIN STATE ───────────────────────
function updateNavState() {
  // Match either an id (#nav-login-btn) or the existing class (.nav-login)
  const loginBtn = document.getElementById("nav-login-btn") ||
                   document.querySelector(".nav-login");
  if (!loginBtn) return;

  const signedIn = !!localStorage.getItem("whop_token");

  if (signedIn) {
    loginBtn.textContent = isMember ? "Member · Logout" : "Logout";
    loginBtn.href = "#";
    loginBtn.onclick = (e) => { e.preventDefault(); logout(); };
  } else {
    loginBtn.textContent = "Login";
    loginBtn.href = "login.html";
    loginBtn.onclick = null;
  }
}

// ── GATE ARBITRARY ELEMENTS ────────────────────────────────
// Anything in the markup with `data-premium-only` is hidden for
// non-members. Anything with `data-non-member-only` is hidden for
// members (e.g. upgrade prompts).
function applyPremiumGating() {
  document.querySelectorAll("[data-premium-only]").forEach(el => {
    el.style.display = isMember ? "" : "none";
  });
  document.querySelectorAll("[data-non-member-only]").forEach(el => {
    el.style.display = isMember ? "none" : "";
  });
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

// Map league string -> sport icon emoji. Falls back to a neutral dot.
function sportIcon(league) {
  const s = String(league || "").toLowerCase();
  if (s.includes("nba") || s.includes("basketball")) return "🏀";
  if (s.includes("mlb") || s.includes("baseball"))   return "⚾";
  if (s.includes("nhl") || s.includes("hockey"))     return "🏒";
  if (s.includes("nfl") || s.includes("football"))   return "🏈";
  if (s.includes("ufc") || s.includes("mma"))        return "🥊";
  if (s.includes("soccer") || s.includes("epl"))     return "⚽";
  if (s.includes("tennis"))                          return "🎾";
  if (s.includes("golf") || s.includes("pga"))       return "⛳";
  return "•";
}

// Pull confidence grade out of the tags array. Tags look like
// "Confidence A", "Confidence B+", etc. Returns "a" / "b" / "c" / "".
function confidenceClass(tags) {
  const found = (tags || []).find(t => /confidence/i.test(t));
  if (!found) return "";
  const m = found.match(/confidence\s*([abc])/i);
  return m ? `conf-${m[1].toLowerCase()}` : "";
}

// ── RENDER PICK CARD ───────────────────────────────────────
function renderPickCard(pick) {
  const tags = (pick.tags || []).map(t => `<span class="tag">${escHtml(t)}</span>`).join("");
  const icon = sportIcon(pick.league);
  const confCls = confidenceClass(pick.tags);

  // Members see ALL picks fully
  if (isMember || !pick.is_premium) {
    const classes = ["pick-card"];
    if (isMember && pick.is_premium) classes.push("member-pick");
    if (confCls) classes.push(confCls);
    return `
      <div class="${classes.join(" ")}">
        ${isMember && pick.is_premium ? '<span class="lock-badge" style="background:var(--win);color:#000;">Members Only</span>' : ""}
        <div class="pick-header">
          <span class="pick-league"><span class="pick-icon">${icon}</span>${escHtml(pick.league)}</span>
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
          <span class="pick-league"><span class="pick-icon">${icon}</span>${escHtml(pick.league)}</span>
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
  // Used when picks.json can't be fetched — match the skeletons in index.html
  // so the page degrades gracefully instead of flashing an error message.
  const skeleton = `
    <div class="pick-card">
      <div class="skeleton-line" style="width:30%;"></div>
      <div class="skeleton-line" style="width:65%;height:24px;margin-top:18px;"></div>
      <div class="skeleton-line" style="width:90%;"></div>
      <div class="skeleton-line" style="width:100%;height:60px;margin-top:18px;"></div>
      <div class="skeleton-line" style="width:80%;height:12px;margin-top:18px;"></div>
      <div class="skeleton-line" style="width:60%;height:12px;"></div>
    </div>`;
  return skeleton.repeat(3);
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

