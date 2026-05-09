// =============================================================
// Vercel serverless: Claude proxy for the AI Pick Explainer
// =============================================================
// Frontend POSTs { pick, messages } where:
//   pick     = the pick object (fields: pick, league, away_team,
//              home_team, game_detail, time, odds, book, stake,
//              reasoning, tags)
//   messages = chat history [{role:"user"|"assistant",content}]
//
// We build a system prompt with the pick context, forward to
// Anthropic with the chat history, and return Claude's reply.
//
// REQUIRED VERCEL ENV VAR:
//   ANTHROPIC_API_KEY — copy from console.anthropic.com,
//   add in Vercel: Settings → Environment Variables.
//
// =============================================================
// RATE LIMITING — in-memory, best-effort
// =============================================================
// Each Vercel serverless instance keeps its own counter Map.
// Two limits run together:
//   - Per-IP: 30 requests / 60s. Stops a single attacker.
//   - Global: 1500 requests / 60s. Circuit-breaker so a botnet
//     can't drain the API budget through many IPs.
//
// This is best-effort only — Vercel may run multiple instances
// in parallel under load, so a determined attacker spread across
// instances could exceed the cap. For higher-traffic deployments,
// swap to a shared store like Upstash Redis + @upstash/ratelimit.
// For an early-stage site, in-memory is significantly better than
// nothing and adds zero infrastructure.
// =============================================================

const MODEL_ID = "claude-sonnet-4-6";

const PER_IP_LIMIT     = 30;       // requests per window per IP
const GLOBAL_LIMIT     = 1500;     // requests per window across all IPs
const RATE_WINDOW_MS   = 60_000;   // 1-minute rolling window
const MAX_QUESTION_LEN = 600;      // tighter than the 2000-char overall cap

// Module-scoped state — survives across invocations within an instance
const ipHits     = new Map();      // ip -> [timestamps]
const globalHits = [];             // [timestamps]

function pruneRecent(arr, now) {
  // Drop timestamps older than the window. Mutates in place to avoid GC churn.
  let i = 0;
  while (i < arr.length && now - arr[i] >= RATE_WINDOW_MS) i++;
  if (i > 0) arr.splice(0, i);
}

function checkRateLimit(ip) {
  const now = Date.now();

  pruneRecent(globalHits, now);
  if (globalHits.length >= GLOBAL_LIMIT) {
    return { ok: false, retryAfter: 60, scope: "global" };
  }

  const arr = ipHits.get(ip) || [];
  pruneRecent(arr, now);
  if (arr.length >= PER_IP_LIMIT) {
    return { ok: false, retryAfter: 60, scope: "ip" };
  }

  arr.push(now);
  globalHits.push(now);
  ipHits.set(ip, arr);

  // Periodic cleanup so the Map doesn't accumulate dead entries from
  // visitors who pinged us once an hour ago.
  if (ipHits.size > 2000) {
    for (const [k, v] of ipHits) {
      pruneRecent(v, now);
      if (v.length === 0) ipHits.delete(k);
    }
  }

  return { ok: true };
}

function clientIp(req) {
  // Vercel sets x-forwarded-for with the real client IP first.
  const fwd = req.headers["x-forwarded-for"];
  if (fwd) return String(fwd).split(",")[0].trim();
  return req.headers["x-real-ip"] || req.socket?.remoteAddress || "unknown";
}

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "method_not_allowed" });
  }

  // Rate-limit BEFORE doing any work — we don't want to validate
  // payloads (a CPU/IO cost) for requests we're going to reject.
  const ip = clientIp(req);
  const rate = checkRateLimit(ip);
  if (!rate.ok) {
    res.setHeader("Retry-After", String(rate.retryAfter));
    return res.status(429).json({
      error: "rate_limited",
      message: rate.scope === "global"
        ? "Service is busy right now — give it a minute and try again."
        : "Too many questions in a short window — try again in a minute.",
    });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return res.status(500).json({
      error: "server_misconfigured",
      message: "ANTHROPIC_API_KEY is not set in Vercel env vars.",
    });
  }

  const { pick, messages } = req.body || {};
  if (!pick || typeof pick !== "object") {
    return res.status(400).json({ error: "missing_pick" });
  }
  if (!Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: "missing_messages" });
  }

  const systemPrompt = [
    "You are a sharp sports betting analyst for SmarterPicks.",
    "The user is asking about ONE specific pick we published. Stay focused on it.",
    "",
    "Pick context:",
    `  League:    ${pick.league || "—"}`,
    `  Game:      ${pick.away_team || "?"} at ${pick.home_team || "?"}${pick.game_detail ? " · " + pick.game_detail : ""}`,
    `  Time:      ${pick.time || "—"}`,
    `  The play:  ${pick.pick || "—"}`,
    `  Odds:      ${pick.odds || "—"} at ${pick.book || "—"}`,
    `  Stake:     ${pick.stake || "—"}`,
    `  Reasoning: ${pick.reasoning || "—"}`,
    `  Tags:      ${(pick.tags || []).join(", ") || "—"}`,
    "",
    "Style:",
    "- Tight, analytical, conversational. 2-4 sentences max.",
    "- Reference specific numbers (line, odds, edge, situational).",
    "- Don't repeat the pick context the user can already see.",
    "- If they ask something off-topic, gently redirect to this pick.",
    "- Don't recommend wagering more than they're comfortable with. 21+.",
  ].join("\n");

  // Cap messages defensively: keep last 12, only well-formed roles,
  // truncate runaway content. The per-question cap (MAX_QUESTION_LEN)
  // is tighter than the per-message cap because the user-facing
  // input box should produce short conversational questions, not essays.
  const trimmed = messages
    .filter(m => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
    .slice(-12)
    .map((m, i, arr) => ({
      role: m.role,
      content: i === arr.length - 1 && m.role === "user"
        ? m.content.slice(0, MAX_QUESTION_LEN)
        : m.content.slice(0, 2000),
    }));

  if (trimmed.length === 0 || trimmed[trimmed.length - 1].role !== "user") {
    return res.status(400).json({ error: "last_message_must_be_user" });
  }

  try {
    const upstream = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key":         apiKey,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
      },
      body: JSON.stringify({
        model:      MODEL_ID,
        max_tokens: 500,
        system:     systemPrompt,
        messages:   trimmed,
      }),
    });

    const text = await upstream.text();
    if (!upstream.ok) {
      return res.status(upstream.status).json({
        error: "upstream_error",
        status: upstream.status,
        detail: text.slice(0, 500),
      });
    }
    const data = JSON.parse(text);
    const reply = (data.content && data.content[0] && data.content[0].text) || "";
    return res.status(200).json({ text: reply.trim() });
  } catch (e) {
    return res.status(502).json({ error: "fetch_failed", detail: e.message });
  }
};
