// =============================================================
// Vercel serverless: Newsletter signup
// =============================================================
// Frontend POSTs { email } from the signup form on index.html.
// We validate the address, rate-limit by IP (a public endpoint),
// then forward to the configurable webhook in NEWSLETTER_WEBHOOK_URL.
//
// The webhook URL is YOUR choice — wire it to:
//   - A Zapier / Make.com / n8n hook that adds rows to a sheet
//   - Beehiiv's subscribe API (auth header in NEWSLETTER_WEBHOOK_AUTH)
//   - ConvertKit, Mailchimp, Resend audiences, or any other ESP
//   - Your own collection endpoint
//
// REQUIRED VERCEL ENV VARS:
//   NEWSLETTER_WEBHOOK_URL  — POST endpoint that receives signups
//   NEWSLETTER_WEBHOOK_AUTH — (optional) value placed in Authorization
//                             header. e.g. "Bearer beehiiv_xxxxx"
//
// If NEWSLETTER_WEBHOOK_URL is unset the endpoint accepts the email
// (returns 202) and logs it. That keeps the form working in dev /
// before you've wired up a provider — but check Vercel logs to harvest
// any signups collected during that window.
// =============================================================

const PER_IP_LIMIT   = 5;        // signups per window per IP — well below abuse, way above honest use
const RATE_WINDOW_MS = 60_000;
const ipHits         = new Map();

function pruneRecent(arr, now) {
  let i = 0;
  while (i < arr.length && now - arr[i] >= RATE_WINDOW_MS) i++;
  if (i > 0) arr.splice(0, i);
}

function checkRateLimit(ip) {
  const now = Date.now();
  const arr = ipHits.get(ip) || [];
  pruneRecent(arr, now);
  if (arr.length >= PER_IP_LIMIT) return false;
  arr.push(now);
  ipHits.set(ip, arr);
  if (ipHits.size > 1000) {
    for (const [k, v] of ipHits) {
      pruneRecent(v, now);
      if (v.length === 0) ipHits.delete(k);
    }
  }
  return true;
}

function clientIp(req) {
  const fwd = req.headers["x-forwarded-for"];
  if (fwd) return String(fwd).split(",")[0].trim();
  return req.headers["x-real-ip"] || req.socket?.remoteAddress || "unknown";
}

// Conservative email validator. Doesn't try to be RFC-perfect — just
// catches the obvious garbage (no @, no domain, whitespace, junk).
function isValidEmail(s) {
  if (typeof s !== "string") return false;
  const trimmed = s.trim();
  if (trimmed.length < 5 || trimmed.length > 254) return false;
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed);
}

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "method_not_allowed" });
  }

  const ip = clientIp(req);
  if (!checkRateLimit(ip)) {
    res.setHeader("Retry-After", "60");
    return res.status(429).json({
      error: "rate_limited",
      message: "Too many signups in a short window — try again in a minute.",
    });
  }

  const email = (req.body && req.body.email || "").trim().toLowerCase();
  if (!isValidEmail(email)) {
    return res.status(400).json({ error: "invalid_email" });
  }

  const webhookUrl  = process.env.NEWSLETTER_WEBHOOK_URL;
  const webhookAuth = process.env.NEWSLETTER_WEBHOOK_AUTH;

  if (!webhookUrl) {
    // No provider wired up yet — log so the operator can scrape from
    // Vercel logs, and return 202 so the user gets a success message.
    console.log(`[newsletter] (no webhook) email=${email} ip=${ip} ts=${new Date().toISOString()}`);
    return res.status(202).json({ ok: true, queued: true });
  }

  try {
    const headers = { "Content-Type": "application/json" };
    if (webhookAuth) headers["Authorization"] = webhookAuth;

    const upstream = await fetch(webhookUrl, {
      method: "POST",
      headers,
      body: JSON.stringify({
        email,
        source: "smarterpicks-site",
        timestamp: new Date().toISOString(),
      }),
    });

    if (!upstream.ok) {
      const text = await upstream.text().catch(() => "");
      console.error(`[newsletter] webhook ${upstream.status}: ${text.slice(0, 200)}`);
      return res.status(502).json({ error: "webhook_failed", status: upstream.status });
    }
    return res.status(200).json({ ok: true });
  } catch (e) {
    console.error("[newsletter] webhook error:", e.message);
    return res.status(502).json({ error: "fetch_failed", detail: e.message });
  }
};
