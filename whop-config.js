// =============================================================
// SMARTERPICKS — Whop config (single source of truth)
// =============================================================
// Every page that talks to Whop reads its IDs and endpoints from
// here so a product/client switch happens in ONE place instead
// of three (callback.html, login.html, picks-loader.js previously
// each had their own copy that could drift).
//
// To switch product tiers later: change PRODUCT_ID below, redeploy,
// done. No file-by-file hunt required.
//
// Load this with <script src="whop-config.js"></script> BEFORE any
// script that depends on it. picks-loader.js, callback.html, and
// login.html all do this.
// =============================================================

window.WHOP_CONFIG = {
  // ── Identifiers (from your Whop dashboard) ──
  CLIENT_ID:   "app_5RPKKi5gvHwpvo",
  PRODUCT_ID:  "prod_JAuXj9K2RJUjd",   // SmarterPicks main; switch to prod_4yaKtjgti7F9m for Premium-only gating

  // ── OAuth + API endpoints (rarely change) ──
  AUTHORIZE_URL: "https://api.whop.com/oauth/authorize",
  TOKEN_URL:     "https://api.whop.com/oauth/token",
  USERINFO_URL:  "https://api.whop.com/oauth/userinfo",
  ACCESS_BASE:   "https://api.whop.com/api/v1/users",
  SCOPE:         "openid profile email",

  // ── Checkout links (used by login.html) ──
  CHECKOUT_URL_MONTHLY: "https://whop.com/checkout/plan_VIlKaPMvfDPoj",
  CHECKOUT_URL_ANNUAL:  "https://whop.com/checkout/plan_UvETXgAfoUQj5",
};
