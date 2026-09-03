/* Tool cards: every figure on screen is drawn from the tool's own JSON.
 *
 * Three rules this file exists to keep:
 *
 *  1. Nothing is computed. If a share or a percentage is not in the envelope,
 *     it is not shown. Counts are shown as the counts the server sent ("1,507,745
 *     of 1,850,115"), never as a percentage this file worked out -- a derived
 *     figure would be a figure with no caveats attached to it.
 *  2. Caveats attach to fields. A caveat naming `pooled_rating` puts a marker on
 *     that column header and repeats the marker beside its own text, inside the
 *     same card, always expanded. Hovering either end highlights the other.
 *  3. No prose is written here about what a number means. The card states the
 *     figure, its n, its unit, its threshold, its caveats and its cost; the
 *     model's text around the card does the interpreting.
 *
 * Both modes draw from here. A card knows nothing about whether a model chose
 * the tool or a person filled in the form -- it is handed the same frame
 * either way, which is what makes the two modes comparable rather than merely
 * similar.
 */

import { hbars, vbars, lineSeries, fmt, escapeHtml } from './charts.js';

const INTERNAL = new Set(['__flagged', 'band_index', 'bucket_floor']);

/* Unit is a property of the result, not decoration: it decides what one row
 * counts. Both strings come from the server's own documentation of the choice. */
const UNIT_TEXT = {
  editions: 'one row per edition, as stored in the table',
  works: 'editions sharing a normalised title collapsed to one representative row '
       + '(the edition carrying the most ratings)',
};

/* The aria-label for a chart. A chart is role="img", so it is one node to a
 * screen reader and its label has to carry what the card shows visually: the
 * measure, the number of marks, the unit one row counts, the n the figure
 * rests on, and what the threshold removed. Anything less would tell a
 * screen-reader user less than the card tells everyone else. */
function figureDesc(env, lead) {
  const bits = [lead.endsWith('.') ? lead : `${lead}.`];

  const unit = (env.filters || {}).unit;
  if (unit) bits.push(`Unit: ${unit} — ${UNIT_TEXT[unit] || 'as returned'}.`);

  const counts = Object.entries(env.n || {}).map(([k, v]) => `${k} ${fmt(v)}`);
  if (counts.length) bits.push(`Counts: ${counts.join(', ')}.`);

  const excluded = env.excluded || {};
  if (excluded.min_ratings !== undefined) {
    let line = `Threshold min_ratings=${fmt(excluded.min_ratings)}`;
    if (excluded.n_books_below_threshold !== undefined) {
      line += ` excluded ${fmt(excluded.n_books_below_threshold)}`
            + ` of ${fmt(excluded.n_books_in_scope)} books in scope`;
    }
    bits.push(`${line}.`);
  }
  return bits.join(' ');
}

export function renderToolCard(frame) {
  if (frame.kind === 'probe') return renderProbeCard(frame);

  const env = frame.envelope || {};
  const caveats = env.caveats || [];
  const card = node('div', 'card');

  card.appendChild(cardHead(frame));
  card.appendChild(paramsRow(frame.params));

  const markers = new MarkerIndex(caveats);
  const figure = node('div', 'figure');
  /* A wide table or chart scrolls inside the card rather than widening the
   * page, which makes this a scrollable region -- so it has to be reachable
   * and operable from the keyboard (WCAG 2.1.1). */
  figure.tabIndex = 0;
  figure.setAttribute('role', 'group');
  figure.setAttribute('aria-label', `${frame.tool} figures`);
  (FIGURES[frame.tool] || genericFigure)(env, figure, markers, frame);
  card.appendChild(figure);

  card.appendChild(groundsBlock(env, markers));
  if (caveats.length) card.appendChild(caveatBlock(caveats, markers));
  card.appendChild(qmetaRow(env.query_meta || {}, frame.mcp_ms));
  const query = queryDetails(env.query_meta || {});
  if (query) card.appendChild(query);

  wireHighlighting(card);
  return card;
}

/* The card that appears the moment a call starts, so the tool and its
 * parameters are on screen while the query runs. Replaced in place by the
 * result card, matched on `data-call`. */
export function renderPendingCard(frame) {
  const card = node('div', 'card');
  card.dataset.call = frame.id;
  card.dataset.state = 'running';
  card.setAttribute('aria-busy', 'true');
  const head = node('div', 'card-head');
  head.appendChild(originBadge(frame.origin));
  head.appendChild(node('span', 'tool-name', frame.tool));
  head.appendChild(node('span', 'timing', 'running…'));
  card.appendChild(head);
  card.appendChild(paramsRow(frame.params));
  return card;
}

export function renderRefusalCard(frame) {
  const card = node('div', 'card refusal');
  const head = node('div', 'card-head');
  head.appendChild(originBadge(frame.origin));
  head.appendChild(node('span', 'tool-name', frame.tool));
  head.appendChild(node('span', 'verdict', `refused · ${frame.kind.replace('_', ' ')}`));
  if (frame.mcp_ms) head.appendChild(node('span', 'timing', `${ms(frame.mcp_ms)}`));
  card.appendChild(head);
  card.appendChild(paramsRow(frame.params));

  const body = node('div', 'refusal-body');
  body.appendChild(node('div', '', frame.message || 'the call was refused'));
  card.appendChild(body);

  if (frame.caveats && frame.caveats.length) {
    card.appendChild(caveatBlock(frame.caveats, new MarkerIndex(frame.caveats)));
  }
  card.appendChild(node('div', 'qmeta', REFUSAL_COST[frame.kind] || 'no query was executed'));
  return card;
}

const REFUSAL_COST = {
  schema: 'the tool schema rejected the argument before the tool body ran — no query was built, '
        + 'no bigquery bytes billed. the caveats below are the server’s own reasons for the constraint',
  param_error: 'the parameter was rejected before any query was built — no bigquery bytes billed',
  guard: 'the query guard rejected the sql before it reached bigquery — no bytes billed',
  transport: 'the call did not reach the server',
  budget: 'stopped by this console’s per-turn tool-call budget',
};

function renderProbeCard(frame) {
  const p = frame.envelope || {};
  const card = node('div', `card refusal`);
  const head = node('div', 'card-head');
  head.appendChild(originBadge('bff'));
  head.appendChild(node('span', 'tool-name', frame.tool));
  head.appendChild(node('span', 'demo-note', '· demonstration probe, not a data path'));
  head.appendChild(node('span', 'verdict', p.rejected ? 'rejected by guard' : p.verdict.replace(/_/g, ' ')));
  head.appendChild(node('span', 'timing', ms(frame.mcp_ms)));
  card.appendChild(head);
  card.appendChild(paramsRow(frame.params));

  const body = node('div', 'refusal-body');
  body.appendChild(node('div', '', p.message || ''));
  if (p.rule) {
    const rule = node('div', 'guard-rule');
    rule.appendChild(node('span', 'rule', `guard rule: ${p.rule}`));
    if (p.rule_summary) rule.appendChild(document.createTextNode(` — ${p.rule_summary}`));
    body.appendChild(rule);
  }
  if (p.candidate_sql) {
    const sql = node('span', 'sql', p.candidate_sql);
    sql.title = 'built only to be handed to the guard; never executed';
    body.appendChild(sql);
  }
  card.appendChild(body);

  if (p.caveats && p.caveats.length) {
    card.appendChild(caveatBlock(p.caveats, new MarkerIndex(p.caveats)));
  }
  card.appendChild(node('div', 'qmeta',
    'the guard is a pure text check — no bigquery client, no query, no bytes billed'));
  wireHighlighting(card);
  return card;
}

/* --- card furniture ------------------------------------------------------ */

function cardHead(frame) {
  const head = node('div', 'card-head');
  head.appendChild(originBadge(frame.origin));
  head.appendChild(node('span', 'tool-name', frame.tool));
  const qm = (frame.envelope || {}).query_meta || {};
  const timing = qm.bq_ms
    ? `${ms(frame.mcp_ms)} total · ${ms(qm.bq_ms)} in bigquery`
    : ms(frame.mcp_ms);
  head.appendChild(node('span', 'timing', timing));
  return head;
}

function originBadge(origin) {
  const b = node('span', `origin ${origin}`, origin === 'bff' ? 'bff' : 'mcp');
  b.title = origin === 'bff'
    ? 'runs in this console, not on the MCP server'
    : 'a tool of the goodreads-stats MCP server, called over MCP';
  return b;
}

function paramsRow(params) {
  const row = node('div', 'params');
  const keys = Object.keys(params || {});
  if (!keys.length) {
    row.appendChild(node('span', 'none', 'called with no arguments (server defaults apply)'));
    return row;
  }
  keys.forEach((k, i) => {
    if (i) row.appendChild(document.createTextNode(' · '));
    row.appendChild(node('span', 'k', `${k}=`));
    row.appendChild(document.createTextNode(String(params[k])));
  });
  return row;
}

function qmetaRow(qm, mcpMs) {
  const row = node('div', 'qmeta');
  if (!Object.keys(qm).length) {
    row.appendChild(node('span', '', `mcp round trip ${ms(mcpMs)}`));
    return row;
  }
  row.appendChild(node('span', '', `bigquery ${qm.queries} ${qm.queries === 1 ? 'query' : 'queries'}`));
  if (qm.bytes_billed !== undefined) {
    const billed = node('span', '', `${bytes(qm.bytes_billed)} billed`);
    billed.title = `${fmt(qm.bytes_billed)} bytes billed, ${fmt(qm.bytes_processed)} processed`;
    row.appendChild(billed);
  }
  if (qm.cache_hits !== undefined) {
    const hit = qm.cache_hits === qm.queries && qm.queries > 0;
    row.appendChild(node('span', hit ? 'hit' : '',
      `cache ${qm.cache_hits}/${qm.queries} hit`));
  }
  if (qm.bq_ms) row.appendChild(node('span', '', `${ms(qm.bq_ms)} in bigquery`));
  row.appendChild(node('span', '', `${ms(mcpMs)} mcp round trip`));
  return row;
}

/* The query inspector: every statement behind the figure, with the values
 * bound to its named parameters, behind a closed disclosure. `statements`
 * is carried in query_meta by merge_meta(); a server built before it exists
 * sends none, and then there is no disclosure rather than an empty one. */
function queryDetails(qm) {
  const statements = qm.statements || [];
  if (!statements.length) return null;
  const details = node('details', 'query');
  details.appendChild(node('summary', '',
    `query · ${statements.length} ${statements.length === 1 ? 'statement' : 'statements'}`));
  statements.forEach((st, i) => {
    const block = node('div', 'stmt');
    const head = node('div', 'stmt-head');
    head.appendChild(node('span', 'k', `statement ${i + 1}`));
    const keys = Object.keys(st.params || {});
    if (keys.length) {
      head.appendChild(document.createTextNode(' · bound: '));
      keys.forEach((k, j) => {
        if (j) head.appendChild(document.createTextNode(', '));
        head.appendChild(node('span', 'k', `@${k}=`));
        head.appendChild(document.createTextNode(JSON.stringify(st.params[k])));
      });
    } else {
      head.appendChild(document.createTextNode(' · no bound parameters'));
    }
    block.appendChild(head);
    block.appendChild(node('pre', 'sql', st.sql));
    details.appendChild(block);
  });
  return details;
}

/* --- n / unit / threshold ------------------------------------------------ */

function groundsBlock(env, markers) {
  const wrap = node('div', 'grounds');
  const dl = document.createElement('dl');

  const n = env.n || {};
  if (Object.keys(n).length) {
    dl.appendChild(node('dt', '', 'n'));
    dl.appendChild(kvList(n, markers));
  }

  const filters = env.filters || {};
  const unit = filters.unit;
  if (unit) {
    dl.appendChild(node('dt', '', 'unit'));
    const dd = node('dd', '');
    dd.appendChild(node('b', '', unit));
    dd.appendChild(document.createTextNode(` — ${UNIT_TEXT[unit] || ''}`));
    markers.decorate(dd, 'unit');
    dl.appendChild(dd);
  }

  const excluded = env.excluded || {};
  if (excluded.min_ratings !== undefined) {
    dl.appendChild(node('dt', '', 'threshold'));
    dl.appendChild(thresholdDd(excluded, markers));
  }

  const shown = Object.entries(filters).filter(([k]) => !['unit', 'min_ratings'].includes(k));
  if (shown.length) {
    dl.appendChild(node('dt', '', 'filters'));
    dl.appendChild(kvList(Object.fromEntries(shown), markers));
  }

  const otherExcluded = Object.entries(excluded).filter(
    ([k]) => !['min_ratings', 'n_books_in_scope', 'n_books_below_threshold', 'note'].includes(k),
  );
  if (otherExcluded.length) {
    dl.appendChild(node('dt', '', 'excluded'));
    dl.appendChild(kvList(Object.fromEntries(otherExcluded), markers));
  }

  if (excluded.note) {
    dl.appendChild(node('dt', '', 'note'));
    dl.appendChild(node('dd', '', excluded.note));
  }

  wrap.appendChild(dl);
  return wrap;
}

function thresholdDd(excluded, markers) {
  /* Counts exactly as sent. No percentage is computed here. */
  const dd = node('dd', '');
  dd.appendChild(node('span', 'k', 'min_ratings='));
  dd.appendChild(node('b', '', fmt(excluded.min_ratings)));
  markers.decorate(dd, 'min_ratings');
  if (excluded.n_books_below_threshold !== undefined) {
    dd.appendChild(document.createTextNode(' excluded '));
    dd.appendChild(node('b', '', fmt(excluded.n_books_below_threshold)));
    dd.appendChild(document.createTextNode(' of '));
    dd.appendChild(node('b', '', fmt(excluded.n_books_in_scope)));
    dd.appendChild(document.createTextNode(' books in scope'));
  }
  return dd;
}

function kvList(obj, markers) {
  const dd = node('dd', '');
  Object.entries(obj).forEach(([k, v], i) => {
    if (i) dd.appendChild(document.createTextNode(' · '));
    const span = node('span', '');
    span.appendChild(node('span', 'k', `${k} `));
    span.appendChild(node('b', '', typeof v === 'object' ? JSON.stringify(v) : fmt(v)));
    markers.decorate(span, k);
    dd.appendChild(span);
  });
  return dd;
}

/* --- caveats ------------------------------------------------------------- */

/* Between two markers on one field. A comma, because a space alone still
 * reads as a pair of digits at this size. */
const MARKER_SEP = ',';

class MarkerIndex {
  constructor(caveats) {
    this.byField = new Map();
    this.marks = new Map();
    caveats.forEach((c, i) => {
      const mark = String(i + 1);
      this.marks.set(i, mark);
      for (const f of c.fields || []) {
        if (!this.byField.has(f)) this.byField.set(f, []);
        this.byField.get(f).push({ index: i, mark, caveat: c });
      }
    });
  }

  /* Append markers for `field` to `target`, if any caveat claims that field.
   *
   * Two markers set side by side read as one number -- a field carrying
   * caveats 2 and 3 would say "caveat 23", which is a different caveat and,
   * once the registry passes ten, an existing one. So consecutive markers are
   * separated by a superscript comma. It is written here rather than at each
   * call site so headers, cells and grounds values all separate the same way,
   * and the caveat list below them shows the same marker glyph. */
  decorate(target, field) {
    const claims = this.byField.get(field) || [];
    claims.forEach((m, i) => {
      if (i) target.appendChild(node('span', 'mk sep', MARKER_SEP));
      const sup = node('span', 'mk', m.mark);
      sup.dataset.cv = String(m.index);
      sup.title = `${m.caveat.source}: ${m.caveat.text.slice(0, 180)}…`;
      target.appendChild(sup);
      target.dataset.cv = target.dataset.cv
        ? `${target.dataset.cv} ${m.index}`
        : String(m.index);
    });
  }

  has(field) { return this.byField.has(field); }
}

function caveatBlock(caveats, markers) {
  const wrap = node('div', 'caveats');
  wrap.appendChild(node('h4', '', `caveats (${caveats.length}) — attached to the figures they qualify`));
  caveats.forEach((c, i) => {
    const row = node('div', 'caveat');
    row.dataset.cv = String(i);
    row.appendChild(node('span', 'mk', markers.marks.get(i) || '·'));
    const body = node('div', '');
    body.appendChild(node('span', 'src', c.source || 'unattributed'));
    body.appendChild(node('span', 'text', c.text));
    if (c.fields && c.fields.length) {
      body.appendChild(node('span', 'applies', `applies to: ${c.fields.join(', ')}`));
    } else {
      body.appendChild(node('span', 'applies', 'applies to this result as a whole'));
    }
    row.appendChild(body);
    wrap.appendChild(row);
  });
  return wrap;
}

function wireHighlighting(card) {
  const targets = card.querySelectorAll('[data-cv]');
  targets.forEach((t) => {
    const ids = t.dataset.cv.split(' ');
    const partners = () => Array.from(card.querySelectorAll('[data-cv]'))
      .filter((o) => o.dataset.cv.split(' ').some((id) => ids.includes(id)));
    t.addEventListener('mouseenter', () => partners().forEach((p) => p.classList.add('hl')));
    t.addEventListener('mouseleave', () => partners().forEach((p) => p.classList.remove('hl')));
  });
}

/* --- tables -------------------------------------------------------------- */

function autoTable(rows, markers, opts = {}) {
  const table = document.createElement('table');
  const cols = (opts.cols || Object.keys(rows[0] || {})).filter((c) => !INTERNAL.has(c));

  /* Named for a screen reader, which meets the table with no surrounding
   * layout to explain it. Hidden visually because the figure label above
   * already says the same thing on screen. */
  const caption = node('caption', 'sr-only',
    opts.caption || `${rows.length} ${rows.length === 1 ? 'row' : 'rows'}: `
      + `${cols.join(', ')}`);
  table.appendChild(caption);

  const thead = document.createElement('thead');
  const hr = document.createElement('tr');
  cols.forEach((c) => {
    const numeric = typeof rows[0][c] === 'number';
    const th = node('th', numeric ? 'num' : '');
    th.setAttribute('scope', 'col');
    th.appendChild(document.createTextNode(c));
    markers.decorate(th, c);
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    if (r.__flagged) tr.className = 'flagged';
    cols.forEach((c) => {
      const numeric = typeof r[c] === 'number';
      const td = node('td', numeric ? 'num' : 'name');
      td.appendChild(document.createTextNode(fmt(r[c])));
      markers.decorate(td, c);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

function label(text) { return node('div', 'figure-label', text); }

function part(target, text, element) {
  if (text) target.appendChild(label(text));
  if (element) target.appendChild(element);
}

/* The query succeeded and matched nothing. That is an answer, so it is stated
 * in the figure area rather than left as an empty panel. */
function empty(target, text) {
  target.appendChild(node('div', 'empty', text));
}

/* --- per-tool figures ---------------------------------------------------- */

function rankFigure(catKey) {
  return (env, out, markers, frame) => {
    const rows = env.data || [];
    if (!rows.length) return empty(out, 'no groups matched this threshold — nothing to rank');
    const measure = (frame.params && frame.params.order_by) || pickMeasure(rows[0]);
    const lead = `${measure} by ${catKey} — ${rows.length} groups, ordered as returned`;
    part(out, lead, hbars({
      rows, cat: catKey, value: measure, unit: measure,
      extra: ['n_books', 'n_ratings', 'avg_book_rating', 'pooled_rating', 'editions_per_title'],
      desc: figureDesc(env, `Bar chart: ${lead}`),
    }));
    part(out, null, autoTable(rows, markers));
  };
}

function pickMeasure(row) {
  for (const c of ['n_ratings', 'n_books', 'pooled_rating', 'avg_book_rating']) {
    if (typeof row[c] === 'number') return c;
  }
  return Object.keys(row).find((k) => typeof row[k] === 'number');
}

function yearFigure(env, out, markers) {
  const rows = env.data || [];
  if (!rows.length) return empty(out, 'no years matched these filters');
  /* Two measures, two charts. Never one chart with two y-axes. */
  part(out, 'n_books per publish_year',
    lineSeries({ rows, x: 'publish_year', y: 'n_books', unit: 'n_books',
      extra: ['n_ratings', 'avg_book_rating', 'pooled_rating'],
      desc: figureDesc(env, `Line chart: n_books per publish_year, ${rows.length} years`) }));
  part(out, 'avg_book_rating per publish_year',
    lineSeries({ rows, x: 'publish_year', y: 'avg_book_rating', unit: 'avg_book_rating',
      extra: ['pooled_rating', 'n_books'],
      desc: figureDesc(env, `Line chart: avg_book_rating per publish_year, ${rows.length} years`) }));
  part(out, null, autoTable(rows, markers));
}

function distFigure(env, out, markers) {
  const d = env.data || {};
  const hist = d.histogram || [];
  if (hist.length) {
    part(out, 'n_books per rating bucket',
      vbars({ rows: hist, cat: 'bucket', value: 'n_books', unit: 'n_books',
        extra: ['n_ratings', 'pct_of_books'], rotate: true,
        desc: figureDesc(env, `Bar chart: n_books per rating bucket, ${hist.length} buckets`) }));
  }
  if (d.star_share_pct) {
    const rows = Object.entries(d.star_share_pct).map(([k, v]) => ({ star: k, pct_of_ratings: v }));
    part(out, 'pct_of_ratings per star (pooled across every rating in scope)',
      vbars({ rows, cat: 'star', value: 'pct_of_ratings', unit: 'pct_of_ratings',
        desc: figureDesc(env, 'Bar chart: pct_of_ratings per star, pooled across every rating in scope') }));
  }
  if (d.summary) part(out, 'summary', autoTable([d.summary], markers));
  if (hist.length) part(out, null, autoTable(hist, markers));
}

function monthFigure(env, out, markers) {
  const rows = (env.data || []).map((r) => ({ ...r, __flagged: !!r.placeholder_inflated }));
  if (!rows.length) return empty(out, 'no months returned for this range');
  part(out, 'n_books_published per publish_month — january carries the placeholder dates',
    vbars({ rows, cat: 'month', value: 'n_books_published', unit: 'n_books_published',
      extra: ['pct_of_books', 'avg_book_rating', 'pooled_rating'],
      desc: figureDesc(env, 'Bar chart: n_books_published per publish_month, 12 months. '
        + 'January is inflated by placeholder dates and is flagged in the table') }));
  part(out, null, autoTable(rows, markers));
}

function pageFigure(env, out, markers) {
  const d = env.data || {};
  const bands = d.by_band || [];
  if (bands.length) {
    part(out, 'avg_book_rating per pages_band',
      vbars({ rows: bands, cat: 'pages_band', value: 'avg_book_rating', unit: 'avg_book_rating',
        extra: ['n_books', 'n_ratings', 'pooled_rating', 'avg_pages'], rotate: true,
        desc: figureDesc(env, `Bar chart: avg_book_rating per pages_band, ${bands.length} bands`) }));
    part(out, null, autoTable(bands, markers));
  }
  if (d.page_count_quartiles) {
    part(out, 'page_count_quartiles', autoTable([d.page_count_quartiles], markers));
  }
}

function userFigure(env, out, markers) {
  const d = env.data || {};
  if (d.star_distribution) {
    part(out, 'n_ratings per star, from the 4,154-user panel',
      vbars({ rows: d.star_distribution, cat: 'rating', value: 'n_ratings', unit: 'n_ratings',
        extra: ['rating_label', 'pct_of_ratings'],
        desc: figureDesc(env, 'Bar chart: n_ratings per star, from the 4,154-user panel') }));
    part(out, null, autoTable(d.star_distribution, markers));
  }
  if (d.summary) part(out, 'summary', autoTable([d.summary], markers));
}

function compareFigure(env, out, markers) {
  const rows = env.data || [];
  if (!rows.length) return empty(out, 'no titles matched in both tables — the join covers about half of them');
  part(out, 'divergence per title — panel average minus goodreads pooled rating',
    hbars({ rows, cat: 'example_raw_title', value: 'divergence', unit: 'divergence',
      extra: ['user_avg_rating', 'book_pooled_rating', 'user_n_ratings', 'book_n_ratings', 'n_editions'],
      desc: figureDesc(env, `Bar chart: divergence per title, ${rows.length} titles — `
        + 'the user panel average minus the goodreads pooled rating') }));
  part(out, null, autoTable(rows, markers));
}

function overviewFigure(env, out, markers) {
  const d = env.data || {};
  for (const [key, value] of Object.entries(d)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const flat = {};
      for (const [k, v] of Object.entries(value)) {
        flat[k] = v && typeof v === 'object' ? JSON.stringify(v) : v;
      }
      part(out, key, autoTable([flat], markers));
    }
  }
}

function genericFigure(env, out, markers) {
  const d = env.data;
  if (Array.isArray(d) && d.length) return part(out, `${d.length} rows`, autoTable(d, markers));
  if (Array.isArray(d)) return empty(out, 'the query succeeded and matched no rows');
  if (d && typeof d === 'object') return overviewFigure(env, out, markers);
  empty(out, 'this tool returned no data');
}

const FIGURES = {
  stats_by_author: rankFigure('authors'),
  stats_by_publisher: rankFigure('publisher'),
  stats_by_language: rankFigure('language_normalised'),
  stats_by_year: yearFigure,
  rating_distribution: distFigure,
  publish_month_seasonality: monthFigure,
  page_count_stats: pageFigure,
  user_ratings_overview: userFigure,
  compare_user_vs_book_ratings: compareFigure,
  dataset_overview: overviewFigure,
  top_books_by_rating: (env, out, markers) => {
    const rows = env.data || [];
    if (!rows.length) return empty(out, 'no books cleared the min_ratings threshold');
    part(out, `rating per book — ${rows.length} rows, ordered as returned`,
      hbars({ rows, cat: 'name', value: 'rating', unit: 'rating',
        extra: ['authors', 'n_ratings', 'publish_year', 'n_editions'],
        desc: figureDesc(env, `Bar chart: rating per book, ${rows.length} books, ordered as returned`) }));
    part(out, null, autoTable(rows, markers));
  },
  top_titles_by_user_ratings: (env, out, markers) => {
    const rows = env.data || [];
    if (!rows.length) return empty(out, 'no titles cleared the threshold in the user panel');
    part(out, 'avg_user_rating per title — the 4,154-user panel only',
      hbars({ rows, cat: 'example_raw_title', value: 'avg_user_rating', unit: 'avg_user_rating',
        extra: ['n_user_ratings', 'n_users'],
        desc: figureDesc(env, `Bar chart: avg_user_rating per title, ${rows.length} titles, `
          + 'from the 4,154-user panel only') }));
    part(out, null, autoTable(rows, markers));
  },
};

/* --- tiny dom helper ----------------------------------------------------- */

function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function ms(v) {
  if (!v) return '—';
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

function bytes(b) {
  if (b === null || b === undefined) return '—';
  if (b === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), units.length - 1);
  return `${(b / 1024 ** i).toFixed(i ? 2 : 0)} ${units[i]}`;
}

export { escapeHtml, node, ms, bytes };
