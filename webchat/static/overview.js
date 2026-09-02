/* The Overview: live counts and the starting points.
 *
 * The three tiles are the `n` block of dataset_overview's envelope, shown as
 * the server sent them. Nothing here is computed, and no figure appears until
 * the tool has returned it -- the tiles render as pending dashes first, so a
 * slow BigQuery round trip is visible rather than blank.
 */

import { overview } from './data.js';
import { node } from './cards.js';
import { fmt } from './charts.js';

const tilesBox = document.getElementById('overview-tiles');

/* Which `n` keys to show, and what one of each counts. The unit text is the
 * server's own: a row in `books` is an edition; `user_ratings` is a panel. */
const TILES = [
  ['n_books', 'editions in books'],
  ['n_user_ratings', 'ratings in user_ratings'],
  ['n_users', 'users in the panel'],
];

let started = false;

export function initOverview() {
  if (started) return;
  started = true;

  tilesBox.replaceChildren();
  const tiles = new Map();
  for (const [key, label] of TILES) {
    const tile = node('div', 'tile pending');
    tile.appendChild(node('div', 'value', '—'));
    tile.appendChild(node('div', 'label', label));
    tile.appendChild(node('div', 'of', 'fetching dataset_overview…'));
    tilesBox.appendChild(tile);
    tiles.set(key, tile);
  }

  overview().then((frame) => {
    if (frame.type !== 'tool_result') {
      fail(tiles, frame.message || 'dataset_overview was refused');
      return;
    }
    const env = frame.envelope || {};
    const n = env.n || {};
    const qm = env.query_meta || {};
    const cost = qm.queries
      ? `${qm.queries} queries · ${qm.cache_hits ?? 0}/${qm.queries} cached`
      : 'live';
    for (const [key, tile] of tiles) {
      tile.classList.remove('pending');
      tile.querySelector('.value').textContent = fmt(n[key]);
      tile.querySelector('.of').textContent = `${key} · dataset_overview · ${cost}`;
    }
  }).catch((err) => fail(tiles, err.message || 'could not reach the console backend'));
}

function fail(tiles, message) {
  for (const tile of tiles.values()) {
    tile.classList.remove('pending');
    tile.querySelector('.value').textContent = '—';
    tile.querySelector('.of').textContent = `unavailable: ${message}`;
  }
}
