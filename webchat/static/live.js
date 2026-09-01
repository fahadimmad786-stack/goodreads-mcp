/* The live region: what the thread is doing, for a reader not watching it.
 *
 * Its own module rather than a helper on app.js, because tools.js needs it
 * too and app.js imports tools.js -- putting it there would make the two
 * modules a cycle. Nothing else belongs in here.
 *
 * Deliberately terse. The card itself is in the document and can be read at
 * leisure; this only has to say that something happened, so it never repeats
 * a figure. `role="status"` is implicitly aria-live="polite", so an
 * announcement waits for a pause rather than interrupting.
 */

const region = document.getElementById('live');

export function announce(message) {
  if (region) region.textContent = message;
}
