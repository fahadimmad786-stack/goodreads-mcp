/* The two fetches more than one view needs, made once and shared.
 *
 * `overview()` is a real tool call -- dataset_overview, through the same
 * /api/run route the form uses -- so the Overview's tiles and the Defects view
 * render from a tool's own envelope and nothing else. The promise is cached
 * because the two views would otherwise bill the same three BigQuery queries
 * twice within seconds; a failed fetch is forgotten so the next view can
 * retry.
 *
 * `catalogue()` is `/api/tools`: `tools/list` reshaped, cached for the page.
 */

let overviewPromise = null;
let cataloguePromise = null;

async function postJson(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await r.json();
  if (!r.ok) throw new Error(payload.error || `request failed (${r.status})`);
  return payload;
}

async function getJson(url) {
  const r = await fetch(url);
  const payload = await r.json();
  if (!r.ok) throw new Error(payload.error || `request failed (${r.status})`);
  return payload;
}

/* The dataset_overview frame: a tool_result or a tool_refusal, exactly as
 * /api/run returns it. */
export function overview({ refresh = false } = {}) {
  if (!overviewPromise || refresh) {
    overviewPromise = postJson('/api/run', { tool: 'dataset_overview', params: {} })
      .catch((err) => { overviewPromise = null; throw err; });
  }
  return overviewPromise;
}

/* The tool list with schemas, sorted by name, the guard probe last. */
export function catalogue() {
  if (!cataloguePromise) {
    cataloguePromise = getJson('/api/tools')
      .then((payload) => payload.tools || [])
      .catch((err) => { cataloguePromise = null; throw err; });
  }
  return cataloguePromise;
}
