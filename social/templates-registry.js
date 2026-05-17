/**
 * SMARTERPICKS — Template registry for the 10 modern social treatments.
 * ====================================================================
 *
 * Single source of truth for every editorial template in social/templates/:
 *   - what role each treatment plays in a content plan
 *   - what fields it expects (logical names, with type hints + notes)
 *   - how to expand chunked-data fields (arrays of rows/cells) into the
 *     HTML chunks the template's placeholders need
 *   - a complete sample payload that renders to a valid image
 *
 * Consumed by:
 *   - renderer.js — to expand `content.json`'s `treatments[]` entries
 *     before substituting placeholders into the template HTML.
 *   - social/sample-payloads.json — kept in lock-step with `sample` values
 *     here so the Python generator can show Claude few-shot examples.
 *   - QA / smoke-test renders that loop through TEMPLATES and dump one
 *     PNG per treatment.
 *
 * Convention: fields whose value can contain HTML markup (e.g. <em> for
 * the gold-accent treatment) end in `_html`. Plain-text fields don't.
 * The renderer's fillTemplate() only skips escaping for `_html`-suffixed
 * keys — matching the existing slide_text / slide_text_html pattern.
 */

// ─────────────────────────────────────────────────────────
// String utilities (mirror the ones in renderer.js so this
// module stays consumable on its own)
// ─────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? "")
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#039;");
}

/** Escape, then re-allow <em>/</em> tags only. For headline fields where
 *  the generator hand-marks emphasis but everything else must be safe. */
function escAllowEm(s) {
  return esc(s)
    .replace(/&lt;em&gt;/g,  "<em>")
    .replace(/&lt;\/em&gt;/g, "</em>");
}

// ─────────────────────────────────────────────────────────
// CHUNK BUILDERS — produce HTML for the *_html placeholders
// used by recap-card, chart-card, and index-card. Each
// accepts an array of plain-data rows and returns the
// rendered HTML chunk the template substitutes in.
// ─────────────────────────────────────────────────────────

/** Build pick-row HTML for recap-card.html's {{pick_rows_html}}.
 *  Each input row: { matchup, play, result: "win"|"loss"|"push", units: number } */
function recapPickRows(picks) {
  return (picks || []).map(p => {
    const r = String(p.result || "push").toLowerCase();
    const cls = ["win","loss","push"].includes(r) ? r : "push";
    const u = Number(p.units || 0);
    const sign = u >= 0 ? "+" : "";
    const unitsLabel = `${sign}${u.toFixed(1)}u`;
    return `<div class="pick-row">
        <span class="pick-result ${cls}"></span>
        <div class="pick-desc">
          <span class="matchup">${esc(p.matchup || "")}</span>
          <span class="play">${esc(p.play || "")}</span>
        </div>
        <div class="pick-units ${cls}">${esc(unitsLabel)}</div>
      </div>`;
  }).join("\n");
}

/** Build bar-row HTML for chart-card.html's {{bar_rows_html}}.
 *  Each input row: { label, value: display-string, width: 0-100, hilite: bool } */
function chartBarRows(bars) {
  return (bars || []).map(b => {
    const w = Math.max(0, Math.min(100, Number(b.width || 0)));
    const cls = b.hilite ? " hilite" : "";
    return `<div class="bar-row${cls}">
        <div class="bar-label">${esc(b.label || "")}</div>
        <div class="bar-track"><div class="bar-fill" style="--w:${w}%"></div></div>
        <div class="bar-value">${esc(b.value || "")}</div>
      </div>`;
  }).join("\n");
}

/** Build cell HTML for index-card.html's {{index_cells_html}}.
 *  Each input cell: { label, num, foot, tone: "accent"|"win"|"loss"|undefined } */
function indexCells(cells) {
  return (cells || []).map(c => {
    const tone = ["accent","win","loss"].includes(c.tone) ? ` ${c.tone}` : "";
    return `<div class="idx-cell">
        <div class="cell-label">${esc(c.label || "")}</div>
        <div class="cell-num${tone}">${esc(c.num || "")}</div>
        <div class="cell-foot">${esc(c.foot || "")}</div>
      </div>`;
  }).join("\n");
}

// ─────────────────────────────────────────────────────────
// TEMPLATES — one entry per treatment.
//
// Schema:
//   role    — one-sentence "when to use this"
//   fields  — { fieldName: { type, note } } — describes what the
//             caller provides (logical fields, not necessarily the
//             placeholder names used inside the HTML).
//   expand  — optional fn(logicalFields) → placeholderFields.
//             Used by chunked templates to turn arrays-of-rows into
//             the *_html chunks the template expects. If absent, the
//             logical fields ARE the placeholder fields.
//   sample  — a complete, ready-to-render payload. Used as Claude
//             few-shot and for sample renders.
// ─────────────────────────────────────────────────────────

const TEMPLATES = {

  // 01 — EDITORIAL PULL-QUOTE
  "quote-card.html": {
    role: "Editorial pull-quote. A thesis-line or one-liner that needs to breathe.",
    fields: {
      quote_text_html: { type: "html",   note: "Up to ~14 words. Wrap key words in <em>...</em> for gold accent." },
      quote_attrib:    { type: "string", note: "Short mono attribution. e.g. 'SMARTERPICKS · NBA'." },
    },
    sample: {
      size: "feed",
      quote_text_html: "The line moved <em>three points</em> in nine hours. Nobody noticed.",
      quote_attrib: "SMARTERPICKS · NBA",
    },
  },

  // 02 — DAILY PICK BRIEF
  "pick-card.html": {
    role: "Daily pick announcement. Matchup label + huge italic play + 3-cell data row.",
    fields: {
      tag:                { type: "string", note: "Top-right mono tag. e.g. \"TODAY'S FREE PICK\"." },
      matchup:            { type: "string", note: "e.g. 'Lakers @ Celtics'." },
      game_time:          { type: "string", note: "e.g. '7:30 PM ET'." },
      pick_headline_html: { type: "html",   note: "The play, italic-serif. Wrap kw in <em>." },
      line:               { type: "string", note: "e.g. '-4.5'." },
      edge:               { type: "string", note: "e.g. '+6.2%'." },
      confidence:         { type: "string", note: "e.g. 'A-'." },
    },
    sample: {
      size: "feed",
      tag: "TODAY'S FREE PICK",
      matchup: "Lakers @ Celtics",
      game_time: "7:30 PM ET",
      pick_headline_html: "Celtics cover the <em>4.5</em>",
      line: "−4.5",
      edge: "+6.2%",
      confidence: "A−",
    },
  },

  // 03 — HERO STATISTIC
  "stat-card.html": {
    role: "One number, weaponized. Use when a single statistic IS the story.",
    fields: {
      eyebrow:      { type: "string", note: "Mono category line. e.g. 'NBA ROAD FAVS · 2024-25'." },
      stat_value:   { type: "string", note: "The big number. e.g. '72' or '+14.6'." },
      stat_unit:    { type: "string", note: "Unit suffix. e.g. '%', 'u', or '' (empty)." },
      caption_html: { type: "html",   note: "Italic-serif so-what under the number. <em> for accent." },
      source:       { type: "string", note: "Receipt line. e.g. 'n=142 · since Jan 1'." },
    },
    sample: {
      size: "feed",
      eyebrow: "NBA ROAD FAVS · 2024-25",
      stat_value: "72",
      stat_unit: "%",
      caption_html: "of road favorites of <em>−4 or more</em> failed to cover this season.",
      source: "n=142 · since Jan 1",
    },
  },

  // 04 — VERSUS MATCHUP
  "matchup-card.html": {
    role: "Head-to-head comparison. Symmetric grid, 'vs' pivot, the play at the bottom.",
    fields: {
      tag:           { type: "string", note: "Top-right mono tag." },
      team_a:        { type: "string", note: "Left team name." },
      team_a_class:  { type: "string", note: "Empty or 'favored' to accent this side." },
      record_a:      { type: "string", note: "e.g. '24-18 ATS'." },
      stat_a:        { type: "string", note: "Headline numeric. e.g. '115.2'." },
      team_b:        { type: "string", note: "Right team name." },
      team_b_class:  { type: "string", note: "Empty or 'favored'." },
      record_b:      { type: "string", note: "e.g. '27-15 ATS'." },
      stat_b:        { type: "string", note: "Headline numeric." },
      stat_label:    { type: "string", note: "Shared label for the two stats. e.g. 'OFFENSIVE RATING'." },
      the_play:      { type: "string", note: "The pick. Italic serif. e.g. 'Celtics −4.5'." },
    },
    sample: {
      size: "feed",
      tag: "MATCHUP",
      team_a: "Lakers",  team_a_class: "",
      record_a: "24-18 ATS", stat_a: "115.2",
      team_b: "Celtics", team_b_class: "favored",
      record_b: "27-15 ATS", stat_b: "121.4",
      stat_label: "OFFENSIVE RATING",
      the_play: "Celtics −4.5",
    },
  },

  // 05 — BET-SLIP RECEIPT
  "slip-card.html": {
    role: "The pick as a bet slip. Receipt aesthetic — perforated edge, dashed rules.",
    fields: {
      slip_title:    { type: "string", note: "e.g. 'BET SLIP · 05.16.26'." },
      slip_ref:      { type: "string", note: "e.g. 'REF #SP-04217'." },
      the_pick_html: { type: "html",   note: "Italic-serif pick line. <em> for accent." },
      matchup:       { type: "string", note: "e.g. 'LAL @ BOS · 7:30 PM ET'." },
      market:        { type: "string", note: "e.g. 'Spread' or 'Total · Under'." },
      line:          { type: "string", note: "e.g. '−4.5'." },
      odds:          { type: "string", note: "e.g. '−110'." },
      edge:          { type: "string", note: "e.g. '+6.2%'." },
    },
    sample: {
      size: "feed",
      slip_title: "BET SLIP · 05.16.26",
      slip_ref:   "REF #SP-04217",
      the_pick_html: "Celtics −4.5 over <em>Lakers</em>",
      matchup: "LAL @ BOS · 7:30 PM ET",
      market: "Spread",
      line:   "−4.5",
      odds:   "−110",
      edge:   "+6.2%",
    },
  },

  // 06 — RESULTS RECAP
  "recap-card.html": {
    role: "Yesterday's ledger. W–L hero, net units, color-chip pick list.",
    fields: {
      tag:    { type: "string", note: "Top-right mono tag." },
      wins:   { type: "string", note: "Win count as string." },
      losses: { type: "string", note: "Loss count as string." },
      units:  { type: "string", note: "Net units. e.g. '+3.7u'." },
      pick_rows: {
        type: "array", max: 5,
        note:  "List of result rows. Renderer expands → pick_rows_html.",
        shape: { matchup: "string", play: "string", result: "win|loss|push", units: "number" },
      },
    },
    expand: (f) => ({
      ...f,
      pick_rows_html: recapPickRows(f.pick_rows),
    }),
    sample: {
      size: "feed",
      tag: "YESTERDAY'S BOARD",
      wins: "3", losses: "1", units: "+3.7u",
      pick_rows: [
        { matchup: "NBA · LAL @ BOS", play: "Celtics −4.5", result: "win",  units:  1.4 },
        { matchup: "NBA · DEN @ PHX", play: "Under 218.5",   result: "win",  units:  1.0 },
        { matchup: "MLB · NYY @ TOR", play: "Yankees ML",    result: "loss", units: -1.0 },
        { matchup: "NHL · COL @ DAL", play: "Stars +1.5",    result: "win",  units:  1.3 },
      ],
    },
  },

  // 07 — NUMBERED CAROUSEL
  "carousel-card.html": {
    role: "Numbered slide for multi-card threads. Ghosted numeral watermark, body lower-left.",
    fields: {
      step_num:      { type: "string", note: "Numeral for the ghost watermark. e.g. '02'." },
      step_label:    { type: "string", note: "Top-right mono. e.g. 'STEP 02 / 04'." },
      eyebrow:       { type: "string", note: "Category line above headline. e.g. 'LINE SHOPPING'." },
      headline_html: { type: "html",   note: "Slide headline. <em> for accent." },
      body_html:     { type: "html",   note: "Slide body copy. <em>=accent, <strong>=text-color." },
    },
    sample: {
      size: "feed",
      step_num: "02",
      step_label: "STEP 02 / 04",
      eyebrow: "LINE SHOPPING",
      headline_html: "The same bet pays <em>different</em> at every book.",
      body_html: "DraftKings has Celtics −4.5 at <strong>−110</strong>; FanDuel has it at <em>−105</em>. Over a season, the difference compounds into real money.",
    },
  },

  // 08 — CHART VISUALIZATION
  "chart-card.html": {
    role: "Editorial bar chart. Comparison across 4–6 options with one row highlighted.",
    fields: {
      eyebrow:      { type: "string", note: "Mono category line. e.g. 'ATS RECORDS · LAST 10'." },
      title_html:   { type: "html",   note: "Chart title. <em> for accent." },
      caption_html: { type: "html",   note: "Italic-serif so-what. <em> for accent." },
      bars: {
        type: "array", max: 6,
        note:  "List of bar rows. Renderer expands → bar_rows_html.",
        shape: { label: "string", value: "string", width: "number 0-100", hilite: "boolean" },
      },
    },
    expand: (f) => ({
      ...f,
      bar_rows_html: chartBarRows(f.bars),
    }),
    sample: {
      size: "feed",
      eyebrow: "ATS RECORDS · LAST 10",
      title_html: "Where the <em>edge</em> actually lives.",
      caption_html: "Road dogs of <em>+3 to +6</em> are the best closing-line value across the NBA board this month.",
      bars: [
        { label: "Road Dogs +3 to +6",   value: "7-3", width: 70, hilite: true  },
        { label: "Home Favs −4 or more", value: "4-6", width: 40, hilite: false },
        { label: "Totals · Under",       value: "5-5", width: 50, hilite: false },
        { label: "Totals · Over",        value: "6-4", width: 60, hilite: false },
        { label: "Home Dogs +3 to +6",   value: "5-5", width: 50, hilite: false },
      ],
    },
  },

  // 09 — MAGAZINE COVER
  "cover-card.html": {
    role: "Hero / issue-announcement. Full-page italic headline, masthead top.",
    fields: {
      issue:         { type: "string", note: "Mono issue label. e.g. 'ISSUE 042'." },
      date_label:    { type: "string", note: "e.g. 'MAY 16, 2026'." },
      section:       { type: "string", note: "e.g. 'NBA · FEATURE'." },
      headline_html: { type: "html",   note: "Massive italic-serif headline. <em> for accent." },
      deck_html:     { type: "html",   note: "Italic subhead under the rule. <em> for accent." },
    },
    sample: {
      size: "feed",
      issue: "ISSUE 042",
      date_label: "MAY 16, 2026",
      section: "NBA · FEATURE",
      headline_html: "The Lakers have a <em>road problem</em>.",
      deck_html: "Three-and-twelve against the spread on the road since January. The market still won't believe it.",
    },
  },

  // 11 — PHOTO COVER (atmospheric / photo-led)
  // NOTE: this template needs a `photo_url` placeholder filled in before
  // rendering. The Python publisher's photo_fetcher.fetch_photo(query)
  // resolves a topic-relevant photo (via Unsplash if UNSPLASH_ACCESS_KEY
  // is set; Picsum fallback otherwise) and returns local_path + credit.
  // The generator should run that resolver and inject photo_url +
  // photo_credit into the treatment's `fields` before this hits the JS
  // renderer — keeps the JS side dep-free (no fetch, no auth).
  "photo-cover-card.html": {
    role: "Atmospheric magazine-spread cover. Use when a post benefits from a real-world photo (game-day, marquee matchup, season feature).",
    fields: {
      tag:           { type: "string", note: "Top-right mono tag." },
      eyebrow:       { type: "string", note: "Category line. e.g. 'NBA · EASTERN CONFERENCE'." },
      headline_html: { type: "html",   note: "Big italic-serif headline. <em> for accent." },
      deck_html:     { type: "html",   note: "Italic subhead under hairline. <em> for accent." },
      photo_query:   { type: "string", note: "Stock photo search query. Resolver picks topic-relevant editorial shot." },
      photo_url:     { type: "string", note: "Filled by photo_fetcher — local file:// path or CDN URL." },
      photo_credit:  { type: "string", note: "Filled by photo_fetcher — 'Photo: NAME / Unsplash'." },
    },
    sample: {
      size: "feed",
      tag: "TONIGHT'S MARQUEE",
      eyebrow: "NBA · EASTERN CONFERENCE",
      headline_html: "The <em>Celtics</em> reset the East.",
      deck_html: "Sixteen straight at home. The market still has them at <em>−4.5</em>. We don't.",
      photo_query: "basketball arena empty",
      // photo_url + photo_credit get injected by the Python resolver
      // before this payload reaches renderer.js.
    },
  },

  // 12 — LIFESTYLE (aspirational, anchored by receipts)
  // Goal: keep the "you could live like this" energy without breaking
  // the editorial voice. Image is luxury (home / car / watch / villa);
  // copy is always a numeric receipt + a time horizon. The math implies
  // the lifestyle without the brand having to promise outcomes.
  "lifestyle-card.html": {
    role: "Aspirational lifestyle post. Luxury photo + receipt-style headline. The image carries the aspiration; the numbers carry the credibility.",
    fields: {
      tag:           { type: "string", note: "Top-right mono tag. e.g. 'COMPOUND', 'YEAR ONE', 'PATIENCE'." },
      eyebrow:       { type: "string", note: "Mono category line. e.g. 'RECEIPTS · 44 DAYS LOGGED' or 'YTD · 2026'." },
      headline_html: { type: "html",   note: "Italic-serif headline. ALWAYS a numeric receipt. Wrap the number in <em>...</em>. e.g. 'Compound at <em>0.85u</em> a day.', 'Forty-four days. <em>+41.9u</em>.'" },
      sub_html:      { type: "html",   note: "Italic-serif sub-line. A time horizon or a math line. e.g. 'Two-fifty trading days a year. Five years.', 'Math, not magic.'" },
      photo_query:   { type: "string", note: "Luxury imagery query. Rotate: 'luxury home interior modern night', 'vintage porsche 911 classic', 'swiss watch macro detail', 'tuscan villa pool sunset', 'manhattan penthouse view', 'private jet tarmac dusk', 'monaco yacht harbor', 'amalfi coast cliff villa', 'lamborghini huracan', 'rolex submariner closeup'." },
      photo_url:     { type: "string", note: "Filled by photo_fetcher." },
      photo_credit:  { type: "string", note: "Filled by photo_fetcher." },
    },
    sample: {
      size: "feed",
      tag: "COMPOUND",
      eyebrow: "RECEIPTS · 44 DAYS LOGGED",
      headline_html: "Compound at <em>0.85u</em> a day.",
      sub_html: "Two-fifty trading days a year. <em>Five years.</em> Math, not magic.",
      photo_query: "luxury home interior modern night",
    },
  },

  // 10 — DATA INDEX
  "index-card.html": {
    role: "Six-cell stat grid. Season receipts in atomic form.",
    fields: {
      tag:        { type: "string", note: "Top-right mono tag." },
      eyebrow:    { type: "string", note: "Category line. e.g. 'SEASON SCOREBOARD · 2024-25'." },
      title_html: { type: "html",   note: "Section title above the grid. <em> for accent." },
      note_html:  { type: "html",   note: "Italic-serif footnote below the grid. <em> for accent." },
      cells: {
        type: "array", max: 6,
        note:  "Six stat cells. Renderer expands → index_cells_html.",
        shape: { label: "string", num: "string", foot: "string", tone: "accent|win|loss|undefined" },
      },
    },
    expand: (f) => ({
      ...f,
      index_cells_html: indexCells(f.cells),
    }),
    sample: {
      size: "feed",
      tag: "RECEIPTS",
      eyebrow: "SEASON SCOREBOARD · 2024-25",
      title_html: "The <em>verified</em> ledger.",
      note_html: "All picks recorded at posted line, settled at +100 / −110 baseline.",
      cells: [
        { label: "Overall",     num: "187-149", foot: "ATS · units fair",  tone: "win"    },
        { label: "ROI",         num: "+8.4%",   foot: "season-to-date",    tone: "accent" },
        { label: "Net Units",   num: "+38.0u",  foot: "since opening day", tone: "accent" },
        { label: "Sample",      num: "336",     foot: "graded picks"                       },
        { label: "Best Sport",  num: "+22.4u",  foot: "NBA · 71-49 ATS",   tone: "win"    },
        { label: "Longest Run", num: "9-0",     foot: "Apr 02 → Apr 14",   tone: "win"    },
      ],
    },
  },

};

// ─────────────────────────────────────────────────────────
// PUBLIC API
// ─────────────────────────────────────────────────────────

/** List every template filename in the registry, in declaration order. */
function listTreatments() {
  return Object.keys(TEMPLATES);
}

/** Return the spec for a single template, or null if unknown. */
function specFor(template) {
  return TEMPLATES[template] || null;
}

/** Apply a template's expand() function (if any) to a payload of logical
 *  fields, returning the placeholder-shaped object the renderer can
 *  substitute against. Pure: same input → same output. Throws on
 *  unknown templates so the renderer surfaces typos loudly. */
function expandFields(template, fields) {
  const spec = TEMPLATES[template];
  if (!spec) throw new Error(`Unknown template: ${template}`);
  return spec.expand ? spec.expand(fields || {}) : (fields || {});
}

/** Return a deep-cloned sample payload for a template — safe to mutate
 *  in callers' tests without poisoning the registry. */
function sampleFor(template) {
  const spec = TEMPLATES[template];
  if (!spec) return null;
  return JSON.parse(JSON.stringify(spec.sample));
}

module.exports = {
  TEMPLATES,
  listTreatments,
  specFor,
  expandFields,
  sampleFor,
  // Chunk builders — exported for ad-hoc renders that bypass expandFields()
  recapPickRows,
  chartBarRows,
  indexCells,
  // String utilities
  esc,
  escAllowEm,
};
