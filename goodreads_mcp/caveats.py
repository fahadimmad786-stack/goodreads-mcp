"""
Caveat registry.

Every caveat the tools can emit lives here exactly once, keyed by id. Tools
never write caveat prose inline -- they name the ids that apply to the code
path they took, and `collect()` renders them. That way a caveat cannot be
forgotten by a tool author who didn't reread DATA_NOTES.md.

`source` records where the caveat came from:
  - "DATA_NOTES.md #n"  -- documented in the notes
  - "measured"          -- found by profiling the loaded tables, NOT in the
                           notes. These are the dangerous ones; the notes give
                           no warning about them at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Caveat:
    id: str
    source: str
    text: str

    def render(self) -> str:
        return f"[{self.source}] {self.text}"


_REGISTRY: dict[str, Caveat] = {}


def _add(id: str, source: str, text: str) -> None:
    _REGISTRY[id] = Caveat(id=id, source=source, text=" ".join(text.split()))


# --- Caveats measured from the loaded tables, absent from DATA_NOTES.md ------

_add(
    "unrated_books",
    "measured",
    """451,777 books (24.4% of the table) have no ratings at all and are stored
    as rating = 0.0 with rating_dist_total = 0. They are NOT books rated zero.
    Any average over the raw `rating` column that does not filter them out is
    dragged toward zero by a quarter of the table. Every rating statistic from
    this server applies a min_ratings threshold of at least 1, which excludes
    them structurally.""",
)

_add(
    "language_coverage",
    "measured",
    """language_normalised is populated for only 251,733 of 1,850,115 books
    (13.6%). Within that labelled subset English is 209,596 books (83%). Any
    result grouped by language describes a small, English-dominated subsample,
    not the corpus. Non-English groups get thin quickly (it = 1,156 books,
    pt = 406), so treat small-group averages as noisy.""",
)

_add(
    "edition_duplication",
    "measured",
    """A row in `books` is an EDITION, and each edition repeats most or all of
    its work's Goodreads rating total rather than holding only its own.
    Crichton's "The Lost World" appears as five rows each holding ~117,000
    ratings and an identical 3.78. The repetition is near-total but not exact:
    of the 68,921 works with more than one edition at >=100 ratings, 62,794
    (91%) have editions whose totals differ, mean relative spread 8.6%. Two
    consequences: under unit="editions" n_ratings double-counts (summed over
    books with >=100 ratings it gives 7,529,817,002 against 3,148,039,676
    deduplicated by work, a 2.4x overcount), and pooled_rating over-weights
    works that have many editions.
    Read n_ratings as "ratings summed across edition rows", not as distinct
    ratings. Each group reports n_distinct_titles and editions_per_title so
    the size of the effect is visible per row; editions_per_title near 1.0
    means the group is barely affected. avg_book_rating is also affected, but
    far less: it counts each edition once rather than weighting by a
    duplicated total. Pass unit="works" to collapse editions instead.""",
)

_add(
    "work_dedup",
    "measured",
    """This result used unit="works": editions sharing a normalised title were
    collapsed to ONE representative row before aggregating -- the edition
    carrying the most ratings, ties broken by lowest id. Three limits on
    reading it. (1) The work total is approximate. Editions repeat most of
    their work's rating pool but not exactly: 62,794 of the 68,921
    multi-edition works (91%) have editions whose totals differ, mean spread
    8.6%. Summing would overcount and taking the maximum undercounts; the
    maximum is used, so n_ratings here is a floor, not an exact work total. (2) Deduplication is WITHIN a group,
    not across groups. A work issued by two publishers, or in two years,
    survives once in each of those groups, so group totals still do not sum to
    a corpus total. (3) Works are identified by title text alone, so the
    collapse still errs in both directions. The key is work_key(), NOT the
    title_norm() used for the cross-table join: it keeps a series RANGE, so
    "Harry Potter Boxed Set (Harry Potter, #1-5)" and "... (#1-7)" stay
    separate products, while a single volume number is dropped so "The Two
    Towers (The Lord of the Rings, #2)" still merges with a bare "The Two
    Towers". No work key now merges two distinct ranges. What remains:
    7,749 of the 68,921 multi-edition keys (11.2%) span more than one author
    string. Some are genuine collisions of different books sharing a title
    ("Twilight" merges Meyer, Erin Hunter, Christie Golden and Elie Wiesel);
    others are one work whose adapter or abridger is credited as the author
    ("Pride and Prejudice" merges Jane Austen with the graded-reader adapters
    Clare West, Diana Stewart and Evelyn Attwood). The two are not separable
    from these fields, so `authors` is deliberately NOT part of the key --
    adding it would split the adaptations, which are concentrated in the
    heavily-rated classics. Exposure falls as the threshold rises: 11.2% of
    keys at min_ratings=100, 4.0% at 5,000, and 0 of the top 50 works at
    5,000. stats_by_author is unaffected either way, because it groups by
    author before collapsing, so a collision can never cross authors there.
    And editions titled differently stay split, so "Calvin And Hobbes: It's a
    Magical World", "Calvin & Hobbes: It's a Magical World" and "It's a
    Magical World (Calvin and Hobbes, #11)" remain three separate works.
    n_books counts works and n_edition_rows reports how many table rows they
    came from.""",
)

_add(
    "title_editions",
    "measured",
    """A single normalised title can map to as many as 36 rows in `books`
    (separate editions of the same work). Editions are pooled into one row per
    title before joining, so rating counts here are summed across editions and
    will not match any single edition's Goodreads page.""",
)

# --- Caveats documented in DATA_NOTES.md ------------------------------------

_add(
    "rating_skew",
    "DATA_NOTES.md #7",
    """Ratings per book are extremely skewed: the median book has 5 ratings,
    the 90th percentile has 523, and the maximum is 7,094,687. Without a
    minimum-ratings threshold a "highest rated" list returns obscure books
    holding a single 5-star rating. Raise min_ratings to tighten the result.""",
)

_add(
    "language_grouping",
    "DATA_NOTES.md guidance",
    """Grouped by language_normalised (base ISO code), not the raw `language`
    column, which fragments eng / en-US / en-GB into separate groups.""",
)

_add(
    "publisher_unnormalised",
    "DATA_NOTES.md #5",
    """`publisher` is unnormalised free text with 79,423 distinct values. The
    same imprint fragments across many spellings -- Penguin alone appears as
    "Penguin Books" (6,723 books), "Penguin Classics" (1,769), "Penguin"
    (1,380), "Penguin Books Ltd" (672), "Penguin Group" (359) and more. These
    rows are NOT combined. A publisher's true output is spread across several
    rows below, so any single row understates it.""",
)

_add(
    "authors_freetext",
    "DATA_NOTES.md books.authors",
    """`authors` is a single free-text string, not a list, and multi-author
    books are inconsistently delimited across 675,289 distinct values. A row
    here is an author *string*, so co-authored works form their own groups
    rather than counting toward each individual author.""",
)

_add(
    "pages_nulled",
    "DATA_NOTES.md #6",
    """11,216 implausible pages_number values were nulled during cleaning and
    are excluded here. n_excluded reports how many rows this dropped.""",
)

_add(
    "january_inflation",
    "DATA_NOTES.md #2",
    """publish_month carries known January inflation: January holds 17.72% of
    rows against a uniform expectation of 8.3%, because records with an unknown
    date were entered as January 1. The January figure below is inflated and
    should not be read as a real publishing peak. The remaining months show
    plausible seasonality (autumn and December peaks, February trough). For
    serious time-series work use publish_year instead.""",
)

_add(
    "publish_year_reliable",
    "DATA_NOTES.md books.publish_year",
    """publish_year is the only reliable temporal field in this dataset.""",
)

_add(
    "title_join",
    "DATA_NOTES.md #4",
    """`user_ratings` carries no book ID, so this joins on normalised title
    text, not identity. Only 52,016 of 98,686 rated titles (52.7%) match a book
    row -- roughly half the user ratings have no book to join to and are absent
    below. Titles that do match may still be matched wrongly where two
    different works normalise to the same string. Do not read these counts as
    complete coverage of either table.""",
)

_add(
    "tables_independent",
    "DATA_NOTES.md #4",
    """`books` (1,850,115 rows) and `user_ratings` (357,396 ratings from 4,154
    users) are largely independent datasets, not two views of one population.
    The user_ratings figures describe those 4,154 users only and do not
    generalise to Goodreads or to the books table.""",
)

_add(
    "text_reviews_sparse",
    "DATA_NOTES.md #6",
    """count_of_text_reviews is present in only 10 of 23 source files and is
    NULL for 1,440,418 of 1,850,115 rows (77.9%).""",
)

_add(
    "description_sparse",
    "DATA_NOTES.md #6",
    """`description` is missing entirely from 6 source files; it is populated
    for 1,171,240 of 1,850,115 rows (63.3%).""",
)

_add(
    "id_sparse",
    "DATA_NOTES.md books.id",
    """`id` is sparse, running to ~5,000,000 with large gaps. Row counts come
    from COUNT(*), never MAX(id).""",
)

_add(
    "publish_day_unusable",
    "DATA_NOTES.md #1",
    """publish_day is a placeholder for 892,696 of 1,850,115 rows (48.25%,
    no NULLs) and is unusable. This server exposes no tool that reads it, and
    a guard rejects any query referencing it. (DATA_NOTES.md #1 previously
    stated 73.6%; that was a denominator error -- 73.60% is the placeholder
    rate within the 1,212,960-row ambiguous subset of caveat 3, not within the
    whole table. Corrected in the notes.)""",
)

_add(
    "dual_average",
    "server",
    """Two averages are reported because they answer different questions.
    avg_book_rating is the mean of each book's own mean rating, counting every
    book once regardless of popularity. pooled_rating is the total stars
    divided by the total ratings, so heavily-rated books dominate. They diverge
    whenever a group mixes blockbusters with long-tail titles.""",
)


def collect(*ids: str) -> list[str]:
    """Render the named caveats, in the order given. Unknown id is a bug."""
    out: list[str] = []
    seen: set[str] = set()
    for i in ids:
        if i not in _REGISTRY:
            raise KeyError(f"unknown caveat id: {i!r}")
        if i in seen:  # a tool naming the same caveat twice states it once
            continue
        seen.add(i)
        out.append(_REGISTRY[i].render())
    return out


def all_caveats() -> list[str]:
    return [c.render() for c in _REGISTRY.values()]
