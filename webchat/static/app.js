/* The shell, and the chat mode's thread controller.
 *
 * Two modes reach the same tools: a model choosing one from a question, and a
 * person filling in its parameters (tools.js). They share the whole of the
 * rendering — cards.js and charts.js — and differ only in what fetches the
 * envelope. Which modes are on offer is the server's answer, not a build-time
 * choice: `/api/health` reports whether an Anthropic key is configured, and
 * the chat box is not offered at all when it is not.
 *
 * Chat mode itself: the model's prose and the tool cards interleave in the
 * order the server emitted them, so a card sits where the model paused to
 * fetch it. Prose is kept as plain text until the turn's `contract` frame
 * arrives, at which point any numeral the checker could not source is marked
 * in place.
 */

import {
  renderToolCard, renderRefusalCard, renderPendingCard, escapeHtml, node as el,
} from './cards.js';
import { initToolMode } from './tools.js';

const thread = document.getElementById('thread');
const chatPane = document.getElementById('pane-chat');
const input = document.getElementById('input');
const send = document.getElementById('send');
const status = document.getElementById('status');
const statusDot = document.getElementById('status-dot');
const modeButtons = {
  chat: document.getElementById('mode-chat'),
  tools: document.getElementById('mode-tools'),
};

const MODE_KEY = 'gr_mode';
let chatEnabled = true;

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
    chatPane.appendChild(this.root);
    this.scroll();
  }

  scroll() { thread.scrollTop = thread.scrollHeight; }

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
  if (busy) return;
  busy = true;
  send.disabled = true;
  input.value = '';
  const intro = document.getElementById('intro');
  if (intro) intro.remove();

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
    case 'tool_call': turn.card(renderPendingCard(frame)); break;
    case 'tool_result': replace(turn, frame, renderToolCard(frame)); break;
    case 'tool_refusal': replace(turn, frame, renderRefusalCard(frame)); break;
    case 'contract': turn.contract(frame); break;
    case 'turn_end': turn.footer(frame); break;
    case 'error': turn.error(frame.message); break;
    default: break;
  }
}

function replace(turn, frame, card) {
  const placeholder = turn.answer.querySelector(`[data-call="${frame.id}"]`);
  if (placeholder) {
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
  input.focus();
}

/* --- chrome -------------------------------------------------------------- */

function buildExamples() {
  const box = document.getElementById('examples');
  box.appendChild(el('span', 'lead', 'try'));
  for (const ex of EXAMPLES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.appendChild(document.createTextNode(ex.q));
    if (ex.warn) b.appendChild(el('span', 'warn', ` ${ex.warn}`));
    b.addEventListener('click', () => ask(ex.q));
    box.appendChild(b);
  }
}

/* --- modes ---------------------------------------------------------------
 *
 * The mode is a property of the page, switched on the body, so the two panes
 * and the two composers are pure CSS. Each keeps its own thread, so switching
 * mode never discards what the other one has already fetched. */

function setMode(mode) {
  const target = mode === 'chat' && !chatEnabled ? 'tools' : mode;
  document.body.dataset.mode = target;
  for (const [name, button] of Object.entries(modeButtons)) {
    button.setAttribute('aria-selected', String(name === target));
  }
  try { localStorage.setItem(MODE_KEY, target); } catch (err) { /* private mode */ }
  if (target === 'tools') initToolMode();
  else input.focus();
}

/* Chat is offered only when the server says a key is configured. The button is
 * disabled rather than removed, so the reason is visible rather than absent. */
function applyChatAvailability(enabled) {
  chatEnabled = enabled;
  modeButtons.chat.disabled = !enabled;
  modeButtons.chat.title = enabled
    ? 'a model chooses the tool and writes the prose'
    : 'off: this deployment has no ANTHROPIC_API_KEY. The tool mode needs none.';
  if (!enabled) modeButtons.chat.appendChild(el('span', 'off', 'no key'));
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

  let remembered = null;
  try { remembered = localStorage.getItem(MODE_KEY); } catch (err) { /* private mode */ }
  setMode(remembered === 'tools' || remembered === 'chat' ? remembered : 'chat');
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = input.value.trim();
    if (text) ask(text);
  }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 144)}px`;
});
send.addEventListener('click', () => {
  const text = input.value.trim();
  if (text) ask(text);
});

modeButtons.chat.addEventListener('click', () => setMode('chat'));
modeButtons.tools.addEventListener('click', () => setMode('tools'));

buildExamples();
loadHealth();
