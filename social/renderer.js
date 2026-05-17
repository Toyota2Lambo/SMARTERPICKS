#!/usr/bin/env node
/**
 * SMARTERPICKS — Social image renderer
 * ====================================
 * Reads a content.json (produced by social_generator.py) plus the
 * HTML templates in social/templates/ and produces one PNG per
 * required render in the same dated directory.
 *
 * Usage:
 *   node social/renderer.js social/2026-05-09/content.json
 *
 * The directory of the content file is also where the output PNGs land,
 * so /social/2026-05-09/content.json → /social/2026-05-09/*.png.
 *
 * Architecture
 * ------------
 *  1. Load content.json.
 *  2. Build a render manifest — {template, content, output, size} per
 *     image. Carousels expand to one render per slide.
 *  3. For each render, do server-side {{placeholder}} substitution
 *     against the template HTML, write a temp file, point Puppeteer at
 *     it, screenshot at the exact viewport.
 *  4. Write a manifest.json next to the PNGs so ig_publisher.py knows
 *     what's in this directory and which file maps to which post.
 *
 * Why not client-side templating? Puppeteer + a bunch of inline JS
 * adds rendering races (font loading, layout reflow). String substitution
 * before the HTML ever hits Chromium is deterministic — same inputs,
 * pixel-identical outputs.
 *
 * Requires: npm i puppeteer
 */

const fs   = require("fs/promises");
const path = require("path");
const os   = require("os");
const REGISTRY = require("./templates-registry");

const TEMPLATE_DIR = path.join(__dirname, "templates");
const FEED_SIZE  = { width: 1080, height: 1080 };
const STORY_SIZE = { width: 1080, height: 1920 };

// ────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────

/** HTML-escape so user content can't break out of the template
 *  (especially apostrophes from Claude-generated copy). */
function esc(s) {
  return String(s ?? "")
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#039;");
}

/** Wrap "key" tokens (numbers, odds, currency, units, percentages,
 *  W-L records, and a few brand-specific tokens) in <span class="kw">
 *  so they render in the accent gold on dark background. Editorial
 *  emphasis on the things readers actually care about in a betting
 *  post: the lines and the receipts. Everything else stays plain.
 *
 *  Returns ESCAPED HTML — pass to a {{key_html}} placeholder, which
 *  the renderer's fillTemplate() lets through unescaped. */
const KW_TOKEN_RE = new RegExp([
  // Currency: $29, $199, $3,840.50
  '\\$[\\d,]+(?:\\.\\d+)?',
  // Signed numbers with optional unit/percent: +135, -1.5, +3.7u, -2.0u, +9.3%
  '[+\\-]\\d+(?:\\.\\d+)?[u%]?\\b',
  // Records / score-like: 5-2, 226-186, 233-197-22
  '\\b\\d+-\\d+(?:-\\d+)?\\b',
  // Trailing unit / percentage: 38u, 9.3%, 54%
  '\\b\\d+(?:\\.\\d+)?[u%]\\b',
  // Specific brand tokens
  '\\b(?:FREE30|WON|LOST|PUSH)\\b',
].join('|'), 'g');

function colorize(text) {
  if (!text) return "";
  // First escape so user content can't break out of the template.
  // Then wrap matches. The wrapped span tags survive because esc()
  // only neutralizes &, <, >, ", ' — not letters or punctuation.
  return esc(text).replace(KW_TOKEN_RE, (m) => {
    if (m === "WON")  return '<span class="kw win">'  + m + '</span>';
    if (m === "LOST") return '<span class="kw loss">' + m + '</span>';
    if (m === "PUSH") return '<span class="kw push">' + m + '</span>';
    return '<span class="kw">' + m + '</span>';
  });
}

/** Map league string to a sport emoji. Mirrors picks-loader.js's
 *  sportIcon() so the IG slide matches the site exactly. */
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

/** Pull "Confidence A/B/C±" out of the tags array and return a meter
 *  decimal (0..1), the label, and the CSS class. Same logic as
 *  picks-loader.js's confidenceInfo() so site and IG stay aligned. */
function confidenceInfo(tags) {
  const found = (tags || []).find(t => /confidence/i.test(t));
  if (!found) return { letter: "B", label: "B", pct: 0.75 };
  const m = found.match(/confidence\s*([abc])\s*([+-])?/i);
  if (!m) return { letter: "B", label: "B", pct: 0.75 };
  const letter = m[1].toUpperCase();
  const mod = m[2] || "";
  const baseline = { A: 92, B: 75, C: 50 }[letter] || 75;
  const adj = mod === "+" ? 8 : mod === "-" ? -8 : 0;
  const pct = Math.max(20, Math.min(100, baseline + adj)) / 100;
  return { letter, label: `${letter}${mod}`, pct };
}

/** Build the placeholder set for site-pick-card.html from a pick object
 *  loaded from picks.json. Returns ONLY plain text — except tags_html,
 *  which we render here because the template renderer doesn't allow
 *  HTML through {{placeholders}} (it escapes them for safety). */
function livePickFields(p) {
  const conf = confidenceInfo(p.tags);
  // Tags minus the Confidence one (it's rendered as the meter)
  const visibleTags = (p.tags || []).filter(t => !/confidence/i.test(t));
  const tagsHtml = visibleTags
    .map(t => `<span class="pick-tag">${esc(t)}</span>`)
    .join("");

  return {
    sport_icon:  sportIcon(p.league),
    league:      p.league      || "",
    time:        p.time        || "",
    away_team:   p.away_team   || "",
    home_team:   p.home_team   || "",
    game_detail: p.game_detail || "",
    conf_pct:    String(conf.pct),
    conf_letter: conf.label,
    pick:        p.pick        || "",
    odds:        p.odds        || "",
    book:        p.book        || "",
    stake:       p.stake       || "",
    reasoning:   p.reasoning   || "",
    // tags_html is pre-rendered HTML. The renderer's substitute() routine
    // recognizes the _html suffix and skips escaping for those keys.
    tags_html:   tagsHtml,
  };
}

/** Drop any text that isn't safe to put inside a CSS data attribute
 *  (used for things like data-direction). */
function attr(s) {
  return String(s ?? "").replace(/[^a-z0-9_-]/gi, "");
}

/** Replace every {{key}} in `tpl` with the corresponding value from
 *  `data` (HTML-escaped). Unknown keys become empty strings — we don't
 *  want a literal {{some_field}} leaking onto an Instagram post. */
function fillTemplate(tpl, data) {
  return tpl.replace(/\{\{(\w+)\}\}/g, (_, key) => {
    if (key === "size" || key === "direction") return attr(data[key]);
    if (key.endsWith("_html"))                 return data[key] ?? ""; // pre-rendered HTML, do not escape
    return esc(data[key]);
  });
}

/** Build the small dot/pip strip used by educational-carousel.html. */
function pipsHtml(activeIndex, total) {
  let out = "";
  for (let i = 1; i <= total; i++) {
    out += `<span class="pip${i === activeIndex ? " active" : ""}"></span>`;
  }
  return out;
}

// ────────────────────────────────────────────────────────────
// Manifest builder — turns content.json into a list of renders
// ────────────────────────────────────────────────────────────
function buildManifest(payload) {
  const c = payload.content || {};
  const renders = [];

  // 1) Daily pick post — 3-slide carousel (feed)
  // Slide 1: editorial hook (daily-pick-card.html)
  // Slide 2: faithful copy of the live site pick card (site-pick-card.html)
  //          falls back to the hook template if today_free_pick is missing
  // Slide 3: CTA / "see the full card" (daily-pick-card.html)
  if (c.ig_pick_post) {
    const livePick = (payload.source && payload.source.today_free_pick) || null;

    // 2-slide carousel only — the templated headline slide read as ad
    // copy on the grid and was getting skipped at publish anyway. Files
    // keep the pick-post-2 / pick-post-3 names so ig_publisher's filter
    // doesn't have to change. Slide pager renumbered to 1/2 and 2/2 so
    // the on-image counter matches what viewers actually see.

    const slideCard = livePick
      ? {
          template: "site-pick-card.html",
          output:   "pick-post-2.png",
          group:    "ig_pick_post",
          slide_index: 1,
          size:     FEED_SIZE,
          content:  Object.assign(
            { size: "feed", slide_index: 1, slide_total: 2, numeral: "01" },
            livePickFields(livePick)
          ),
        }
      : {
          template: "daily-pick-card.html",
          output:   "pick-post-2.png",
          group:    "ig_pick_post",
          slide_index: 1,
          size:     FEED_SIZE,
          content:  {
            size:           "feed",
            slide_label:    "The Play",
            slide_text:     c.ig_pick_post.slide2_text,
            slide_text_html: colorize(c.ig_pick_post.slide2_text),
            slide_index:    1,
            slide_total:    2,
            numeral:        "01",
          },
        };

    const slideCTA = {
      template: "daily-pick-card.html",
      output:   "pick-post-3.png",
      group:    "ig_pick_post",
      slide_index: 2,
      size:     FEED_SIZE,
      content:  {
        size:           "feed",
        slide_label:    "The Full Card",
        slide_text:     c.ig_pick_post.slide3_text,
        slide_text_html: colorize(c.ig_pick_post.slide3_text),
        slide_index:    2,
        slide_total:    2,
        numeral:        "02",
      },
    };

    renders.push(slideCard, slideCTA);
  }

  // 2) Yesterday's results — 1 image (feed)
  if (c.ig_results_post) {
    // The headline_text from Claude looks like "Yesterday: 5-2 · +3.7u".
    // Pull out the W-L and the units so we can size them properly.
    const head = c.ig_results_post.headline_text || "";
    const recordMatch = head.match(/(\d+-\d+(?:-\d+)?)/);
    const unitsMatch  = head.match(/([+\-]?\d+(?:\.\d+)?)u/);
    const record  = recordMatch ? recordMatch[1] : "0-0";
    const units   = unitsMatch  ? `${unitsMatch[1]}u` : "+0u";
    const isWin   = !units.startsWith("-");

    // The picks list comes from the source results block, not Claude.
    const sourcePicks = (payload.source && payload.source.results_picks) || [];
    const picksHtml = sourcePicks.slice(0, 7).map(p => {
      const r = String(p.result || "").toUpperCase();
      const cls = r === "WON" ? "win" : r === "LOST" ? "loss" : "push";
      const u = Number(p.units || 0);
      const sign = u >= 0 ? "+" : "";
      return `<li>
        <span>${esc(p.pick || "—")}</span>
        <span><span class="result ${cls}">${esc(r || "—")}</span>
              &nbsp;&nbsp;${sign}${u.toFixed(2)}u</span>
      </li>`;
    }).join("");

    renders.push({
      template: "results-recap.html",
      output:   "results-recap.png",
      group:    "ig_results_post",
      size:     FEED_SIZE,
      content:  {
        size:             "feed",
        date_text:        (payload.source && payload.source.results_date) || "Yesterday",
        record_text:      record,
        net_units_text:   units,
        net_units_class:  isWin ? "win" : "loss",
        picks_html:       picksHtml || "<li><span>No picks logged</span><span>—</span></li>",
      },
    });
  }

  // 3) Educational carousel — 5 slides (feed)
  if (c.ig_carousel_topic) {
    const k = c.ig_carousel_topic;
    const slides = [
      { role: "Title",   text: k.slide1_text },
      { role: "Setup",   text: k.slide2_text },
      { role: "Insight", text: k.slide3_text },
      { role: "Example", text: k.slide4_text },
      { role: "Payoff",  text: k.slide5_text },
    ];
    slides.forEach((s, i) => renders.push({
      template: "educational-carousel.html",
      output:   `educational-${i + 1}.png`,
      group:    "ig_carousel_topic",
      slide_index: i + 1,
      size:     FEED_SIZE,
      content:  {
        size:            "feed",
        topic:           k.topic,
        slide_role:      s.role,
        slide_text:      s.text,
        slide_text_html: colorize(s.text),
        slide_index:     i + 1,
        slide_total:     slides.length,
        pips_html:       pipsHtml(i + 1, slides.length),
        numeral:         String(i + 1).padStart(2, "0"),
      },
    }));
  }

  // 4) Story sequence — 5 vertical stories
  if (Array.isArray(c.story_sequence)) {
    const labels = ["Morning", "Midday Poll", "Pre-Lock", "Live Tracker", "Night Recap"];
    c.story_sequence.forEach((text, i) => renders.push({
      template: "daily-pick-card.html",   // re-use card template at story size
      output:   `story-${i + 1}.png`,
      group:    "story_sequence",
      slide_index: i + 1,
      size:     STORY_SIZE,
      content:  {
        size:            "story",
        slide_label:     labels[i] || `Story ${i + 1}`,
        slide_text:      text,
        slide_text_html: colorize(text),
        slide_index:     i + 1,
        slide_total:     c.story_sequence.length,
        numeral:         String(i + 1).padStart(2, "0"),
      },
    }));
  }

  // 5) Meme post — 1 image (feed)
  if (c.meme_post) {
    renders.push({
      template: "meme-post.html",
      output:   "meme.png",
      group:    "meme_post",
      size:     FEED_SIZE,
      content:  {
        size:          "feed",
        top_text:      c.meme_post.top_text,
        bottom_text:   c.meme_post.bottom_text,
        image_concept: c.meme_post.image_concept || "",
      },
    });
  }

  // 6) Generic "treatments" — flexible array using any of the 10 modern
  // visual templates registered in templates-registry.js. Each entry:
  //   { template, output, group, slide_index, size, fields }
  // The registry's expand() turns logical fields (e.g. recap-card's
  // `pick_rows` array, chart-card's `bars`, index-card's `cells`) into
  // the *_html chunk placeholders the templates substitute against.
  // Unknown templates are warned + skipped — never silently rendered as
  // a literal "{{name}}" surfacing on the IG feed.
  if (Array.isArray(c.treatments)) {
    c.treatments.forEach((t, i) => {
      const spec = REGISTRY.specFor(t.template);
      if (!spec) {
        console.warn(`  ⚠ treatments[${i}]: unknown template '${t.template}' — skipping`);
        return;
      }
      const sizeKey  = t.size === "story" ? "story" : "feed";
      const sizeDim  = sizeKey === "story" ? STORY_SIZE : FEED_SIZE;
      const logical  = Object.assign({}, t.fields || {}, { size: sizeKey });
      const expanded = REGISTRY.expandFields(t.template, logical);
      renders.push({
        template:    t.template,
        output:      t.output || `treatment-${String(i + 1).padStart(2, "0")}.png`,
        group:       t.group  || "treatments",
        slide_index: t.slide_index || (i + 1),
        size:        sizeDim,
        content:     expanded,
      });
    });
  }

  return renders;
}

// ────────────────────────────────────────────────────────────
// Main
// ────────────────────────────────────────────────────────────
async function main() {
  const contentPath = process.argv[2];
  if (!contentPath) {
    console.error("Usage: node renderer.js <path/to/content.json>");
    process.exit(1);
  }

  const absContent = path.resolve(contentPath);
  const outDir = path.dirname(absContent);

  const payload = JSON.parse(await fs.readFile(absContent, "utf8"));
  const renders = buildManifest(payload);
  if (renders.length === 0) {
    console.error("No renders to do — content.json is empty.");
    process.exit(2);
  }

  console.log(`📐 Rendering ${renders.length} images to ${outDir}`);

  // Lazy-require puppeteer so the script can at least parse + show usage
  // even when puppeteer hasn't been installed.
  const puppeteer = require("puppeteer");
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  // Templates load _shared.css via a relative <link>. Puppeteer needs
  // to resolve that from the template's own directory, so we point file:
  // URLs at TEMPLATE_DIR (with a temp-file alongside _shared.css).
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "spx-render-"));
  await fs.copyFile(path.join(TEMPLATE_DIR, "_shared.css"), path.join(tempDir, "_shared.css"));

  try {
    const page = await browser.newPage();
    page.on("pageerror", err => console.warn("  page error:", err.message));

    const manifestEntries = [];
    for (const r of renders) {
      const tplPath = path.join(TEMPLATE_DIR, r.template);
      const tplHtml = await fs.readFile(tplPath, "utf8");
      const filled  = fillTemplate(tplHtml, r.content);

      const tempHtml = path.join(tempDir, `render-${Date.now()}-${Math.random().toString(36).slice(2,8)}.html`);
      await fs.writeFile(tempHtml, filled);

      await page.setViewport({ width: r.size.width, height: r.size.height, deviceScaleFactor: 1 });
      await page.goto(`file://${tempHtml}`, { waitUntil: "networkidle0" });
      // Wait for fonts to settle so headlines don't render in the fallback face.
      await page.evaluate(() => document.fonts && document.fonts.ready);
      // One more frame to let layout settle after font swap
      await new Promise(r => setTimeout(r, 60));

      const outPath = path.join(outDir, r.output);
      await page.screenshot({ path: outPath, type: "png", omitBackground: false });
      console.log(`  ✓ ${r.output}  (${r.size.width}×${r.size.height})`);

      manifestEntries.push({
        file:        r.output,
        template:    r.template,
        group:       r.group,
        slide_index: r.slide_index || null,
        size:        r.size,
      });

      await fs.unlink(tempHtml).catch(() => {});
    }

    // Write a small manifest for ig_publisher.py to consume.
    const manifestPath = path.join(outDir, "manifest.json");
    await fs.writeFile(manifestPath, JSON.stringify({
      rendered_at: new Date().toISOString(),
      content_file: path.basename(absContent),
      renders: manifestEntries,
    }, null, 2));
    console.log(`\n✅ Wrote ${path.basename(manifestPath)}`);
  } finally {
    await browser.close();
    await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
  }
}

main().catch(err => {
  console.error("❌ Renderer failed:", err.stack || err.message);
  process.exit(2);
});
