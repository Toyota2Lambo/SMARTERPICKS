// =============================================================
// Vercel serverless: server-side Whop OAuth token exchange
// =============================================================
// Whop's OAuth app is a confidential client and rejects
// /oauth/token without a client_secret. We can't put the secret
// in browser code, so this proxy adds it server-side.
//
// REQUIRED VERCEL ENV VAR:
//   WHOP_CLIENT_SECRET — copy from your Whop OAuth app dashboard,
//   then add in Vercel: Settings -> Environment Variables.
//
// The frontend posts the same body shape it would have posted
// directly to Whop. We inject client_secret here, forward to Whop,
// and return Whop's response verbatim (same shape).
// =============================================================

const CLIENT_ID = "app_5RPKKi5gvHwpvo";
const TOKEN_URL = "https://api.whop.com/oauth/token";

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "method_not_allowed" });
  }

  const secret = process.env.WHOP_CLIENT_SECRET;
  if (!secret) {
    return res.status(500).json({
      error: "server_misconfigured",
      error_description: "WHOP_CLIENT_SECRET is not set in Vercel env vars.",
    });
  }

  const incoming = req.body || {};
  const body = {
    client_id:     CLIENT_ID,
    client_secret: secret,
    grant_type:    incoming.grant_type,
  };

  if (incoming.grant_type === "authorization_code") {
    body.code          = incoming.code;
    body.code_verifier = incoming.code_verifier;
    body.redirect_uri  = incoming.redirect_uri;
  } else if (incoming.grant_type === "refresh_token") {
    body.refresh_token = incoming.refresh_token;
  } else {
    return res.status(400).json({ error: "unsupported_grant_type" });
  }

  try {
    const upstream = await fetch(TOKEN_URL, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    const text = await upstream.text();
    res.status(upstream.status);
    try {
      res.json(JSON.parse(text));
    } catch (_) {
      res.send(text);
    }
  } catch (e) {
    res.status(502).json({
      error: "upstream_failed",
      error_description: e.message,
    });
  }
};
