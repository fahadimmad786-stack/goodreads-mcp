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

/* --- horizontal bars: rankings ------------------------------------------
 * No value axis and no ticks. Each bar is labelled with its exact value, so
 * there is nothing to read off a scale and nothing rounded on screen.
 */
export function hbars({ rows, cat, value, unit, extra, desc }) {
  const W = 900;
  const rowH = 20;
  const gap = 2;               /* surface gap between adjacent bars */
  const gutter = 232;
  const rightPad = 132;
  const H = rows.length * (rowH + gap) + 8;
  const plot = W - gutter - rightPad;
  const max = Math.max(...rows.map((r) => Number(r[value]) || 0), 0) || 1;

  const svg = svgRoot(W, H);
  rows.forEach((r, i) => {
    const y = i * (rowH + gap) + 4;
    const v = Number(r[value]) || 0;
    const w = Math.max((v / max) * plot, v > 0 ? 2 : 0);

    svg.appendChild(el('text', {
      x: gutter - 10, y: y + rowH * 0.72, class: 'cat', 'text-anchor': 'end',
    }, truncate(r[cat], 34)));

    const bar = el('rect', {
      x: gutter, y, width: w, height: rowH, rx: 3,
      class: r.__flagged ? 'bar flagged' : 'bar',
    });
    attachTip(bar, tipHtml(r, cat, value, unit, extra));
    svg.appendChild(bar);

    svg.appendChild(el('text', {
      x: gutter + w + 8, y: y + rowH * 0.72, class: 'value',
    }, fmt(v)));
  });
  return describe(svg, desc
    || `Bar chart: ${unit || value} by ${cat}, ${rows.length} bars.`);
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
  const max = Math.max(...rows.map((r) => Number(r[value]) || 0), 0) || 1;

  const svg = svgRoot(W, H);
  svg.appendChild(el('line', { x1: left, y1: top + plotH, x2: W - right, y2: top + plotH, class: 'axis' }));

  rows.forEach((r, i) => {
    const v = Number(r[value]) || 0;
    const h = Math.max((v / max) * plotH, v > 0 ? 2 : 0);
    const x = left + i * slot + (slot - barW) / 2;
    const y = top + plotH - h;

    const bar = el('rect', {
      x, y, width: barW, height: h, rx: 3,
      class: r.__flagged ? 'bar flagged' : 'bar',
    });
    attachTip(bar, tipHtml(r, cat, value, unit, extra));
    svg.appendChild(bar);

    svg.appendChild(el('text', {
      x: x + barW / 2, y: y - 6, class: 'value', 'text-anchor': 'middle',
    }, fmt(v)));

    const label = el('text', {
      x: x + barW / 2, y: top + plotH + 14, class: 'cat', 'text-anchor': rotate ? 'end' : 'middle',
    }, truncate(r[cat], rotate ? 14 : 9));
    if (rotate) {
      label.setAttribute('transform', `rotate(-38 ${x + barW / 2} ${top + plotH + 14})`);
    }
    svg.appendChild(label);
  });
  return describe(svg, desc
    || `Bar chart: ${unit || value} by ${cat}, ${rows.length} bars.`);
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
