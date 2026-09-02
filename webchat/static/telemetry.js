/* The Telemetry view: the local log, summarised by the summariser.
 *
 * `/api/telemetry` is `goodreads-telemetry --json` behind a route: the same
 * load() and summarise() the CLI runs, so a figure here is the figure the
 * command prints. This file only lays the summary out. It never re-derives a
 * rate or a percentile from the raw lines, because it never sees them.
 *
 * Scope is stated on the view, not assumed: the log is the one a server on
 * this machine writes under stdio. A deployed server writes to Cloud Logging,
 * which this console cannot read and should not be able to.
 */

import { node, ms, bytes } from './cards.js';
import { hbars, fmt } from './charts.js';

const box = document.getElementById('telemetry');

let started = false;

export function initTelemetry() {
  if (started) return;
  started = true;
  box.replaceChildren(node('div', 'empty', 'reading the telemetry log…'));
  fetch('/api/telemetry')
    .then(async (r) => {
      const payload = await r.json();
      if (!r.ok) throw new Error(payload.error || `request failed (${r.status})`);
      return payload;
    })
    .then(render)
    .catch((err) => {
      box.replaceChildren(node('div', 'notice', err.message || 'could not reach the console backend'));
    });
}

function render(s) {
  box.replaceChildren();

  const lede = node('header', 'lede');
  const title = node('h2', '');
  title.appendChild(document.createTextNode('Telemetry '));
  title.appendChild(node('span', 'pill', s.scope || 'local-session'));
  lede.appendChild(title);
  lede.appendChild(node('p', '',
    'One line per tool call, written by the server on this machine and summarised by the '
    + 'goodreads-telemetry command’s own code. Production telemetry is not here: a deployed '
    + 'server writes to Cloud Logging, which this console cannot read.'));
  box.appendChild(lede);

  if (!s.exists) {
    const empty = node('div', 'empty');
    empty.appendChild(node('div', '', 'no local telemetry log'));
    empty.appendChild(node('div', 'footnote',
      `nothing has been written at ${s.path}. Run the server on this machine and call a tool; `
      + 'each call appends one line.'));
    box.appendChild(empty);
    return;
  }
  if (!s.calls) {
    box.appendChild(node('div', 'empty', `the log at ${s.path} holds no complete lines`));
    return;
  }

  box.appendChild(headline(s));
  box.appendChild(perTool(s));
  box.appendChild(guards(s));
  box.appendChild(params(s));

  const foot = node('div', 'contract');
  const bits = [s.path, `${fmt(s.calls)} lines`];
  if (s.malformed) bits.push(`${fmt(s.malformed)} malformed line(s) skipped`);
  foot.textContent = bits.join(' · ');
  box.appendChild(foot);
}

/* --- the headline figures ------------------------------------------------ */

function headline(s) {
  const tiles = node('div', 'tiles');
  const outcomes = Object.entries(s.outcomes || {}).map(([k, v]) => `${k} ${fmt(v)}`).join(' · ');
  tiles.appendChild(tile(fmt(s.calls), 'tool calls', outcomes));
  tiles.appendChild(tile(`${fmt(s.error_rate * 100)}%`, 'error rate',
    'anything but outcome=ok, including parameter refusals', s.error_rate > 0));
  tiles.appendChild(tile(ms(s.p50_ms), 'p50 wall time', `p95 ${ms(s.p95_ms)} · nearest-rank`));
  tiles.appendChild(tile(bytes(s.bytes_billed), 'bigquery bytes billed',
    `${bytes(s.bytes_processed)} processed`));
  tiles.appendChild(tile(
    s.cache_hit_rate === null || s.cache_hit_rate === undefined ? '—' : `${fmt(s.cache_hit_rate * 100)}%`,
    'cache hit rate',
    s.cache_hit_rate === null || s.cache_hit_rate === undefined
      ? 'no call reported a cache flag' : 'of calls whose every query was served from cache'));
  const guardTotal = Object.values(s.guard_rejections_by_rule || {}).reduce((a, b) => a + b, 0);
  tiles.appendChild(tile(fmt(guardTotal), 'guard rejections',
    guardTotal ? 'a query named a banned column or pattern' : 'none: the tools cannot reach a banned column',
    guardTotal > 0));
  return tiles;
}

function tile(value, label, of, flag = false) {
  const t = node('div', 'tile');
  t.appendChild(node('div', `value${flag ? ' flag' : ''}`, value));
  t.appendChild(node('div', 'label', label));
  if (of) t.appendChild(node('div', 'of', of));
  return t;
}

/* --- calls per tool ------------------------------------------------------ */

function perTool(s) {
  const section = node('section', 'section');
  section.appendChild(head('calls per tool', 'ordered by calls, as the summariser orders them'));
  const rows = Object.entries(s.per_tool || {}).map(([tool, t]) => ({ tool, ...t }));
  const panel = node('div', 'panel');
  if (!rows.length) {
    panel.appendChild(node('div', 'empty', 'no calls'));
  } else {
    panel.appendChild(hbars({
      rows, cat: 'tool', value: 'calls', unit: 'calls',
      extra: ['errors', 'p50_ms', 'p95_ms', 'bytes_billed'],
      desc: `Bar chart: calls per tool, ${rows.length} tools, ${fmt(s.calls)} calls in the local log.`,
    }));
    panel.appendChild(table(
      ['tool', 'calls', 'errors', 'p50 ms', 'p95 ms', 'billed'],
      rows.map((r) => [r.tool, fmt(r.calls), fmt(r.errors), fmt(Math.round(r.p50_ms)),
        fmt(Math.round(r.p95_ms)), bytes(r.bytes_billed)]),
      `${rows.length} tools: calls, errors, p50 and p95 wall time, bytes billed`,
    ));
  }
  section.appendChild(panel);
  return section;
}

/* --- guard rejections ---------------------------------------------------- */

function guards(s) {
  const section = node('section', 'section');
  section.appendChild(head('guard rejections', 'the rule that fired and the column it named; never the SQL'));
  const panel = node('div', 'panel');
  const byRule = Object.entries(s.guard_rejections_by_rule || {});
  const byColumn = Object.entries(s.guard_rejections_by_column || {});
  if (!byRule.length) {
    panel.appendChild(node('div', 'empty',
      'no guard rejections in this log. That is the design working: no tool interpolates a '
      + 'caller-supplied column name, so no call can reach a banned one.'));
  } else {
    panel.appendChild(table(['rule', 'rejections'], byRule.map(([k, v]) => [k, fmt(v)]),
      'guard rejections by rule'));
    panel.appendChild(table(['column', 'rejections'], byColumn.map(([k, v]) => [k, fmt(v)]),
      'guard rejections by column'));
  }
  section.appendChild(panel);
  return section;
}

/* --- parameter distributions -------------------------------------------- */

function params(s) {
  const section = node('section', 'section');
  section.appendChild(head('parameters passed',
    'the values callers actually chose for the parameters that decide what a result means'));
  const panel = node('div', 'panel');
  const tracked = Object.entries(s.params || {});
  if (!tracked.length) panel.appendChild(node('div', 'empty', 'no tracked parameters'));
  for (const [key, counts] of tracked) {
    const values = Object.entries(counts);
    const label = node('div', 'figure-label', key);
    panel.appendChild(label);
    if (!values.length) {
      panel.appendChild(node('div', 'footnote', 'never passed: always left at its default'));
      continue;
    }
    const pct = (s.params_pct || {})[key] || {};
    panel.appendChild(table(
      ['value', 'calls', 'share of all calls'],
      values.sort((a, b) => b[1] - a[1]).map(([v, n]) => [v, fmt(n), pct[v] ?? '—']),
      `${key}: values passed, with calls and share of all calls`,
    ));
  }
  section.appendChild(panel);
  return section;
}

/* --- small parts --------------------------------------------------------- */

function head(title, note) {
  const h = node('div', 'section-head');
  h.appendChild(node('span', '', title));
  if (note) h.appendChild(node('span', 'note', note));
  return h;
}

/* A plain table of already-formatted strings. The first column is a name;
 * the rest are numbers, right-aligned. */
function table(columns, rows, caption) {
  const t = document.createElement('table');
  t.appendChild(node('caption', 'sr-only', caption));
  const thead = document.createElement('thead');
  const hr = document.createElement('tr');
  columns.forEach((c, i) => {
    const th = node('th', i ? 'num' : '', c);
    th.setAttribute('scope', 'col');
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  t.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    r.forEach((v, i) => tr.appendChild(node('td', i ? 'num' : 'mono', v)));
    tbody.appendChild(tr);
  }
  t.appendChild(tbody);
  return t;
}
