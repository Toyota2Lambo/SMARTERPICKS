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
// =============================================================

const MODEL_ID = "claude-sonnet-4-6";

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "method_not_allowed" });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return res.status(500).json({
      error: "server_misconfigured",
      message: "ANTHROPIC_API_KEY is not set in Vercel env vars.",
    });
  }

  const { pick, messages } = req.body || {};
  if (!pick || !Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: "missing_pick_or_messages" });
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

  // Cap messages defensively (in case a runaway client posts a giant history).
  const trimmed = messages
    .filter(m => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
    .slice(-12)
    .map(m => ({ role: m.role, content: m.content.slice(0, 2000) }));

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
