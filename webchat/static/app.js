/* The shell: four views on one rail, and the chat thread in the Overview.
 *
 * Overview, Tools, Defects and Telemetry are panels of a vertical tablist in
 * the left rail. The view is a property of the page, switched on the body, so
 * showing one and hiding the rest is pure CSS; each view keeps its own state,
 * so switching never discards what another has already fetched.
 *
 * Chat lives in the Overview. A model choosing the tool and a person filling
 * in its form (tools.js) share the whole of the rendering -- cards.js and
 * charts.js -- and differ only in what fetches the envelope. Which of the two
 * is on offer is the server's answer: `/api/health` reports whether an
 * Anthropic key is configured, and the composer is not shown when it is not.
 *
 * Chat itself: the model's prose and the tool cards interleave in the order
 * the server emitted them, so a card sits where the model paused to fetch it.
 * Prose is kept as plain text until the turn's `contract` frame arrives, at
 * which point any numeral the checker could not source is marked in place.
 */

import {
  renderToolCard, renderRefusalCard, renderPendingCard, escapeHtml, node as el,
} from './cards.js';
import { announce } from './live.js';
import { initToolMode, PRESETS, runPreset } from './tools.js';
import { initOverview } from './overview.js';
import { initDefects } from './defects.js';
import { initTelemetry } from './telemetry.js';
import { catalogue } from './data.js';

const main = document.getElementById('thread');
const chatThread = document.getElementById('chat-thread');
const input = document.getElementById('input');
const send = document.getElementById('send');
const composer = document.getElementById('composer-chat');
const status = document.getElementById('status');
const statusDot = document.getElementById('status-dot');
const railTools = document.getElementById('rail-tools');

const VIEWS = ['overview', 'tools', 'defects', 'telemetry'];
const tabs = Object.fromEntries(VIEWS.map((v) => [v, document.getElementById(`tab-${v}`)]));

const VIEW_KEY = 'gr_view';
let chatEnabled = true;

/* The access key arrives as ?k= on the first visit and is exchanged for an
 * HttpOnly cookie by the server. Once that has happened the query string is
 * pure liability -- it sits in the address bar, in the back/forward history,
 * in a screenshot, and in anything the reader copies out of the URL bar. The
 * page has already loaded, so drop it. (Referrer-Policy: no-referrer covers
 * the leg this cannot: a request made before the rewrite.) */
function stripAccessKeyFromUrl() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('k')) return;
  url.searchParams.delete('k');
  const clean = url.pathname + (url.search || '') + url.hash;
  window.history.replaceState(null, '', clean);
}

const EXAMPLES = [
  { q: 'Which authors are the most read?' },
  { q: 'How did publication volume and ratings change between 1950 and 2020?' },
  { q: 'Do longer books get better ratings?' },
  {
    q: 'What is the average rating across every book, including the ones with no ratings?',
    warn: 'trips the min-ratings floor',
  },
  {
    q: 'Which day of the month do publishers favour?',
    warn: 'trips the query guard',
  },
];

let busy = false;

/* --- turn state ---------------------------------------------------------- */

class Turn {
  constructor(text) {
    this.root = document.createElement('div');
    this.root.className = 'turn';

    this.root.appendChild(el('div', 'role', 'you'));
    this.root.appendChild(el('div', 'user-text', text));

    this.answer = document.createElement('div');
    this.root.appendChild(this.answer);

    this.reasoning = null;
    this.proseBlocks = [];
    this.proseLen = 0;
    this.current = null;
    chatThread.appendChild(this.root);
    this.scroll();
  }

  scroll() { main.scrollTop = main.scrollHeight; }

  thinking(text) {
    if (!this.reasoning) {
      const details = document.createElement('details');
      details.className = 'reasoning';
      const summary = document.createElement('summary');
      summary.textContent = 'reasoning';
      const body = el('div', 'body', '');
      details.append(summary, body);
      this.answer.appendChild(details);
      this.reasoning = body;
    }
    this.reasoning.textContent += text;
    this.scroll();
  }

  text(chunk) {
    if (!this.current) {
      const block = el('div', 'prose', '');
      this.answer.appendChild(block);
      this.current = { el: block, start: this.proseLen, text: '' };
      this.proseBlocks.push(this.current);
    }
    this.current.text += chunk;
    this.current.el.textContent = this.current.text;
    this.proseLen += chunk.length;
    this.scroll();
  }

  card(element) {
    this.answer.appendChild(element);
    this.current = null;        /* prose after a card starts a new block */
    this.scroll();
  }

  /* Mark the numerals the checker could not trace to a tool result. */
  contract(frame) {
    for (const block of this.proseBlocks) {
      const end = block.start + block.text.length;
      const hits = frame.unsourced
        .filter((f) => f.start >= block.start && f.end <= end)
        .sort((a, b) => a.start - b.start);
      if (!hits.length) continue;
      let html = '';
      let cursor = 0;
      for (const h of hits) {
        const s = h.start - block.start;
        const e = h.end - block.start;
        html += escapeHtml(block.text.slice(cursor, s));
        html += `<span class="unsourced" title="this numeral does not appear in any tool result, `
             + `tool parameter, server instruction or your own question">`
             + `${escapeHtml(block.text.slice(s, e))}</span>`;
        cursor = e;
      }
      html += escapeHtml(block.text.slice(cursor));
      block.el.innerHTML = html;
    }

    const line = el('div', 'contract', '');
    if (frame.unsourced.length) {
      line.className = 'contract violated';
      line.textContent = `rendering contract: ${frame.unsourced.length} numeral(s) in the prose `
        + 'could not be traced to a tool result — marked above';
    } else {
      line.textContent = 'rendering contract: every numeral in the prose traces to a tool result';
    }
    this.answer.appendChild(line);
    this.scroll();
  }

  footer(frame) {
    const usage = frame.usage || {};
    const bits = [
      `${frame.tool_calls} tool ${frame.tool_calls === 1 ? 'call' : 'calls'}`,
      `${frame.turns_left} turns left in this session`,
      `${usage.input_tokens ?? 0} in / ${usage.output_tokens ?? 0} out tokens`,
    ];
    if (usage.cache_read_input_tokens) bits.push(`${usage.cache_read_input_tokens} cached`);
    this.answer.appendChild(el('div', 'contract', bits.join(' · ')));
    this.scroll();
  }

  error(message) {
    const box = el('div', 'notice', message);
    this.answer.appendChild(box);
    this.scroll();
  }
}

/* --- transport ----------------------------------------------------------- */

async function ask(text) {
  if (busy || !chatEnabled) return;
  busy = true;
  send.disabled = true;
  send.setAttribute('aria-busy', 'true');
  announce('working');
  input.value = '';
  setView('overview');

  const turn = new Turn(text);

  let response;
  try {
    response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    turn.error('could not reach the console backend');
    finish();
    return;
  }

  if (!response.ok) {
    let message = `request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body.error) message = body.error;
    } catch (err) { /* keep the status-code message */ }
    turn.error(message);
    finish();
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split('\n\n');
    buffer = chunks.pop();
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      let frame;
      try { frame = JSON.parse(line.slice(6)); } catch (err) { continue; }
      place(turn, frame);
    }
  }
  finish();
}

function place(turn, frame) {
  switch (frame.type) {
    case 'session':
      if (frame.restarted) {
        turn.error('the previous session expired on the server, so this turn starts a new one');
      }
      break;
    case 'thinking_delta': turn.thinking(frame.text); break;
    case 'text_delta': turn.text(frame.text); break;
    case 'tool_call':
      turn.card(renderPendingCard(frame));
      announce(`running ${frame.tool}`);
      break;
    case 'tool_result':
      replace(turn, frame, renderToolCard(frame));
      announce(`${frame.tool} returned a result`);
      break;
    case 'tool_refusal':
      replace(turn, frame, renderRefusalCard(frame));
      announce(`${frame.tool} was refused: ${frame.kind.replace('_', ' ')}`);
      break;
    case 'contract': turn.contract(frame); break;
    case 'turn_end': turn.footer(frame); break;
    case 'error': turn.error(frame.message); break;
    default: break;
  }
}

function replace(turn, frame, card) {
  const placeholder = turn.answer.querySelector(`[data-call="${frame.id}"]`);
  if (placeholder) {
    card.dataset.call = frame.id;
    placeholder.replaceWith(card);
    turn.current = null;
    turn.scroll();
  } else {
    turn.card(card);
  }
}

function finish() {
  busy = false;
  send.disabled = false;
  send.removeAttribute('aria-busy');
  input.focus();
}

/* --- starting points -----------------------------------------------------
 *
 * Cards, in a grid. Questions go to the model; tool calls go to the Tools
 * view and run there. Two of each exist to be refused, and say so. A tool
 * call card is offered only for a tool the catalogue actually lists. */

async function buildStarts() {
  const box = document.getElementById('examples');
  box.replaceChildren();
  box.appendChild(el('div', 'lead label', 'start from'));

  if (chatEnabled) {
    for (const ex of EXAMPLES) {
      const b = document.createElement('button');
      b.type = 'button';
      b.appendChild(el('span', 'kind', 'question'));
      b.appendChild(document.createTextNode(ex.q));
      if (ex.warn) b.appendChild(el('span', 'warn', ex.warn));
      b.addEventListener('click', () => ask(ex.q));
      box.appendChild(b);
    }
  }

  let tools = [];
  try { tools = await catalogue(); } catch (err) { /* the tool cards are simply absent */ }
  const known = new Set(tools.map((t) => t.name));
  for (const preset of PRESETS) {
    if (!known.has(preset.tool)) continue;
    const b = document.createElement('button');
    b.type = 'button';
    b.appendChild(el('span', 'kind', 'tool call'));
    b.appendChild(document.createTextNode(preset.label));
    b.appendChild(el('span', 'tool', preset.tool));
    if (preset.warn) b.appendChild(el('span', 'warn', preset.warn));
    b.addEventListener('click', async () => {
      setView('tools');
      await initToolMode();
      runPreset(preset);
    });
    box.appendChild(b);
  }
}

/* --- views ---------------------------------------------------------------
 *
 * The view is a property of the page, switched on the body, so the four
 * panels are pure CSS. A view initialises itself the first time it is shown
 * and never again, so nothing is fetched for a view nobody opens. */

const INIT = {
  overview: () => initOverview(),
  tools: () => initToolMode(),
  defects: () => initDefects(),
  telemetry: () => initTelemetry(),
};

function setView(view, { focusTab = false } = {}) {
  const target = VIEWS.includes(view) ? view : 'overview';
  document.body.dataset.view = target;
  /* Roving tabindex: one tab stop for the whole tablist, arrow keys inside
   * it. That is the tablist pattern, and it keeps the rail from costing four
   * stops on the way to the content. */
  for (const [name, button] of Object.entries(tabs)) {
    const selected = name === target;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  railTools.hidden = target !== 'tools';
  if (focusTab) tabs[target].focus();
  try { localStorage.setItem(VIEW_KEY, target); } catch (err) { /* private mode */ }
  INIT[target]();
}

/* Up/down and left/right move between tabs, home/end jump to the ends -- the
 * keyboard contract a tablist advertises by having role="tab". */
function onTabKey(event) {
  const current = document.body.dataset.view;
  const at = Math.max(VIEWS.indexOf(current), 0);
  let next = null;
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = VIEWS[(at + 1) % VIEWS.length];
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = VIEWS[(at - 1 + VIEWS.length) % VIEWS.length];
  else if (event.key === 'Home') next = VIEWS[0];
  else if (event.key === 'End') next = VIEWS[VIEWS.length - 1];
  if (!next) return;
  event.preventDefault();
  setView(next, { focusTab: true });
}

/* Chat is offered only when the server says a key is configured. The
 * composer is hidden and the reason is stated where it stood, so the mode
 * is visibly absent rather than silently missing. */
function applyChatAvailability(enabled) {
  chatEnabled = enabled;
  composer.hidden = !enabled;
  if (!enabled) {
    const why = el('div', 'footnote',
      'chat is off: this deployment has no ANTHROPIC_API_KEY. The Tools view needs none.');
    document.getElementById('examples').before(why);
  }
}

async function loadHealth() {
  let health = null;
  try {
    const r = await fetch('/api/health');
    health = await r.json();
    if (health.mcp === 'ok') {
      statusDot.className = 'dot ok';
      const model = health.chat_enabled ? health.model : 'no model';
      status.textContent = `mcp ok · ${health.tools} tools · ${model} · auth ${health.auth}`;
    } else {
      statusDot.className = 'dot bad';
      status.textContent = `mcp ${health.mcp || 'unknown'} · ${health.model || 'no model'}`;
    }
  } catch (err) {
    statusDot.className = 'dot bad';
    status.textContent = 'backend unreachable';
  }

  /* `chat_enabled` is absent from the public health body, which is what an
   * unauthorised caller gets; that body never reaches this code, because the
   * page itself is behind the same token. Default to on if it is missing
   * anyway — /api/chat says so plainly rather than failing obscurely. */
  applyChatAvailability(!health || health.chat_enabled !== false);
  buildStarts();

  let remembered = null;
  try { remembered = localStorage.getItem(VIEW_KEY); } catch (err) { /* private mode */ }
  setView(VIEWS.includes(remembered) ? remembered : 'overview');
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = input.value.trim();
    if (text) ask(text);
  }
});
/* Grow with the content; the cap lives in the stylesheet as max-height, so
 * the limit is stated once rather than in two places that can disagree. */
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
});
send.addEventListener('click', () => {
  const text = input.value.trim();
  if (text) ask(text);
});

for (const [name, button] of Object.entries(tabs)) {
  button.addEventListener('click', () => setView(name));
}
document.getElementById('views').addEventListener('keydown', onTabKey);

stripAccessKeyFromUrl();
loadHealth();
