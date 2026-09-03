/* Inline SVG charts, hand-rolled.
 *
 * No chart library: every mark here has to carry its own exact value, because
 * nothing in this console may present a rounded number. A library would also
 * import its own aesthetic, and there are only three forms to draw.
 *
 * One series per chart, so one accent colour and no legend -- the figure label
 * names the measure. Two measures never share an axis; the caller draws two
 * charts instead.
 *
 * Every chart is role="img" with an aria-label the caller supplies: the
 * measure, the category, how many marks, and -- crucially -- the n the figure
 * rests on and the unit one row counts. A chart whose description omitted its
 * n would tell a screen-reader user less than the card tells everyone else,
 * which would break this console's one promise. `desc` comes from cards.js,
 * which holds the envelope; charts.js only draws.
 */

const NS = 'http://www.w3.org/2000/svg';
const exact = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

export function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return exact.format(v);
  return String(v);
}

function el(name, attrs = {}, text) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgRoot(w, h, minWidth = 620) {
  const s = el('svg', {
    class: 'chart',
    viewBox: `0 0 ${w} ${h}`,
    role: 'img',
  });
  s.style.minWidth = `${minWidth}px`;
  return s;
}

/* role="img" makes the SVG a single node to assistive tech, so the label has
 * to carry the whole figure. The <title> child is what a pointer tooltip
 * shows; both say the same thing. */
function describe(svg, text) {
  svg.setAttribute('aria-label', text);
  const title = el('title', {}, text);
  svg.insertBefore(title, svg.firstChild);
  return svg;
}

/* --- one shared tooltip -------------------------------------------------- */

let tip;
function tooltip() {
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'tip';
    document.body.appendChild(tip);
  }
  return tip;
}

function showTip(evt, html) {
  const t = tooltip();
  t.innerHTML = html;
  t.classList.add('on');
  const pad = 12;
  const rect = t.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
}

function hideTip() {
  if (tip) tip.classList.remove('on');
}

function attachTip(node, html) {
  node.addEventListener('mousemove', (e) => showTip(e, html));
  node.addEventListener('mouseleave', hideTip);
}

function truncate(text, max) {
  const s = String(text ?? '');
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/* --- a scale that can hold negative values --------------------------------
 *
 * A bar chart drawn from `v / max` renders nothing at all for a negative
 * value: the width is negative, so the rect collapses and the row reads as
 * missing rather than as a fall. Scaling |v| instead would be worse -- a drop
 * would draw the same bar as a rise of the same size.
 *
 * So a series with any negative value gets a zero line and a domain that is
 * symmetric about it, [-M, +M] for M the largest absolute value. Bars run one
 * way for positive and the other for negative, and equal magnitudes draw
 * equal bars whichever side they fall. An all-positive series keeps the plain
 * scale anchored at the edge: there is nothing to divide it around, and
 * halving the plot for an empty negative side would only shrink every bar.
 */
function signedScale(values) {
  const nums = values.map((v) => Number(v) || 0);
  return {
    signed: nums.some((v) => v < 0),
    max: Math.max(...nums.map(Math.abs), 0) || 1,
  };
}

const SIGNED_NOTE = ' Values are signed: the bars run from a zero line, '
  + 'negative one way and positive the other, scaled symmetrically to the '
  + 'largest absolute value.';

/* --- the category gutter -------------------------------------------------
 *
 * An SVG clips at its viewBox, so a label wider than the gutter loses its
 * first characters rather than overflowing: `City of Ashes (The Mortal
 * Instruments #2)` arrived as `ity of Ashes (The Mortal Instrum…`, which
 * reads as a different book. A fixed gutter cannot be right for both a
 * language code and a boxed-set title, so it is measured from the labels the
 * chart actually has.
 *
 * Measured arithmetically, not by asking the browser: these labels are set in
 * the mono, where every character is one advance wide, and the chart is built
 * off-DOM so there is nothing to call getComputedTextLength on yet. The
 * advance is rounded up from the measured one, which makes every estimate an
 * over-estimate -- a gutter a hair too wide costs nothing, a gutter a hair
 * too narrow is the bug again.
 *
 * The cap is where a title stops paying for itself: past it the chart is
 * mostly text and the bars have nowhere left to go, so the label truncates at
 * an ellipsis and carries its full text in a <title> for hover. The floor
 * keeps a chart of three-letter language codes from looking like a different
 * component.
 */
const CAT_SIZE = 11.5;       /* --t-meta; a test pins this to the stylesheet */
const CAT_ADVANCE = 0.63;    /* JetBrains Mono measures 0.609em here, rounded up */
const CAT_PAD = 10;          /* the gap the label keeps from the bars */
const CAT_MIN = 140;
const CAT_MAX = 340;
const CAT_CHAR = CAT_SIZE * CAT_ADVANCE;

function catGutter(labels) {
  const widest = Math.max(...labels.map((s) => s.length * CAT_CHAR), 0);
  const gutter = Math.ceil(Math.min(Math.max(widest + CAT_PAD, CAT_MIN), CAT_MAX));
  /* What fits in the gutter it settled on -- the cap's truncation length,
   * derived from the same arithmetic rather than guessed alongside it. */
  return { gutter, fits: Math.floor((gutter - CAT_PAD) / CAT_CHAR) };
}

/* --- horizontal bars: rankings ------------------------------------------
 * No value axis and no ticks. Each bar is labelled with its exact value, so
 * there is nothing to read off a scale and nothing rounded on screen.
 */
export function hbars({ rows, cat, value, unit, extra, desc }) {
  const W = 900;
  const rowH = 20;
  const gap = 2;               /* surface gap between adjacent bars */
  const labels = rows.map((r) => String(r[cat] ?? ''));
  const { gutter, fits } = catGutter(labels);
  const rightPad = 132;
  const labelPad = 84;         /* room for the label at a leftward bar's end */
  const H = rows.length * (rowH + gap) + 8;
  const { signed, max } = signedScale(rows.map((r) => r[value]));
  /* Signed: the label of the longest negative bar has to land clear of the
   * category names, so the plot starts a label's width in from the gutter. */
  const left = gutter + (signed ? labelPad : 0);
  const plot = W - rightPad - left;
  const reach = signed ? plot / 2 : plot;   /* the longest a bar can draw */
  const zero = signed ? left + reach : left;

  const svg = svgRoot(W, H);
  if (signed) {
    svg.appendChild(el('line', { x1: zero, y1: 0, x2: zero, y2: H, class: 'axis zero' }));
  }

  rows.forEach((r, i) => {
    const y = i * (rowH + gap) + 4;
    const v = Number(r[value]) || 0;
    const w = Math.max((Math.abs(v) / max) * reach, v !== 0 ? 2 : 0);
    const x = v < 0 ? zero - w : zero;

    /* Truncated only where the cap bit. The <title> is a pointer tooltip;
     * the chart is role="img", so its children say nothing to a screen
     * reader -- what the figure holds is in the aria-label and, exactly, in
     * the table below it. */
    const shown = truncate(labels[i], fits);
    const label = el('text', {
      x: gutter - CAT_PAD, y: y + rowH * 0.72, class: 'cat', 'text-anchor': 'end',
    }, shown);
    if (shown !== labels[i]) label.appendChild(el('title', {}, labels[i]));
    svg.appendChild(label);

    const bar = el('rect', {
      x, y, width: w, height: rowH, rx: 3,
      class: r.__flagged ? 'bar flagged' : 'bar',
    });
    attachTip(bar, tipHtml(r, cat, value, unit, extra));
    svg.appendChild(bar);

    /* Always at the free end of the bar, so the number never sits over the
     * fill and never crosses the zero line. */
    svg.appendChild(el('text', {
      x: v < 0 ? x - 8 : x + w + 8,
      y: y + rowH * 0.72,
      class: 'value',
      'text-anchor': v < 0 ? 'end' : 'start',
    }, fmt(v)));
  });
  return describe(svg, (desc
    || `Bar chart: ${unit || value} by ${cat}, ${rows.length} bars.`)
    + (signed ? SIGNED_NOTE : ''));
}

/* --- vertical bars: histograms, months, bands --------------------------- */
export function vbars({ rows, cat, value, unit, extra, rotate, desc }) {
  const W = 900;
  const H = 236;
  const top = 22;
  const bottom = rotate ? 52 : 34;
  const left = 8;
  const right = 8;
  const plotW = W - left - right;
  const plotH = H - top - bottom;
  const n = rows.length || 1;
  const slot = plotW / n;
  const barW = Math.max(slot - 3, 2);
  const { signed, max } = signedScale(rows.map((r) => r[value]));
  /* Signed: zero sits mid-plot and each half keeps a label's height clear, so
   * the longest bar's own value never runs off the plot or into the
   * category labels below it. */
  const labelPad = 14;
  const zeroY = signed ? top + plotH / 2 : top + plotH;
  const reach = signed ? plotH / 2 - labelPad : plotH;

  const svg = svgRoot(W, H);
  svg.appendChild(el('line', {
    x1: left, y1: zeroY, x2: W - right, y2: zeroY, class: signed ? 'axis zero' : 'axis',
  }));

  rows.forEach((r, i) => {
    const v = Number(r[value]) || 0;
    const h = Math.max((Math.abs(v) / max) * reach, v !== 0 ? 2 : 0);
    const x = left + i * slot + (slot - barW) / 2;
    const y = v < 0 ? zeroY : zeroY - h;

    const bar = el('rect', {
      x, y, width: barW, height: h, rx: 3,
      class: r.__flagged ? 'bar flagged' : 'bar',
    });
    attachTip(bar, tipHtml(r, cat, value, unit, extra));
    svg.appendChild(bar);

    svg.appendChild(el('text', {
      x: x + barW / 2, y: v < 0 ? y + h + 12 : y - 6, class: 'value', 'text-anchor': 'middle',
    }, fmt(v)));

    /* Truncated labels carry their whole text in a <title>, as the hbars
     * gutter does. The limit stays a character count here rather than being
     * measured: a vertical bar's label gets a slot, plotW/n, and a slot
     * cannot grow to fit -- widening one would narrow its neighbour. So the
     * tooltip is the whole of the fix on this side. */
    const full = String(r[cat] ?? '');
    const shown = truncate(full, rotate ? 14 : 9);
    const label = el('text', {
      x: x + barW / 2, y: top + plotH + 14, class: 'cat', 'text-anchor': rotate ? 'end' : 'middle',
    }, shown);
    if (rotate) {
      label.setAttribute('transform', `rotate(-38 ${x + barW / 2} ${top + plotH + 14})`);
    }
    if (shown !== full) label.appendChild(el('title', {}, full));
    svg.appendChild(label);
  });
  return describe(svg, (desc
    || `Bar chart: ${unit || value} by ${cat}, ${rows.length} bars.`)
    + (signed ? SIGNED_NOTE : ''));
}

/* --- line: one measure over publication year ---------------------------
 * The only chart here with a value axis, so the only one with ticks. Ticks
 * are compact scale marks; the exact figures are the endpoint labels and the
 * hover readout, and the table below the chart carries every value.
 */
export function lineSeries({ rows, x, y, unit, extra, desc }) {
  const W = 900;
  const H = 210;
  const m = { t: 16, r: 20, b: 28, l: 62 };
  const plotW = W - m.l - m.r;
  const plotH = H - m.t - m.b;

  const pts = rows
    .map((r) => ({ x: Number(r[x]), y: Number(r[y]), row: r }))
    .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (pts.length === 0) return svgRoot(W, 1);

  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  const yLo = y0 - (y1 - y0) * 0.12 || y0 * 0.98;
  const yHi = y1 + (y1 - y0) * 0.12 || y1 * 1.02;

  const px = (v) => m.l + (x1 === x0 ? plotW / 2 : ((v - x0) / (x1 - x0)) * plotW);
  const py = (v) => m.t + plotH - (yHi === yLo ? plotH / 2 : ((v - yLo) / (yHi - yLo)) * plotH);

  const svg = svgRoot(W, H);

  for (let i = 0; i <= 3; i += 1) {
    const v = yLo + ((yHi - yLo) * i) / 3;
    const gy = py(v);
    svg.appendChild(el('line', { x1: m.l, y1: gy, x2: W - m.r, y2: gy, class: 'grid' }));
    svg.appendChild(el('text', { x: m.l - 8, y: gy + 3.5, 'text-anchor': 'end' }, compact.format(v)));
  }

  /* A line can cross zero where a bar cannot: the ticks alone would leave the
   * sign of a dip to arithmetic, so zero gets its own rule when it is inside
   * the domain. */
  if (yLo < 0 && yHi > 0) {
    svg.appendChild(el('line', {
      x1: m.l, y1: py(0), x2: W - m.r, y2: py(0), class: 'axis zero',
    }));
  }

  svg.appendChild(el('path', {
    class: 'series',
    d: pts.map((p, i) => `${i ? 'L' : 'M'}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(''),
  }));

  svg.appendChild(el('text', { x: m.l, y: H - 8 }, fmt(x0)));
  svg.appendChild(el('text', { x: W - m.r, y: H - 8, 'text-anchor': 'end' }, fmt(x1)));

  /* Direct labels only where they earn the space: the peak and the last point. */
  const peak = pts.reduce((a, b) => (b.y > a.y ? b : a), pts[0]);
  const last = pts[pts.length - 1];
  for (const p of new Set([peak, last])) {
    svg.appendChild(el('circle', { cx: px(p.x), cy: py(p.y), r: 4, class: 'marker' }));
    svg.appendChild(el('text', {
      x: px(p.x), y: py(p.y) - 10, class: 'value',
      'text-anchor': p === last ? 'end' : 'middle',
    }, fmt(p.y)));
  }

  /* Crosshair + readout across the whole plot. */
  const cross = el('line', { x1: 0, y1: m.t, x2: 0, y2: m.t + plotH, class: 'crosshair' });
  cross.style.display = 'none';
  const dot = el('circle', { r: 4.5, class: 'marker' });
  dot.style.display = 'none';
  svg.appendChild(cross);
  svg.appendChild(dot);

  const hit = el('rect', { x: m.l, y: m.t, width: plotW, height: plotH, class: 'hit' });
  hit.addEventListener('mousemove', (evt) => {
    const box = svg.getBoundingClientRect();
    const scale = W / box.width;
    const vx = (evt.clientX - box.left) * scale;
    let nearest = pts[0];
    for (const p of pts) {
      if (Math.abs(px(p.x) - vx) < Math.abs(px(nearest.x) - vx)) nearest = p;
    }
    cross.setAttribute('x1', px(nearest.x));
    cross.setAttribute('x2', px(nearest.x));
    dot.setAttribute('cx', px(nearest.x));
    dot.setAttribute('cy', py(nearest.y));
    cross.style.display = '';
    dot.style.display = '';
    showTip(evt, tipHtml(nearest.row, x, y, unit, extra));
  });
  hit.addEventListener('mouseleave', () => {
    cross.style.display = 'none';
    dot.style.display = 'none';
    hideTip();
  });
  svg.appendChild(hit);
  return describe(svg, desc
    || `Line chart: ${unit || y} by ${x}, ${pts.length} points.`);
}

function tipHtml(row, cat, value, unit, extra = []) {
  const lines = [`<b>${escapeHtml(String(row[cat] ?? ''))}</b>`];
  lines.push(`<span class="t">${escapeHtml(unit || value)}</span> ${fmt(row[value])}`);
  for (const k of extra) {
    if (row[k] === undefined || row[k] === null) continue;
    lines.push(`<span class="t">${escapeHtml(k)}</span> ${fmt(row[k])}`);
  }
  return lines.join('<br>');
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
