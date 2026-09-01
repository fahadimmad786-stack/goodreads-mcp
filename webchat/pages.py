"""
The standalone pages: locked, and not-found.

They are served without the app shell -- no thread, no composer, no JavaScript
-- but they are not bare status text either. Both link the console's own
stylesheet and use its tokens, so a 401 looks like part of the instrument
rather than like the server falling over.

Two rules hold for everything in this module:

  * **The access token is never written into the page.** Not into a link, not
    into a code sample, not into a "try this URL" hint. The whole point of the
    locked page is that the reader does not have the key; echoing it back to
    someone who reached the page without it would be absurd, and echoing it to
    someone who reached it *with* a wrong one would be worse. The placeholder
    is always the literal string `<key>`.
  * **Anything from the request is escaped.** The 404 page names the path that
    missed, and a path is caller-controlled text.
"""

from __future__ import annotations

from html import escape

# Kept identical to index.html's head. Duplicated deliberately rather than
# templated: these pages must render even if the app shell is broken, and
# three lines of markup are cheaper to keep true than a template engine.
_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#fcfcfb" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1a1a19" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='5' y='16' width='5' height='9' rx='1.5' fill='%232a78d6'/%3E%3Crect x='13.5' y='10' width='5' height='15' rx='1.5' fill='%232a78d6'/%3E%3Crect x='22' y='5' width='5' height='20' rx='1.5' fill='%232a78d6'/%3E%3Crect x='4' y='27' width='24' height='2' rx='1' fill='%238b8780'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<main class="page">"""

_FOOT = """</main>
</body>
</html>"""


def _page(title: str, body: str) -> str:
    return f"{_HEAD.format(title=escape(title))}\n{body}\n{_FOOT}"


def locked() -> str:
    """
    401. Explains what `?k=` is, rather than asserting that it is missing.

    Someone reaching this page is either a person who lost the URL or a
    stranger who guessed the host. The first needs to know where the key
    lives; the second learns nothing they could not have guessed, because the
    page contains no key and no hint of one.
    """
    return _page(
        "Access key required · goodreads-stats console",
        """<span class="status">401 · access key required</span>
<h1>This console is private.</h1>

<p>
  It is reachable by anyone with the URL, so it is gated by a shared key rather
  than by a login. Open it with the key appended to the address:
</p>

<p><code>https://&lt;this-host&gt;/?k=&lt;key&gt;</code></p>

<p>
  The key is presented once. After that it lives in an <code>HttpOnly</code>
  cookie for twelve hours, so the plain URL works for the rest of the session
  and the key stops travelling in the address bar.
</p>

<hr>

<p class="note">where the key lives</p>
<ul>
  <li>Deployed: Secret Manager, as <code>chat-access-token</code>.
      <code>gcloud secrets versions access latest --secret=chat-access-token</code></li>
  <li>Local: the gitignored <code>.env</code>, as <code>CHAT_ACCESS_TOKEN</code>.
      <code>./run-local.sh</code> generates one on first run and prints the
      ready-made URL.</li>
</ul>

<p class="note">
  The gate exists because the service pays for what it serves: BigQuery bytes on
  every tool call, and Anthropic tokens too when the chat mode is configured.
</p>""",
    )


def not_found(path: str) -> str:
    """404. Names the path that missed and points at the one page there is."""
    return _page(
        "Not found · goodreads-stats console",
        f"""<span class="status">404 · no such page</span>
<h1>There is nothing here.</h1>

<p>
  This console is a single page, and <code>{escape(path)}</code> is not it.
</p>

<p>
  Its other addresses are a health probe and the two tool routes the page calls
  for itself; none of them are meant to be typed.
</p>

<hr>

<p><a href="/">Back to the console</a></p>
<p class="note">
  If you arrive there and are asked for an access key, that is the same console
  — it has simply forgotten you.
</p>""",
    )
