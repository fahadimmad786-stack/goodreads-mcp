/* The Defects view: dataset_overview, laid out so the defects cannot be missed.
 *
 * Everything on this view is drawn from one envelope -- the same
 * dataset_overview call the Overview's tiles use -- and nothing is computed
 * here. A share is shown only where the server sent one; a count is shown as
 * the count the server sent. The three headline tiles are the three defects
 * that most change what a figure means: a quarter of the table unrated, the
 * edition overcount of every summed rating total, and the placeholder day.
 *
 * The caveat rows are the server's registry, in the server's order, with the
 * live figures that measure each one placed beside its prose. LIVE maps a
 * caveat id to the envelope paths that quantify it; a test checks every leaf
 * name against the server's own source, so a renamed field fails loudly
 * rather than rendering a dash.
 */

import { overview } from './data.js';
import { node } from './cards.js';
import { hbars, fmt } from './charts.js';

const box = document.getElementById('defects');

/* caveat id -> envelope paths under `data` that quantify it. */
const LIVE = {
  unrated_books: ['books.n_books_unrated', 'books.coverage_pct.unrated', 'books.n_books'],
  language_coverage: [
    'books.n_language_normalised', 'books.coverage_pct.language_normalised',
    'books.n_distinct_languages',
  ],
  edition_duplication: [
    'edition_duplication.overcount_factor', 'edition_duplication.n_edition_rows',
    'edition_duplication.n_distinct_titles', 'edition_duplication.n_ratings_summed_over_editions',
    'edition_duplication.n_ratings_deduplicated_by_title',
  ],
  work_dedup: [
    'edition_duplication.n_titles_multi_edition',
    'edition_duplication.n_titles_with_differing_edition_totals',
  ],
  title_editions: ['edition_duplication.max_editions_for_one_title'],
  rating_skew: [
    'books.median_ratings_per_book', 'books.p90_ratings_per_book', 'books.max_ratings_per_book',
    'books.n_books_100plus',
  ],
  publisher_unnormalised: ['books.n_publisher', 'books.n_distinct_publishers'],
  authors_freetext: ['books.n_distinct_author_strings'],
  pages_nulled: ['books.n_pages_number', 'books.coverage_pct.pages_number'],
  publish_year_reliable: ['books.n_publish_year', 'books.min_publish_year', 'books.max_publish_year'],
  title_join: ['join.matched_titles', 'join.rated_titles', 'join.coverage_pct'],
  tables_independent: [
    'user_ratings.n_user_ratings', 'user_ratings.n_users', 'user_ratings.n_distinct_titles',
    'user_ratings.avg_user_rating',
  ],
  text_reviews_sparse: ['books.n_count_of_text_reviews', 'books.coverage_pct.count_of_text_reviews'],
  description_sparse: ['books.n_description', 'books.coverage_pct.description'],
  publish_day_unusable: [
    'publish_day_placeholder.n_rows', 'publish_day_placeholder.pct_of_rows',
    'publish_day_placeholder.n_rows_total',
  ],
};

/* The coverage block, one bar per column: which columns can be read at all. */
const COVERAGE_LABEL = 'pct_of_rows';

let started = false;

export function initDefects() {
  if (started) return;
  started = true;
  box.replaceChildren(node('div', 'empty', 'fetching dataset_overview…'));
  overview().then(render).catch((err) => {
    box.replaceChildren(node('div', 'notice', err.message || 'could not reach the console backend'));
  });
}

function render(frame) {
  box.replaceChildren();
  if (frame.type !== 'tool_result') {
    box.appendChild(node('div', 'notice', frame.message || 'dataset_overview was refused'));
    return;
  }
  const env = frame.envelope || {};
  const d = env.data || {};
  const caveats = env.caveats || [];

  const lede = node('header', 'lede');
  lede.appendChild(node('h2', '', 'Known defects, with their live size.'));
  lede.appendChild(node('p', '',
    'What dataset_overview measures on every call, laid out by defect. Each row below is '
    + 'one caveat from the server’s registry, with the figures that quantify it and the '
    + 'columns it weakens. The three at the top change what almost every figure means.'));
  box.appendChild(lede);

  box.appendChild(heroTiles(d, caveats));
  box.appendChild(coverageSection(d, env));
  box.appendChild(duplicationSection(d));
  box.appendChild(caveatSection(d, caveats));
  box.appendChild(costLine(env.query_meta || {}, frame.mcp_ms));
}

/* --- the three headline defects ----------------------------------------- */

function heroTiles(d, caveats) {
  const tiles = node('div', 'tiles');
  const books = d.books || {};
  const cov = books.coverage_pct || {};
  const dup = d.edition_duplication || {};
  const day = d.publish_day_placeholder;

  tiles.appendChild(heroTile({
    value: fmt(books.n_books_unrated),
    label: 'unrated editions',
    of: [
      ['of', books.n_books, 'editions'],
      cov.unrated !== undefined ? [null, `${fmt(cov.unrated)}%`, 'of the table'] : null,
      [null, null, 'stored as rating = 0.0; excluded by the min_ratings floor, never averaged'],
    ],
  }));

  tiles.appendChild(heroTile({
    value: dup.overcount_factor !== undefined && dup.overcount_factor !== null
      ? `${fmt(dup.overcount_factor)}×` : '—',
    label: 'edition overcount of n_ratings',
    of: [
      [null, dup.n_ratings_summed_over_editions, 'summed over edition rows'],
      ['vs', dup.n_ratings_deduplicated_by_title, 'deduplicated by title'],
      [null, null, dup.scope ? `scope: ${dup.scope}` : ''],
    ],
  }));

  if (day && day.pct_of_rows !== undefined) {
    tiles.appendChild(heroTile({
      value: `${fmt(day.pct_of_rows)}%`,
      label: 'publish_day placeholder rows',
      of: [
        [null, day.n_rows, `of ${fmt(day.n_rows_total)} rows`],
        [null, null, day.measured || ''],
      ],
    }));
  } else {
    /* The deployed server predates the structured block: the figure is in
     * the caveat prose, so point there rather than parse it out. */
    const caveat = caveats.find((c) => c.id === 'publish_day_unusable');
    tiles.appendChild(heroTile({
      value: '—',
      label: 'publish_day placeholder rows',
      of: [[null, null, caveat
        ? 'share stated in the caveat below; the deployed server does not yet send it as a field'
        : 'not reported by this server']],
    }));
  }
  return tiles;
}

function heroTile({ value, label, of }) {
  const tile = node('div', 'tile hero');
  tile.appendChild(node('div', 'value flag', value));
  tile.appendChild(node('div', 'label', label));
  for (const line of of) {
    if (!line) continue;
    const [lead, figure, tail] = line;
    if (figure === null && !tail) continue;
    const p = node('div', 'of');
    if (lead) p.appendChild(document.createTextNode(`${lead} `));
    if (figure !== null && figure !== undefined) {
      p.appendChild(node('b', '', typeof figure === 'string' ? figure : fmt(figure)));
      p.appendChild(document.createTextNode(' '));
    }
    if (tail) p.appendChild(document.createTextNode(tail));
    tile.appendChild(p);
  }
  return tile;
}

/* --- column coverage ----------------------------------------------------- */

function coverageSection(d, env) {
  const section = node('section', 'section');
  section.appendChild(sectionHead('column coverage',
    'share of rows where the column is populated, as dataset_overview reports it'));
  const cov = (d.books || {}).coverage_pct || {};
  const rows = Object.entries(cov)
    .filter(([k]) => k !== 'unrated')
    .map(([column, pct]) => ({ column, [COVERAGE_LABEL]: pct }));
  if (!rows.length) {
    section.appendChild(node('div', 'empty', 'no coverage figures in this envelope'));
    return section;
  }
  const panel = node('div', 'panel');
  const n = (env.n || {}).n_books;
  panel.appendChild(hbars({
    rows, cat: 'column', value: COVERAGE_LABEL, unit: COVERAGE_LABEL,
    desc: `Bar chart: ${COVERAGE_LABEL} per column, ${rows.length} columns. `
      + `Counts: n_books ${fmt(n)}. Unit: editions — one row per edition, as stored in the table.`,
  }));
  section.appendChild(panel);
  return section;
}

/* --- edition duplication, in full ---------------------------------------- */

function duplicationSection(d) {
  const section = node('section', 'section');
  section.appendChild(sectionHead('edition duplication',
    'a row in books is an edition, and each edition repeats most of its work’s rating pool'));
  const dup = d.edition_duplication || {};
  const figures = node('div', 'figures');
  for (const [k, v] of Object.entries(dup)) {
    if (typeof v === 'string') continue;
    figures.appendChild(stat(k, v));
  }
  const panel = node('div', 'panel');
  panel.appendChild(figures);
  if (dup.scope) panel.appendChild(node('div', 'footnote', `scope: ${dup.scope}`));
  section.appendChild(panel);
  return section;
}

/* --- every caveat, with what measures it --------------------------------- */

function caveatSection(d, caveats) {
  const section = node('section', 'section');
  section.appendChild(sectionHead(`every caveat (${caveats.length})`,
    'the server’s registry, in its order; [measured] marks a defect DATA_NOTES.md does not mention'));
  const panel = node('div', 'panel');
  caveats.forEach((c) => {
    const row = node('div', 'defect');
    const id = node('div', 'id');
    const src = node('span', `src pill${c.source === 'measured' ? ' measured' : ''}`, c.source || 'unattributed');
    id.appendChild(src);
    id.appendChild(node('span', 'name', c.id || '·'));
    row.appendChild(id);

    const body = node('div', '');
    const live = (LIVE[c.id] || []).map((path) => [leaf(path), dig(d, path)]).filter(([, v]) => v !== undefined);
    if (live.length) {
      const figures = node('div', 'figures');
      for (const [k, v] of live) figures.appendChild(stat(k, v));
      body.appendChild(figures);
    }
    body.appendChild(node('p', 'text', c.text));
    if (c.fields && c.fields.length) {
      const chips = node('div', 'chips');
      chips.appendChild(node('span', 'label', 'weakens'));
      for (const f of c.fields) chips.appendChild(node('span', 'chip', f));
      body.appendChild(chips);
    }
    row.appendChild(body);
    panel.appendChild(row);
  });
  section.appendChild(panel);
  return section;
}

/* --- small parts --------------------------------------------------------- */

function sectionHead(title, note) {
  const head = node('div', 'section-head');
  head.appendChild(node('span', '', title));
  if (note) head.appendChild(node('span', 'note', note));
  return head;
}

function stat(key, value) {
  const s = node('span', 'stat');
  s.appendChild(document.createTextNode(`${key} `));
  s.appendChild(node('b', '', fmt(value)));
  return s;
}

function costLine(qm, mcpMs) {
  const line = node('div', 'contract');
  const bits = ['dataset_overview'];
  if (qm.queries) bits.push(`${qm.queries} bigquery queries`, `cache ${qm.cache_hits ?? 0}/${qm.queries} hit`);
  if (qm.bq_ms) bits.push(`${Math.round(qm.bq_ms)} ms in bigquery`);
  if (mcpMs) bits.push(`${Math.round(mcpMs)} ms mcp round trip`);
  line.textContent = bits.join(' · ');
  return line;
}

function dig(obj, path) {
  return path.split('.').reduce((o, k) => (o && typeof o === 'object' ? o[k] : undefined), obj);
}

function leaf(path) { return path.slice(path.lastIndexOf('.') + 1); }
