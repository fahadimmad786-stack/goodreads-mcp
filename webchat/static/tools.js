/* No-model mode: pick a tool, fill in its parameters, get the same card.
 *
 * Every field on screen is generated from the tool's own JSON Schema, fetched
 * from `/api/tools`, which is `tools/list` reshaped and nothing else. There is
 * no per-tool form in this file and no list of any tool's parameters: the
 * widget, its label, its description, its accepted range and its default all
 * come out of the schema, so a `Field(...)` edited in `server.py` changes this
 * form on the next deploy. Hand-writing the forms would make the UI a
 * hand-maintained copy of the tool surface — the documentation-instead-of-
 * structure failure the whole project is built to avoid.
 *
 * Two consequences worth stating, because they look like omissions:
 *
 *  * An empty field is not sent. The server's default applies, and the card's
 *    parameter row then shows exactly what was overridden rather than a wall
 *    of values the caller never chose. The default is printed beside each
 *    field, and sits in the placeholder, so nothing is hidden by this.
 *  * Values are passed verbatim. `min_ratings=0` and `unit=chapters` reach the
 *    server unaltered, because the refusal that comes back — with the server's
 *    own explanation and the caveats behind the constraint — is the most
 *    instructive thing this console can show. Validating here would replace
 *    that with silence.
 *
 * The result is handed to the same renderers chat mode uses. A card cannot
 * tell which mode fetched it.
 */

import { renderToolCard, renderRefusalCard, renderPendingCard, node } from './cards.js';
import { announce } from './live.js';

const thread = document.getElementById('thread');
const pane = document.getElementById('pane-tools');
const picker = document.getElementById('tool-picker');
const fieldsBox = document.getElementById('tool-fields');
const describe = document.getElementById('tool-describe');
const runButton = document.getElementById('tool-run');
const resetButton = document.getElementById('tool-reset');
const presetsBox = document.getElementById('tool-presets');

let catalogue = [];
let fields = [];          /* the current tool's fields, read from its schema */
let loaded = false;
let busy = false;

/* Starting points, in parameter values only — never form structure. Two of
 * them exist to be refused: the schema floor on min_ratings, and the guard.
 * Each is dropped silently if its tool is not in the catalogue. */
const PRESETS = [
  { label: 'most-read authors, by works', tool: 'stats_by_author',
    params: { unit: 'works', order_by: 'n_ratings' } },
  { label: 'ratings by decade', tool: 'stats_by_year',
    params: { year_from: 1950, year_to: 2020 } },
  { label: 'rating distribution', tool: 'rating_distribution',
    params: { bucket_size: 0.25 } },
  { label: 'include unrated books', tool: 'top_books_by_rating',
    params: { min_ratings: 0 }, warn: 'trips the schema floor' },
  { label: 'day-of-month', tool: 'check_column_available',
    params: { column: 'publish_day' }, warn: 'trips the query guard' },
];

/* --- loading ------------------------------------------------------------- */

export async function initToolMode() {
  if (loaded) return;
  loaded = true;
  picker.disabled = true;
  picker.setAttribute('aria-busy', 'true');
  picker.replaceChildren(node('option', '', 'loading the tool list…'));
  fieldsBox.replaceChildren(node('div', 'empty', 'fetching the tool schemas over MCP…'));

  let payload;
  try {
    const r = await fetch('/api/tools');
    payload = await r.json();
    if (!r.ok) throw new Error(payload.error || `request failed (${r.status})`);
  } catch (err) {
    loaded = false;
    picker.removeAttribute('aria-busy');
    picker.replaceChildren(node('option', '', 'tool list unavailable'));
    fieldsBox.replaceChildren(node('div', 'empty',
      'no schemas, so no form — the tool list could not be fetched'));
    notice(err.message || 'could not reach the console backend');
    return;
  }

  catalogue = payload.tools || [];
  picker.disabled = false;
  picker.removeAttribute('aria-busy');
  picker.replaceChildren();
  for (const tool of catalogue) {
    const option = node('option', '', tool.name);
    option.value = tool.name;
    picker.appendChild(option);
  }
  buildPresets();
  picker.addEventListener('change', () => select(picker.value));
  select(catalogue[0] && catalogue[0].name);
}

function toolNamed(name) {
  return catalogue.find((t) => t.name === name) || null;
}

/* --- the form ------------------------------------------------------------ */

function select(name, values) {
  const tool = toolNamed(name);
  if (!tool) return;
  picker.value = tool.name;

  describe.replaceChildren();
  const badge = node('span', `origin ${tool.origin}`, tool.origin);
  badge.title = tool.origin === 'bff'
    ? 'runs in this console, not on the MCP server'
    : 'a tool of the goodreads-stats MCP server, called over MCP';
  describe.appendChild(badge);
  if (tool.origin === 'bff') {
    describe.appendChild(node('span', 'demo-note', 'demonstration probe, not a data path'));
  }
  /* The tool's own docstring, as the server wrote it -- minus the source
   * wrapping. A docstring is hard-wrapped at ~80 columns; those breaks are
   * a fact about the Python file, not about the prose, so lines are joined
   * within a paragraph and only blank lines survive as paragraph breaks. */
  for (const text of paragraphsOf(tool.description)) {
    describe.appendChild(node('p', '', text));
  }

  fields = readSchema(tool.schema);
  fieldsBox.replaceChildren();
  if (!fields.length) {
    fieldsBox.appendChild(node('div', 'no-params', 'this tool takes no parameters'));
  }
  for (const field of fields) {
    fieldsBox.appendChild(renderField(field, values && values[field.name]));
  }
}

/* Description text -> its paragraphs, each with its whitespace collapsed.
 * Splits on blank lines (one or more, with any trailing spaces on the empty
 * line), joins the hard-wrapped lines inside a paragraph with single spaces,
 * and drops paragraphs that were only whitespace. Pure: no DOM, so it can be
 * run outside the page. */
export function paragraphsOf(text) {
  return String(text || '')
    .split(/\n[ \t]*\n/)
    .map((para) => para.replace(/\s+/g, ' ').trim())
    .filter((para) => para.length > 0);
}

/* JSON Schema -> the facts one form field needs. Nothing tool-specific: the
 * same reader handles every tool, and an unrecognised shape falls back to a
 * text box rather than being dropped. */
function readSchema(schema) {
  const properties = (schema && schema.properties) || {};
  const required = new Set((schema && schema.required) || []);
  return Object.entries(properties).map(([name, spec]) => {
    const types = typesOf(spec);
    const values = enumOf(spec);
    return {
      name,
      spec,
      type: types.find((t) => t !== 'null') || 'string',
      nullable: types.includes('null'),
      required: required.has(name),
      enum: values,
      description: spec.description || '',
      hasDefault: Object.prototype.hasOwnProperty.call(spec, 'default'),
      default: spec.default,
      minimum: numberIn(spec, 'minimum'),
      maximum: numberIn(spec, 'maximum'),
      exclusiveMinimum: numberIn(spec, 'exclusiveMinimum'),
      exclusiveMaximum: numberIn(spec, 'exclusiveMaximum'),
    };
  });
}

/* `type` may be a string, a list, or split across anyOf/oneOf — which is how a
 * nullable parameter arrives from pydantic. All three are read the same way. */
function typesOf(spec) {
  const out = [];
  const push = (t) => {
    if (typeof t === 'string' && !out.includes(t)) out.push(t);
    else if (Array.isArray(t)) t.forEach(push);
  };
  push(spec.type);
  for (const branch of spec.anyOf || spec.oneOf || []) push(branch.type);
  return out;
}

function enumOf(spec) {
  if (Array.isArray(spec.enum)) return spec.enum;
  for (const branch of spec.anyOf || spec.oneOf || []) {
    if (Array.isArray(branch.enum)) return branch.enum;
  }
  return null;
}

/* A constraint may sit on the property or inside one anyOf branch. */
function numberIn(spec, key) {
  if (typeof spec[key] === 'number') return spec[key];
  for (const branch of spec.anyOf || spec.oneOf || []) {
    if (typeof branch[key] === 'number') return branch[key];
  }
  return undefined;
}

function renderField(field, prefill) {
  const wrap = node('div', 'field');

  const label = node('label', '');
  label.htmlFor = `p-${field.name}`;
  label.appendChild(node('span', 'pname', field.name));
  if (field.required) label.appendChild(node('span', 'req', 'required'));
  wrap.appendChild(label);

  const input = widget(field);
  input.id = `p-${field.name}`;
  input.dataset.param = field.name;
  if (prefill !== undefined && prefill !== null) input.value = String(prefill);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
  wrap.appendChild(input);

  /* Everything on this line is read off the schema. */
  wrap.appendChild(node('div', 'meta', constraintText(field)));
  for (const text of paragraphsOf(field.description)) {
    wrap.appendChild(node('div', 'pdesc', text));
  }
  return wrap;
}

function widget(field) {
  if (field.enum) {
    const select = node('select', '');
    select.appendChild(blankOption(field));
    for (const value of field.enum) {
      const option = node('option', '', String(value));
      option.value = String(value);
      select.appendChild(option);
    }
    return select;
  }
  if (field.type === 'boolean') {
    const select = node('select', '');
    select.appendChild(blankOption(field));
    for (const value of ['true', 'false']) {
      const option = node('option', '', value);
      option.value = value;
      select.appendChild(option);
    }
    return select;
  }
  const input = node('input', '');
  if (field.type === 'integer' || field.type === 'number') {
    input.type = 'number';
    input.step = field.type === 'integer' ? '1' : 'any';
    /* min/max are shown on the meta line, not set as attributes: the browser
     * would clamp them, and a value the schema rejects is exactly what the
     * refusal cards exist to show. */
  } else {
    input.type = 'text';
  }
  input.placeholder = field.hasDefault && field.default !== null
    ? String(field.default)
    : (field.required ? '' : 'server default');
  return input;
}

function blankOption(field) {
  const option = node('option', '',
    field.required ? '— choose —' : '— server default —');
  option.value = '';
  return option;
}

function constraintText(field) {
  const bits = [field.type + (field.nullable ? ' or null' : '')];
  if (field.minimum !== undefined) bits.push(`min ${field.minimum}`);
  if (field.exclusiveMinimum !== undefined) bits.push(`> ${field.exclusiveMinimum}`);
  if (field.maximum !== undefined) bits.push(`max ${field.maximum}`);
  if (field.exclusiveMaximum !== undefined) bits.push(`< ${field.exclusiveMaximum}`);
  if (field.enum) bits.push(`one of ${field.enum.join(', ')}`);
  if (field.hasDefault) {
    bits.push(`default ${field.default === null ? 'none' : field.default}`);
  }
  if (!field.required && !field.hasDefault) bits.push('optional');
  return bits.join(' · ');
}

/* --- collecting and sending ---------------------------------------------- */

/* Typed to the schema, not guessed from the text: an integer field sends a
 * number, a string field sends a string. Junk in a number field is sent as the
 * string it is, so the server refuses it rather than this file inventing a
 * value. */
function collect() {
  const params = {};
  for (const field of fields) {
    const input = fieldsBox.querySelector(`[data-param="${field.name}"]`);
    if (!input) continue;
    const raw = input.value.trim();
    if (raw === '') continue;                 /* omitted -> server default */
    params[field.name] = coerce(field, raw);
  }
  return params;
}

function coerce(field, raw) {
  if (field.type === 'integer' || field.type === 'number') {
    const value = Number(raw);
    return Number.isFinite(value) ? value : raw;
  }
  if (field.type === 'boolean') {
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return raw;
  }
  return raw;
}

async function submit() {
  if (busy) return;
  const tool = toolNamed(picker.value);
  if (!tool) return;

  busy = true;
  runButton.disabled = true;
  runButton.setAttribute('aria-busy', 'true');
  runButton.textContent = 'running…';
  announce(`running ${tool.name}`);
  const intro = document.getElementById('intro-tools');
  if (intro) intro.remove();

  const params = collect();
  const id = `local_${Math.random().toString(36).slice(2, 10)}`;
  const block = node('div', 'turn');
  block.appendChild(node('div', 'role', 'tool call'));
  const body = node('div', '');
  body.appendChild(renderPendingCard({ id, tool: tool.name, origin: tool.origin, params }));
  block.appendChild(body);
  pane.appendChild(block);
  scroll();

  let frame;
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: tool.name, params }),
    });
    frame = await r.json();
    if (!r.ok) {
      /* A console-level rejection — rate limit, unknown tool, malformed body.
       * Not a tool refusal, so it must not be dressed up as one. */
      const message = frame.error || `request failed (${r.status})`;
      body.replaceChildren(noticeBox(message));
      announce(message);
      finish();
      return;
    }
  } catch (err) {
    body.replaceChildren(noticeBox('could not reach the console backend'));
    announce('could not reach the console backend');
    finish();
    return;
  }

  const card = frame.type === 'tool_refusal'
    ? renderRefusalCard(frame)
    : renderToolCard(frame);
  body.replaceChildren(card);
  announce(frame.type === 'tool_refusal'
    ? `${tool.name} was refused: ${frame.kind.replace('_', ' ')}`
    : `${tool.name} returned a result`);
  finish();
}

function finish() {
  busy = false;
  runButton.disabled = false;
  runButton.removeAttribute('aria-busy');
  runButton.textContent = 'run';
  scroll();
}

function scroll() { thread.scrollTop = thread.scrollHeight; }

/* --- chrome -------------------------------------------------------------- */

function buildPresets() {
  presetsBox.replaceChildren();
  const available = PRESETS.filter((p) => toolNamed(p.tool));
  if (!available.length) return;
  presetsBox.appendChild(node('span', 'lead', 'try'));
  for (const preset of available) {
    const button = node('button', '');
    button.type = 'button';
    button.appendChild(document.createTextNode(preset.label));
    if (preset.warn) button.appendChild(node('span', 'warn', ` ${preset.warn}`));
    button.addEventListener('click', () => {
      select(preset.tool, preset.params);
      submit();
    });
    presetsBox.appendChild(button);
  }
}

function noticeBox(message) { return node('div', 'notice', message); }

function notice(message) {
  pane.appendChild(noticeBox(message));
  scroll();
}

runButton.addEventListener('click', submit);
resetButton.addEventListener('click', () => select(picker.value));
