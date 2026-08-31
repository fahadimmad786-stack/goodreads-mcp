"""
Attaching each caveat to the figure fields it qualifies.

The server sends caveats as rendered prose -- `"[measured] A row in `books` is
an EDITION..."` -- with no machine-readable id, because a tool names ids and
`caveats.collect()` renders them. To place a caveat next to the number it
limits rather than in a footnote, the UI needs the id back, plus a mapping
from id to the fields it applies to.

The id is recovered by exact match against the server's own registry, not by
parsing prose: `collect()` renders deterministically (whitespace collapsed at
registration), so the rendered string is a reliable key. FIELDS is the only
new knowledge in this package, and a test asserts every registry id appears in
it, so adding a caveat to the server fails this service's test suite rather
than silently rendering an unattached caveat.
"""

from __future__ import annotations

# Read-only use of the registry: caveats.py exposes no id->Caveat accessor, and
# reproducing the ids here would be exactly the duplication this module exists
# to avoid. test_registry_reverse_index_covers_every_caveat fails loudly if
# this attribute ever goes away.
from goodreads_mcp.caveats import _REGISTRY

# id -> the envelope/row field names this caveat qualifies.
#
# A field name is matched against row keys, `n` keys, `excluded` keys and
# `filters` keys, so "min_ratings" attaches to the threshold line and
# "pooled_rating" to a table column. An empty tuple means the caveat is about
# the result as a whole and renders at card level.
FIELDS: dict[str, tuple[str, ...]] = {
    # --- measured, absent from DATA_NOTES.md --------------------------------
    "unrated_books": ("min_ratings", "n_books_unrated", "n_books_below_threshold"),
    "language_coverage": ("language_normalised", "n_language_normalised"),
    "edition_duplication": (
        "n_ratings",
        "pooled_rating",
        "editions_per_title",
        "n_edition_rows",
        "n_distinct_titles",
        "book_n_ratings",
    ),
    "work_dedup": ("n_ratings", "n_edition_rows", "editions_per_title", "unit"),
    "title_editions": ("n_editions", "book_n_ratings", "book_pooled_rating"),
    # --- from DATA_NOTES.md -------------------------------------------------
    "rating_skew": ("rating", "avg_book_rating", "pooled_rating", "avg_user_rating"),
    "language_grouping": ("language_normalised",),
    "publisher_unnormalised": ("publisher",),
    "authors_freetext": ("authors",),
    "pages_nulled": ("pages_number", "pages_band", "avg_pages", "n_books_null_pages"),
    "january_inflation": ("month", "n_books_published", "pct_of_books"),
    "publish_year_reliable": ("publish_year", "year_from", "year_to"),
    "title_join": ("title_normalised", "n_matched_titles", "match_coverage_pct"),
    "tables_independent": (
        "user_avg_rating",
        "n_user_ratings",
        "avg_user_rating",
        "n_users",
        "divergence",
    ),
    "text_reviews_sparse": ("count_of_text_reviews",),
    "description_sparse": ("description",),
    "id_sparse": ("id",),
    "publish_day_unusable": ("publish_day", "month"),
    # --- stated by the server itself ---------------------------------------
    "dual_average": ("avg_book_rating", "pooled_rating", "book_pooled_rating"),
}

# rendered text -> id. Built once; render() is pure.
_BY_TEXT: dict[str, str] = {c.render(): cid for cid, c in _REGISTRY.items()}


def structure(rendered: list[str]) -> list[dict]:
    """
    Turn the envelope's caveat strings into structured records for rendering.

    Order is preserved -- the tools name caveats most-relevant-first and that
    ordering is meaningful. An unrecognised string is kept, never dropped: it
    renders at card level with `id: null`, so a caveat can go unattached but
    never invisible.
    """
    out = []
    for text in rendered:
        cid = _BY_TEXT.get(text)
        source, body = _split_source(text)
        out.append(
            {
                "id": cid,
                "source": source,
                "text": body,
                "fields": list(FIELDS.get(cid, ())) if cid else [],
            }
        )
    return out


def _split_source(rendered: str) -> tuple[str, str]:
    """`"[measured] body"` -> `("measured", "body")`."""
    if rendered.startswith("[") and "] " in rendered:
        head, _, rest = rendered.partition("] ")
        return head[1:], rest
    return "", rendered


def caveats_for_column(column: str) -> list[dict]:
    """
    Registry entries that speak to a raw column, for the guard probe.

    Grounded in registry ids, so the probe quotes the server's own prose about
    a column instead of writing new prose about it.
    """
    ids = COLUMN_CAVEATS.get(column, ())
    return structure([_REGISTRY[i].render() for i in ids if i in _REGISTRY])


def caveats_for_param(param: str) -> list[dict]:
    """
    Registry entries that explain why a parameter is constrained.

    Needed because the most instructive refusal in the whole tool surface --
    min_ratings=0 -- is caught by the tool schema before the tool body runs, so
    the server's own explanation of WHY the floor is 1 never reaches the
    caller. This puts the server's prose back, from the registry, rather than
    writing a second explanation here.
    """
    ids = PARAM_CAVEATS.get(param, ())
    return structure([_REGISTRY[i].render() for i in ids if i in _REGISTRY])


# Tool parameter -> the caveat ids that explain its constraint.
PARAM_CAVEATS: dict[str, tuple[str, ...]] = {
    "min_ratings": ("unrated_books", "rating_skew"),
    "min_user_ratings": ("tables_independent",),
    "min_book_ratings": ("unrated_books", "edition_duplication"),
    "unit": ("edition_duplication", "work_dedup"),
    "order_by": ("dual_average",),
    "bucket_size": ("rating_skew",),
    "language": ("language_coverage", "language_grouping"),
    "year_from": ("publish_year_reliable",),
    "year_to": ("publish_year_reliable",),
}


# Raw column -> the caveat ids that describe it. Used only by the guard probe.
COLUMN_CAVEATS: dict[str, tuple[str, ...]] = {
    "publish_day": ("publish_day_unusable",),
    "language": ("language_grouping", "language_coverage"),
    "language_normalised": ("language_coverage", "language_grouping"),
    "publish_month": ("january_inflation", "publish_day_unusable"),
    "publish_year": ("publish_year_reliable",),
    "pages_number": ("pages_nulled",),
    "description": ("description_sparse",),
    "count_of_text_reviews": ("text_reviews_sparse",),
    "id": ("id_sparse",),
    "publisher": ("publisher_unnormalised",),
    "authors": ("authors_freetext",),
    "rating": ("rating_skew", "unrated_books"),
}
