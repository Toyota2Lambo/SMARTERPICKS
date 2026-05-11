// ============================================================
// SMARTERPICKS — Picks Loader with Whop Membership Gating
// ============================================================
// Reads whop_token + whop_user_id set by callback.html, then asks
// Whop "does this user have access to PRODUCT_ID?". On 401 we try
// to refresh the token once before treating them as logged out.
// Also gates any element with [data-premium-only] for non-members
// and swaps the nav login link for a logout when authenticated.
// ============================================================

// Read the shared Whop config that whop-config.js sets on window. Fall
// back to a hardcoded copy ONLY so a forgotten <script> tag doesn't break
// auth — but log loudly so we notice the drift in console.
const WHOP_CONFIG = window.WHOP_CONFIG || (function () {
  console.warn("[picks-loader] whop-config.js not loaded — using fallback constants. Add <script src='whop-config.js'></script> before picks-loader.js.");
  return {
    CLIENT_ID:   "app_5RPKKi5gvHwpvo",
    PRODUCT_ID:  "prod_JAuXj9K2RJUjd",
    ACCESS_BASE: "https://api.whop.com/api/v1/users",
    TOKEN_URL:   "https://api.whop.com/oauth/token",
  };
})();

// ── STATE ──────────────────────────────────────────────────
let isMember = false;

// ── ENTRY POINT ────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  wireCheckoutLinks();
  await checkMembership();
  await loadPicks();
  updateNavState();
  applyPremiumGating();
});

// Reads window.WHOP_CONFIG and writes per-plan checkout URLs onto every
// element tagged [data-checkout="monthly|annual|free"]. The HTML has
// the current URLs hardcoded as a no-JS fallback, but this overrides
// them at runtime so a plan-ID change in whop-config.js cascades to
// every checkout button on the page without grepping. Safe no-op if
// whop-config.js isn't loaded or the element wasn't tagged.
function wireCheckoutLinks() {
  const cfg = window.WHOP_CONFIG || {};
  const map = {
    monthly: cfg.CHECKOUT_URL_MONTHLY,
    annual:  cfg.CHECKOUT_URL_ANNUAL,
    free:    "login", // free tier: send through OAuth, no Whop checkout
  };
  document.querySelectorAll("[data-checkout]").forEach(el => {
    const target = map[el.dataset.checkout];
    if (target) el.href = target;
  });
}

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
  // Prefer the shared logout from auth-chrome.js (it's the single
  // source of truth for "what keys must we clear"). Fall back to an
  // inline cleanup if auth-chrome happens not to be loaded on this
  // page so we never silently no-op a logout click.
  if (typeof window.smarterpicksLogout === "function") {
    window.smarterpicksLogout();
    return;
  }
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
  if (loginBtn) {
    const signedIn = !!localStorage.getItem("whop_token");
    if (signedIn) {
      loginBtn.textContent = isMember ? "Member · Logout" : "Logout";
      loginBtn.href = "#";
      loginBtn.onclick = (e) => { e.preventDefault(); logout(); };
    } else {
      loginBtn.textContent = "Login";
      loginBtn.href = "login";
      loginBtn.onclick = null;
    }
  }

  // Members get a premium "Members Area" pill in the nav (and the mobile
  // drawer); non-members see the gold "Go Premium" CTA instead.
  const goPremium  = document.getElementById("nav-go-premium");
  const membersBtn = document.getElementById("nav-members-btn");
  if (goPremium)  goPremium.style.display  = isMember ? "none" : "inline-block";
  if (membersBtn) membersBtn.style.display = isMember ? "inline-flex" : "none";

  const drawerGoPremium = document.getElementById("drawer-go-premium");
  const drawerMembers   = document.getElementById("drawer-members-btn");
  if (drawerGoPremium) drawerGoPremium.style.display = isMember ? "none" : "block";
  if (drawerMembers)   drawerMembers.style.display   = isMember ? "block" : "none";
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

    // Count picks. _premiumCount is read by getPremiumCount() so the
    // lock-overlay copy ("Subscribe to unlock all N premium picks today")
    // matches the actual slate instead of the hardcoded fallback.
    const freePicks    = data.picks.filter(p => !p.is_premium).length;
    const premiumPicks = data.picks.filter(p =>  p.is_premium).length;
    _premiumCount = premiumPicks;

    const countEl = document.getElementById("pick-count");
    if (countEl) {
      const total = freePicks + premiumPicks;
      if (isMember) {
        countEl.textContent = `Full card unlocked · ${total} picks today`;
      } else {
        // Matches the 3-card limit + "more" tile rendered below.
        countEl.textContent = `Free preview · ${freePicks} free pick + ${total - freePicks} premium behind the paywall`;
      }
    }

    // Render pick cards.
    //
    // Members see the full slate. Non-members on the public landing
    // (index.html) see exactly THREE cards on purpose: the free pick
    // plus two locked premium teasers, then a tasteful "+N more"
    // unlock tile. Spamming a non-member with 6 blurred cards reads
    // as desperate; showing just enough to prove there's more behind
    // the paywall converts better.
    const grid = document.getElementById("picks-grid");
    if (grid && data.picks && data.picks.length > 0) {
      let visiblePicks = data.picks;
      let hiddenCount  = 0;
      if (!isMember) {
        const FREE_PAGE_LIMIT = 3;
        visiblePicks = data.picks.slice(0, FREE_PAGE_LIMIT);
        hiddenCount  = Math.max(0, data.picks.length - FREE_PAGE_LIMIT);
      }
      grid.innerHTML = visiblePicks.map(pick => renderPickCard(pick)).join("");
      if (!isMember && hiddenCount > 0) {
        grid.insertAdjacentHTML("beforeend", renderMoreTile(hiddenCount));
      }
      wireCardInteractivity(grid);
    }

    // Show member banner if logged in
    if (isMember) showMemberBanner();

  } catch (err) {
    console.warn("Picks loader:", err.message);
    renderPicksError(err.message);
  }
}

// Render an honest error state instead of leaving the skeleton
// shimmering forever when picks.json can't load. The most common
// cause is the daily generator workflow not having run yet (or
// having failed) — calling that out beats silent failure.
function renderPicksError(detail) {
  const grid = document.getElementById("picks-grid");
  if (grid) {
    grid.innerHTML = `
      <div class="pick-card" style="grid-column:1/-1;text-align:center;padding:48px 32px;">
        <div style="font-size:32px;margin-bottom:16px;">⚠️</div>
        <div style="font-family:var(--display);font-size:24px;font-weight:400;letter-spacing:-.01em;margin-bottom:10px;">Today's slate isn't loading.</div>
        <p style="color:var(--text-muted);font-size:14px;line-height:1.6;max-width:48ch;margin:0 auto 22px;">
          We couldn't fetch picks.json. The morning generator may still be running, or the file may be temporarily unreachable. Try refreshing in a minute.
        </p>
        <button onclick="location.reload()" style="font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:13px 24px;background:var(--accent);color:#000;border:none;cursor:pointer;">Refresh →</button>
      </div>
    `;
  }
  const dateEl = document.getElementById("slate-date");
  if (dateEl) dateEl.textContent = "Picks unavailable";
  const countEl = document.getElementById("pick-count");
  if (countEl) countEl.textContent = "Refresh in a moment";
  // Surface the underlying message in console so a debugger can see it,
  // but don't show the raw error to the user — it'd be noise.
  if (detail) console.warn("picks.json detail:", detail);
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

// Pull confidence info from tags ("Confidence A", "Confidence B+", "Confidence C+").
// Returns { letter, label, percent, cls } where percent is 0-100 for the meter fill.
function confidenceInfo(tags) {
  const found = (tags || []).find(t => /confidence/i.test(t));
  if (!found) return null;
  const m = found.match(/confidence\s*([abc])\s*([+-])?/i);
  if (!m) return null;
  const letter = m[1].toUpperCase();
  const mod = m[2] || "";
  const baseline = { A: 92, B: 75, C: 50 }[letter] ?? 50;
  const adj = mod === "+" ? 8 : mod === "-" ? -8 : 0;
  const percent = Math.max(20, Math.min(100, baseline + adj));
  return {
    letter,
    label: `${letter}${mod}`,
    percent,
    cls: `conf-${letter.toLowerCase()}`,
  };
}

// Map sportsbook name -> homepage URL for the "Open in [book]" deep-link button.
// (Most US books don't expose pre-filled-bet deep links publicly, so we link to
// the book's homepage. The user can then paste/search the pick. Removes the
// "where do I even bet this" friction.)
function bookHref(book) {
  const map = {
    "DraftKings": "https://sportsbook.draftkings.com",
    "FanDuel":    "https://sportsbook.fanduel.com",
    "Caesars":    "https://sportsbook.caesars.com",
    "BetMGM":     "https://sports.betmgm.com",
    "PointsBet":  "https://www.pointsbet.com",
    "ESPN BET":   "https://espnbet.com",
    "ESPNBet":    "https://espnbet.com",
    "Hard Rock":  "https://www.hardrock.bet",
    "bet365":     "https://www.bet365.com",
  };
  return map[book] || null;
}

// Result chip HTML — shown on cards that have a `result` field set
// ("WON" / "LOST" / "PUSH"). Animates in via CSS @keyframes resultFlip.
function resultChip(pick) {
  if (!pick.result) return "";
  const r = String(pick.result).toUpperCase();
  const cls = r === "WON" ? "won" : r === "LOST" ? "lost" : "push";
  const units = (pick.units != null) ? `${pick.units > 0 ? "+" : ""}${pick.units}u` : "";
  return `<div class="result-reveal"><div class="result-chip ${cls}">${r}${units ? " · " + units : ""}</div></div>`;
}

// Confidence meter HTML. Sets --conf as a unitless decimal (0..1)
// because the CSS uses transform:scaleX(var(--conf)) — GPU-composited,
// no layout thrash.
function confidenceMeter(info) {
  if (!info) return "";
  const decimal = (Math.max(0, Math.min(100, info.percent)) / 100).toFixed(2);
  return `
    <div class="conf-meter">
      <span>Confidence</span>
      <div class="conf-meter-track">
        <div class="conf-meter-fill" style="--conf:${decimal};"></div>
      </div>
      <span class="conf-label">${info.label}</span>
    </div>
  `;
}

// Tools row at the bottom of each pick card. Always shows "Ask Claude";
// shows "Open in [book]" only when we know that book's URL.
function toolsRow(pick) {
  const url = bookHref(pick.book);
  const open = url
    ? `<a class="pick-tool primary" href="${url}" target="_blank" rel="noopener">↗ Open in ${escHtml(pick.book)}</a>`
    : "";
  const ask = `<button class="pick-tool ask-btn" type="button">💬 Ask Claude</button>`;
  return `<div class="pick-tools">${open}${ask}</div>`;
}

// Embed the pick JSON in a data-attribute so the chat panel can read it
// without us having to maintain a separate JS map.
function pickPayload(pick) {
  const safe = {
    pick: pick.pick, league: pick.league,
    away_team: pick.away_team, home_team: pick.home_team,
    game_detail: pick.game_detail, time: pick.time,
    odds: pick.odds, book: pick.book, stake: pick.stake,
    reasoning: pick.reasoning, tags: pick.tags || [],
  };
  return escHtml(JSON.stringify(safe));
}

function chatPanel() {
  return `
    <div class="chat-panel">
      <div class="chat-msgs"></div>
      <div class="chat-suggest">
        <button type="button" class="chat-chip">Why this side?</button>
        <button type="button" class="chat-chip">Worst case scenario?</button>
        <button type="button" class="chat-chip">Should I parlay this?</button>
      </div>
      <form class="chat-form">
        <input class="chat-input" type="text" placeholder="Ask anything about this pick…" maxlength="240">
        <button class="chat-send" type="submit">Send</button>
      </form>
      <div class="chat-byline">Powered by Claude · responses are analysis, not financial advice</div>
    </div>
  `;
}

// ── RENDER PICK CARD ───────────────────────────────────────
function renderPickCard(pick) {
  // Strip the "Confidence X" tag from the visible tag chips since
  // it now renders as a dedicated meter.
  const visibleTags = (pick.tags || []).filter(t => !/confidence/i.test(t));
  const tags = visibleTags.map(t => `<span class="tag">${escHtml(t)}</span>`).join("");

  const icon    = sportIcon(pick.league);
  const conf    = confidenceInfo(pick.tags);
  const confCls = conf ? conf.cls : "";

  // Members see ALL picks fully
  if (isMember || !pick.is_premium) {
    const classes = ["pick-card"];
    if (isMember && pick.is_premium) classes.push("member-pick");
    if (confCls) classes.push(confCls);
    if (pick.result) classes.push("has-result");
    return `
      <div class="${classes.join(" ")}" data-pick='${pickPayload(pick)}'>
        ${resultChip(pick)}
        ${isMember && pick.is_premium ? '<span class="lock-badge" style="background:var(--win);color:#000;">Members Only</span>' : ""}
        <div class="pick-header">
          <span class="pick-league"><span class="pick-icon">${icon}</span>${escHtml(pick.league)}</span>
          <span class="pick-time">${escHtml(pick.time)}</span>
        </div>
        <div class="pick-matchup">
          ${escHtml(pick.away_team)} <span class="vs">at</span> ${escHtml(pick.home_team)}
        </div>
        <div class="pick-detail">${escHtml(pick.game_detail)}</div>
        ${confidenceMeter(conf)}
        <div class="pick-call">
          <div class="pick-call-label">The Play</div>
          <div class="pick-call-value">${escHtml(pick.pick)}</div>
          <div class="pick-call-odds">
            ${escHtml(pick.odds)} · ${escHtml(pick.book)} · Stake ${escHtml(pick.stake)}
          </div>
        </div>
        <p class="pick-reasoning">${escHtml(pick.reasoning)}</p>
        <div class="pick-tags">${tags}</div>
        ${toolsRow(pick)}
        ${chatPanel()}
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
        <a href="login" class="lock-btn">Unlock for $29/mo →</a>
      </div>
    </div>
  `;
}

// ── "MORE PICKS BEHIND THE PAYWALL" TILE ───────────────────
// Slots into the .picks-grid like a normal card so the layout stays
// even (3 cards in the row instead of 2 + an awkward strip below).
// Tone is suggestive, not nag-y: "there are N more, here's how to
// see them" — no flashing arrows, no countdown timers.
function renderMoreTile(hiddenCount) {
  const noun = hiddenCount === 1 ? "pick" : "picks";
  return `
    <div class="pick-card more-tile" style="background:linear-gradient(180deg, var(--bg-elevated), var(--bg)); border-left:3px solid var(--accent); padding:36px 32px; display:flex; flex-direction:column; justify-content:center; text-align:center; gap:18px;">
      <div style="font-family:var(--mono);font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent);">
        + ${hiddenCount} more ${noun} on today's full card
      </div>
      <div style="font-family:var(--display);font-size:30px;font-weight:400;line-height:1.1;letter-spacing:-.01em;color:var(--text);">
        The plays we like most<br>
        <em style="font-style:italic;color:var(--accent);font-weight:300;">are behind the paywall.</em>
      </div>
      <p style="font-size:13px;color:var(--text-muted);line-height:1.55;max-width:32ch;margin:0 auto;">
        Free tier sees today's free pick. Members see the full card — line moves, props, the best bet of the day, and the reasoning on each one.
      </p>
      <a href="login" style="display:inline-block;background:var(--accent);color:#000;padding:14px 22px;font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;text-decoration:none;margin-top:6px;transition:background .2s, transform .2s;">
        Unlock all ${hiddenCount + 1} picks · $29/mo →
      </a>
      <div style="font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim);">
        7-day money-back · cancel anytime
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
let _premiumCount = null;   // populated when picks.json loads

function getPremiumCount() {
  // Return the actual count once picks.json loads. While we're still
  // fetching, fall back to a conservative "the rest" so the overlay
  // never reads "0 premium picks" mid-render.
  return _premiumCount != null ? _premiumCount : 6;
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

// ============================================================
// CARD INTERACTIVITY — 3D tilt, confidence-meter reveal,
// AI Pick Explainer chat
// ============================================================
function wireCardInteractivity(grid) {
  const cards = grid.querySelectorAll(".pick-card");

  // -- 1) 3D tilt on cursor (skip locked + touch devices) --
  // Throttled to one transform write per animation frame via rAF. This
  // is the difference between buttery (60fps) and choppy on busy pages.
  // Inline transition is set once on enter / cleared on leave; mousemove
  // never touches it. translate3d hints the GPU to give us a layer.
  const isTouch = matchMedia("(hover: none)").matches;
  if (!isTouch) {
    cards.forEach(card => {
      if (card.classList.contains("locked")) return;

      let pendingX = 0, pendingY = 0;
      let rafId = null;
      let active = false;

      const apply = () => {
        rafId = null;
        if (!active) return;
        const rotY =  pendingX * 8;
        const rotX = -pendingY * 6;
        card.style.transform =
          `translate3d(0,-4px,0) perspective(1200px) ` +
          `rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg)`;
      };

      card.addEventListener("mouseenter", () => {
        active = true;
        // Snappy transition while the cursor is over the card so tilt
        // tracks naturally; CSS-defined transition resumes on leave.
        card.style.transition = "transform 80ms ease-out";
        card.style.willChange = "transform";
      });

      card.addEventListener("mousemove", (e) => {
        const r = card.getBoundingClientRect();
        pendingX = (e.clientX - r.left) / r.width  - 0.5;
        pendingY = (e.clientY - r.top)  / r.height - 0.5;
        if (rafId == null) rafId = requestAnimationFrame(apply);
      });

      card.addEventListener("mouseleave", () => {
        active = false;
        if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
        // Hand control back to the CSS transition for a smooth return.
        card.style.transition = "";
        card.style.transform = "";
        card.style.willChange = "";
      });
    });
  }

  // -- 2) Animate confidence meter fill when card scrolls into view --
  if ("IntersectionObserver" in window) {
    const obs = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          const fill = e.target.querySelector(".conf-meter-fill");
          if (fill) fill.classList.add("in-view");
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.3 });
    cards.forEach(c => {
      if (c.querySelector(".conf-meter-fill")) obs.observe(c);
    });
  } else {
    grid.querySelectorAll(".conf-meter-fill").forEach(el => el.classList.add("in-view"));
  }

  // -- 3) AI Pick Explainer chat --
  cards.forEach(card => wireChat(card));
}

function wireChat(card) {
  const askBtn = card.querySelector(".ask-btn");
  if (!askBtn) return;

  const panel    = card.querySelector(".chat-panel");
  const msgsEl   = card.querySelector(".chat-msgs");
  const form     = card.querySelector(".chat-form");
  const input    = card.querySelector(".chat-input");
  const sendBtn  = card.querySelector(".chat-send");
  const chips    = card.querySelectorAll(".chat-chip");
  const history  = []; // [{role, content}]

  let pickData = null;
  try { pickData = JSON.parse(card.dataset.pick || "null"); } catch (_) { pickData = null; }
  if (!pickData) return;

  askBtn.addEventListener("click", () => {
    card.classList.toggle("chat-open");
    askBtn.textContent = card.classList.contains("chat-open") ? "✕ Close" : "💬 Ask Claude";
    if (card.classList.contains("chat-open")) {
      setTimeout(() => input && input.focus(), 60);
    }
  });

  function appendMessage(role, content, opts = {}) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}${opts.error ? " error" : ""}`;
    if (opts.html) div.innerHTML = content; else div.textContent = content;
    msgsEl.appendChild(div);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    return div;
  }

  async function send(question) {
    const q = (question || "").trim();
    if (!q) return;
    input.value = "";
    sendBtn.disabled = true;
    appendMessage("user", q);
    history.push({ role: "user", content: q });

    const typing = appendMessage("assistant", `<span class="chat-typing"><span></span><span></span><span></span></span>`, { html: true });

    try {
      const res = await fetch("/api/claude-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pick: pickData, messages: history }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const msg = err.message || err.error || `HTTP ${res.status}`;
        typing.remove();
        appendMessage("assistant", `Couldn't reach the analyst — ${msg}`, { error: true });
        // Don't push error responses into history
      } else {
        const data = await res.json();
        const text = (data.text || "").trim();
        typing.remove();
        appendMessage("assistant", text || "(empty response)");
        if (text) history.push({ role: "assistant", content: text });
      }
    } catch (e) {
      typing.remove();
      appendMessage("assistant", `Network error — ${e.message}`, { error: true });
    } finally {
      sendBtn.disabled = false;
      input && input.focus();
    }
  }

  form && form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });

  chips.forEach(chip => {
    chip.addEventListener("click", () => send(chip.textContent));
  });
}

