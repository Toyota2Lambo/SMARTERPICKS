// ============================================================
// SMARTERPICKS — Auth chrome
//
// One small shared script that makes every page visually
// distinguish the visitor's state. Three states:
//
//   member       — has whop_token + has_access:true
//   no-access    — has whop_token but has_access:false (signed in,
//                  no active subscription)
//   guest        — no whop_token at all
//
// What it does:
//   1. Tags <body> with data-auth-state so CSS can react —
//      any element with [data-auth-show="member"|"guest"|"no-access"]
//      gets shown/hidden automatically.
//   2. Injects a thin colored status bar at the top of the page
//      for signed-in users (green for member, gold for no-access).
//      Guests see nothing extra. Skipped if the page already has
//      its own .member-bar (so members.html / billing.html stay
//      in charge of their own chrome).
//   3. Wires any [data-action="logout"] link/button to clear Whop
//      tokens and reload.
//
// Inject it once per public page with <script src="auth-chrome.js">.
// Members-only pages (members.html, billing.html) don't need it
// because they already have a permanent member-bar — but it's
// harmless to include there too.
// ============================================================

(function () {
  injectStyles();

  function init() {
    const state = detectState();
    document.body.dataset.authState = state;

    if (state === "member" || state === "no-access") {
      maybeInsertStatusBar(state);
    }

    // Event delegation — works for any logout link added later too.
    document.addEventListener("click", (e) => {
      const t = e.target.closest('[data-action="logout"]');
      if (!t) return;
      e.preventDefault();
      logout();
    });
  }

  function detectState() {
    const token = localStorage.getItem("whop_token");
    if (!token) return "guest";
    try {
      const access = JSON.parse(localStorage.getItem("whop_access") || "{}");
      return access && access.has_access ? "member" : "no-access";
    } catch (_) {
      return "guest";
    }
  }

  function maybeInsertStatusBar(state) {
    // Don't double-insert if the page already has its own bar.
    if (document.getElementById("auth-status-bar")) return;
    if (document.querySelector(".member-bar"))      return;

    const bar = document.createElement("div");
    bar.id = "auth-status-bar";
    bar.className = "auth-status-bar " + state;

    if (state === "member") {
      bar.innerHTML = `
        <span class="auth-dot"></span>
        <strong>Premium member</strong>
        <span class="auth-sep">·</span>
        <span class="auth-msg">You're signed in across the site</span>
        <span class="auth-sep">·</span>
        <a href="members.html" class="auth-link">Open Members Area →</a>
        <span class="auth-sep">·</span>
        <a href="#" class="auth-link" data-action="logout">Logout</a>
      `;
    } else {
      bar.innerHTML = `
        <span class="auth-dot"></span>
        <strong>Signed in</strong>
        <span class="auth-sep">·</span>
        <span class="auth-msg">No active subscription yet</span>
        <span class="auth-sep">·</span>
        <a href="login.html" class="auth-link">Activate subscription →</a>
        <span class="auth-sep">·</span>
        <a href="#" class="auth-link" data-action="logout">Logout</a>
      `;
    }
    document.body.insertBefore(bar, document.body.firstChild);
  }

  function logout() {
    ["whop_token","whop_refresh","whop_user_id","whop_user","whop_access"]
      .forEach(k => localStorage.removeItem(k));
    location.reload();
  }
  // Expose on window so picks-loader.js (and any future caller) can
  // route through the same path instead of duplicating the cleanup
  // list. Single source of truth for "what does logout actually mean".
  window.smarterpicksLogout = logout;

  // Inject the status-bar CSS + the [data-auth-show] visibility rules
  // so individual pages don't have to duplicate this. Runs immediately
  // at script-eval time so [data-auth-show] elements never flash before
  // body[data-auth-state] is set.
  function injectStyles() {
    if (document.getElementById("auth-chrome-styles")) return;
    const css = `
      /* ── Status bar ─────────────────────────────────────── */
      .auth-status-bar{
        font-family:var(--mono,"JetBrains Mono",monospace);
        font-size:11px;
        font-weight:700;
        letter-spacing:.08em;
        text-transform:uppercase;
        text-align:center;
        padding:9px 16px;
        display:flex;
        align-items:center;
        justify-content:center;
        gap:12px;
        flex-wrap:wrap;
        border-bottom:1px solid;
        position:relative;
        z-index:60;
      }
      .auth-status-bar.member{
        background:rgba(109,190,122,.1);
        border-bottom-color:rgba(109,190,122,.3);
        color:var(--win,#6dbe7a);
      }
      .auth-status-bar.no-access{
        background:rgba(212,168,67,.12);
        border-bottom-color:rgba(212,168,67,.35);
        color:var(--accent,#d4a843);
      }
      .auth-status-bar .auth-dot{
        width:7px;height:7px;
        background:currentColor;
        border-radius:50%;
        display:inline-block;
        flex-shrink:0;
      }
      .auth-status-bar .auth-link{
        color:currentColor;
        text-decoration:underline;
        text-underline-offset:3px;
        text-decoration-thickness:1px;
        cursor:pointer;
      }
      .auth-status-bar .auth-link:hover{opacity:.75;}
      .auth-status-bar .auth-sep{opacity:.35;}
      .auth-status-bar strong{font-weight:700;}
      @media(max-width:640px){
        .auth-status-bar{font-size:10px;gap:8px;padding:8px 12px;}
        .auth-status-bar .auth-sep{display:none;}
      }

      /* ── data-auth-show visibility ────────────────────────
         Hide everything tagged [data-auth-show] until the body
         attribute exists (avoids a flash); then show only the
         elements that match the current state. */
      body:not([data-auth-state]) [data-auth-show]{display:none;}
      body[data-auth-state]:not([data-auth-state="member"])    [data-auth-show="member"]{display:none;}
      body[data-auth-state]:not([data-auth-state="guest"])     [data-auth-show="guest"]{display:none;}
      body[data-auth-state]:not([data-auth-state="no-access"]) [data-auth-show="no-access"]{display:none;}
      /* "signed-in" matches member OR no-access */
      body[data-auth-state="guest"] [data-auth-show="signed-in"]{display:none;}
    `;
    const style = document.createElement("style");
    style.id = "auth-chrome-styles";
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
