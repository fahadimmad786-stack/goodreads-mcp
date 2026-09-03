/* Draws the console's charts and cards outside a browser, and prints what
 * came out as JSON for tests/test_webchat.py to assert on.
 *
 * The rest of the suite reads webchat/static/*.js as text, which is enough for
 * "this call site passes a description" but cannot answer "does a bar with a
 * negative value draw at all" -- that is geometry, and only running the code
 * settles it. So this file supplies the smallest DOM the two modules touch:
 * elements with attributes, children, text and a dataset, plus the one
 * selector wireHighlighting() uses. Nothing here lays anything out; the charts
 * compute every coordinate themselves, which is exactly why they can be
 * checked this way.
 *
 * It prints facts, never verdicts. The assertions live in pytest with the
 * rest of them.
 */

class Txt {
  constructor(text) { this.text = String(text); this.children = []; this.attrs = {}; }
  get textContent() { return this.text; }
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.attrs = {};
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.classList = {
      add: (c) => { this.attrs.class = `${this.attrs.class || ''} ${c}`.trim(); },
      remove: () => {},
    };
  }

  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener() {}

  set className(v) { this.attrs.class = String(v); }
  get className() { return this.attrs.class || ''; }
  set textContent(v) { this.children = [new Txt(v)]; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  get firstChild() { return this.children[0] || null; }

  appendChild(n) { this.children.push(n); return n; }
  insertBefore(n, ref) {
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(n); else this.children.splice(at, 0, n);
    return n;
  }

  /* Only what the console asks for: a tag name, a .class, or [attr]. */
  querySelectorAll(sel) {
    const out = [];
    const match = (el) => {
      if (sel.startsWith('[') && sel.endsWith(']')) {
        const name = sel.slice(1, -1);
        if (name.startsWith('data-')) {
          const key = name.slice(5).replace(/-(.)/g, (_, c) => c.toUpperCase());
          return el.dataset[key] !== undefined;
        }
        return el.attrs[name] !== undefined;
      }
      if (sel.startsWith('.')) return this.constructor.classes(el).includes(sel.slice(1));
      return el.tagName === sel;
    };
    const walk = (el) => {
      for (const c of el.children) {
        if (c instanceof El) { if (match(c)) out.push(c); walk(c); }
      }
    };
    walk(this);
    return out;
  }

  static classes(el) { return (el.attrs.class || '').split(/\s+/).filter(Boolean); }
}

globalThis.document = {
  createElement: (t) => new El(t),
  createElementNS: (_ns, t) => new El(t),
  createTextNode: (t) => new Txt(t),
  body: new El('body'),
};

const HERE = new URL('../webchat/static/', import.meta.url);
const charts = await import(new URL('charts.js', HERE));
const cards = await import(new URL('cards.js', HERE));

const cls = (el) => El.classes(el);
const num = (el, a) => Number(el.getAttribute(a));

/* --- what a drawn chart looks like, as numbers --------------------------- */

function readChart(svg, sizeAttr, posAttr) {
  const bars = svg.querySelectorAll('rect')
    .filter((r) => cls(r).includes('bar'))
    .map((r) => ({
      pos: num(r, posAttr),
      size: num(r, sizeAttr),
      end: num(r, posAttr) + num(r, sizeAttr),
    }));
  const zero = svg.querySelectorAll('line').filter((l) => cls(l).includes('zero'));
  const values = svg.querySelectorAll('text')
    .filter((t) => cls(t).includes('value'))
    .map((t) => ({
      text: t.textContent,
      x: num(t, 'x'),
      y: num(t, 'y'),
      anchor: t.getAttribute('text-anchor'),
    }));
  /* A category label as drawn: the text that renders, where its anchor sits,
   * and the <title> a truncated one carries. `textContent` would swallow the
   * title, which is a descriptive element and never rendered, so the two are
   * read apart. */
  const cats = svg.querySelectorAll('text')
    .filter((t) => cls(t).includes('cat'))
    .map((t) => ({
      text: t.children.filter((c) => c instanceof Txt).map((c) => c.textContent).join(''),
      x: num(t, 'x'),
      anchor: t.getAttribute('text-anchor'),
      title: (t.children.find((c) => c instanceof El && c.tagName === 'title') || {}).textContent
        ?? null,
    }));
  return {
    label: svg.getAttribute('aria-label'),
    view_box: svg.getAttribute('viewBox'),
    bars,
    values,
    cats,
    zero_lines: zero.length,
    zero_at: zero.length ? num(zero[0], posAttr === 'x' ? 'x1' : 'y1') : null,
  };
}

/* A series that falls, rises and sits still, so every branch has a row. */
const SIGNED = [
  { cat: 'sank hardest', v: -1.2 },
  { cat: 'sank a little', v: -0.3 },
  { cat: 'unmoved', v: 0 },
  { cat: 'rose', v: 0.6 },
];
const POSITIVE = [
  { cat: 'most', v: 12 },
  { cat: 'fewer', v: 5 },
  { cat: 'fewest', v: 1 },
];
/* Labels around the gutter's old fixed 232 units: the title that arrived
 * clipped, one past any sensible cap, and a short one to prove the gutter is
 * sized to the longest and not to each row. */
const LONG = [
  { cat: 'City of Ashes (The Mortal Instruments #2)', v: 9 },
  { cat: 'Harry Potter and the Order of the Phoenix (Harry Potter #5, Special Edition)', v: 6 },
  { cat: 'Dear John', v: 3 },
];
const SHORT = [
  { cat: 'eng', v: 900 },
  { cat: 'spa', v: 120 },
  { cat: 'fre', v: 80 },
];

const out = { series: { signed: SIGNED, positive: POSITIVE, long: LONG, short: SHORT } };

out.hbars_signed = readChart(
  charts.hbars({ rows: SIGNED, cat: 'cat', value: 'v', unit: 'divergence', desc: 'Bar chart: divergence.' }),
  'width', 'x',
);
out.hbars_positive = readChart(
  charts.hbars({ rows: POSITIVE, cat: 'cat', value: 'v', unit: 'n_books', desc: 'Bar chart: n_books.' }),
  'width', 'x',
);
out.hbars_long = readChart(
  charts.hbars({ rows: LONG, cat: 'cat', value: 'v', unit: 'n_ratings', desc: 'Bar chart: n_ratings.' }),
  'width', 'x',
);
out.hbars_short = readChart(
  charts.hbars({ rows: SHORT, cat: 'cat', value: 'v', unit: 'n_books', desc: 'Bar chart: n_books.' }),
  'width', 'x',
);
out.vbars_signed = readChart(
  charts.vbars({ rows: SIGNED, cat: 'cat', value: 'v', unit: 'divergence', desc: 'Bar chart: divergence.' }),
  'height', 'y',
);
out.vbars_positive = readChart(
  charts.vbars({ rows: POSITIVE, cat: 'cat', value: 'v', unit: 'n_books', desc: 'Bar chart: n_books.' }),
  'height', 'y',
);

/* --- a field carrying two caveats ---------------------------------------- */

const twoCaveats = {
  id: 'call-1',
  tool: 'stats_by_author',
  origin: 'mcp',
  kind: 'ok',
  mcp_ms: 11,
  params: { unit: 'works' },
  envelope: {
    data: [{ authors: 'Ursula K. Le Guin', n_books: 4, pooled_rating: 4.11 }],
    n: { n_groups: 1, pooled_rating: 4.11 },
    filters: { unit: 'works' },
    excluded: {},
    caveats: [
      { id: 'edition_duplication', source: '[DATA_NOTES.md #3]',
        text: 'editions repeat their work ratings.', fields: ['pooled_rating'] },
      { id: 'rating_skew', source: '[measured]',
        text: 'the rating distribution is skewed high.', fields: ['pooled_rating', 'n_groups'] },
      { id: 'title_join', source: '[DATA_NOTES.md #1]',
        text: 'the join covers about half the titles.', fields: ['n_books'] },
    ],
    query_meta: {},
  },
};

const card = cards.renderToolCard(twoCaveats);
const textOf = (sel) => card.querySelectorAll(sel).map((e) => e.textContent);

out.markers = {
  headers: textOf('th'),
  cells: textOf('td'),
  grounds: card.querySelectorAll('dd').map((d) => d.textContent),
  /* The list at the foot of the card: one marker per caveat, same glyph. */
  caveat_marks: card.querySelectorAll('.caveat')
    .map((row) => row.children.find((c) => cls(c).includes('mk')))
    .map((mk) => (mk ? mk.textContent : null)),
  separators: card.querySelectorAll('.sep').map((s) => s.textContent),
};

process.stdout.write(JSON.stringify(out, null, 1));
