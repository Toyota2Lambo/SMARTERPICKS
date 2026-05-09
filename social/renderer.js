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
  if (c.ig_pick_post) {
    const slides = [
      { label: "Today's Free Pick", text: c.ig_pick_post.slide1_text },
      { label: "The Play",          text: c.ig_pick_post.slide2_text },
      { label: "The Full Card",     text: c.ig_pick_post.slide3_text },
    ];
    slides.forEach((s, i) => renders.push({
      template: "daily-pick-card.html",
      output:   `pick-post-${i + 1}.png`,
      group:    "ig_pick_post",
      slide_index: i + 1,
      size:     FEED_SIZE,
      content:  {
        size:        "feed",
        slide_label: s.label,
        slide_text:  s.text,
        slide_index: i + 1,
        slide_total: slides.length,
      },
    }));
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
        size:        "feed",
        topic:       k.topic,
        slide_role:  s.role,
        slide_text:  s.text,
        slide_index: i + 1,
        slide_total: slides.length,
        pips_html:   pipsHtml(i + 1, slides.length),
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
        size:        "story",
        slide_label: labels[i] || `Story ${i + 1}`,
        slide_text:  text,
        slide_index: i + 1,
        slide_total: c.story_sequence.length,
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
