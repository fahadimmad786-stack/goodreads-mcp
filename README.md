# goodreads-mcp

An MCP server answering statistical questions about the Goodreads dataset in
BigQuery (`example-project.goodreads`), built on FastMCP and
google-cloud-bigquery with Application Default Credentials.

The dataset has defects that produce confident wrong numbers. This server is
built so that a caller who never reads `DATA_NOTES.md` still cannot get one:
the rules are enforced in code, and the relevant caveat travels with every
figure returned.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
gcloud auth application-default login   # if ADC is not already set up
```

Register with Claude Code:

```bash
claude mcp add goodreads -- /home/safilo/projects/goodreads-mcp/.venv/bin/python \
    -m goodreads_mcp
```

Environment overrides: `GOODREADS_BQ_PROJECT`, `GOODREADS_BQ_DATASET`,
`GOODREADS_BQ_LOCATION`, `GOODREADS_MAX_BYTES_BILLED` (default 20 GiB).

## Tools

Twelve tools, all aggregate. There is no row browser.

| tool | answers |
|---|---|
| `dataset_overview` | shape, live column coverage, every known defect |
| `rating_distribution` | histogram of book ratings + pooled star share |
| `top_books_by_rating` | best/worst books above a ratings threshold — `unit` |
| `stats_by_language` | ratings grouped by `language_normalised` — `unit` |
| `stats_by_year` | ratings and volume per publication year — `unit` |
| `stats_by_publisher` | ratings grouped by publisher string — `unit` |
| `stats_by_author` | ratings grouped by author string — `unit` |
| `page_count_stats` | book length against rating |
| `publish_month_seasonality` | coarse monthly seasonality |
| `user_ratings_overview` | shape of the 4,154-user panel |
| `top_titles_by_user_ratings` | best/worst liked titles in the panel |
| `compare_user_vs_book_ratings` | where the panel disagrees with Goodreads |

Every tool returns the same envelope:

```json
{"data": ..., "n": {...}, "excluded": {...}, "filters": {...},
 "caveats": ["[measured] ...", "[DATA_NOTES.md #7] ..."], "query_meta": {...}}
```

`n` is mandatory and keyword-only in `queries.envelope()` — no average leaves
this server without the count it rests on, and `excluded` reports what the
threshold removed to get there.

## Editions or works: the `unit` parameter

A row in `books` is an **edition**, and the five tools marked `unit` above take
`unit="editions"` (default, except `stats_by_author`) or `unit="works"`.

Which is right depends on the question. *Most prolific publisher* wants
editions — issuing five editions is five editions of work. *Most-read author*
wants works — a novel should not count five times because it has five
editions. `stats_by_author` therefore defaults to `"works"`; everything else
defaults to `"editions"`, which preserves the numbers those tools returned
before this option existed.

Under `"works"`, editions sharing a normalised title collapse to **one
representative row** — the edition carrying the most ratings — before
aggregating. Three things to know about that:

1. **`n_ratings` is a floor, not an exact work total.** Editions repeat most of
   their work's rating pool but not exactly: of the 68,921 works with more
   than one edition at ≥100 ratings, **62,794 (91%) have editions whose
   totals differ**, mean relative spread 8.6%. Summing would overcount
   and taking the maximum undercounts. The maximum is used.
2. **Deduplication is within a group, not across groups.** A work issued by two
   publishers survives once in each, so group totals still do not sum to a
   corpus total.
3. **Works are identified by title text alone**, so the collapse still errs in
   both directions — see *Two title keys* and *Why `authors` is not in the
   key* below. No work key merges two distinct series ranges any more, but
   7,749 keys (11.2%) span more than one author string, and editions titled
   differently stay split (`Calvin And Hobbes: It's a Magical World` vs
   `Calvin & Hobbes: ...`).

Both branches emit identical column names, so `order_by` and the envelope are
unaffected by the choice. Grouped results add `n_edition_rows` under `"works"`
so the size of the collapse is visible; `n_books` counts works.

### Two title keys

`queries.py` has two title normalisers, and they are not interchangeable:

| | used for | series suffix |
|---|---|---|
| `title_norm()` | the `user_ratings` join **only** | stripped entirely |
| `work_key()` | all work-level dedup and `n_distinct_titles` | **range kept**, volume number dropped |

`title_norm()` must reproduce the cleaning script's `book_title_normalised`
exactly or the documented 52,016-title join coverage breaks, so it cannot
change. But stripping the whole suffix is wrong as a *work identity*: it
deletes the only thing separating one boxed set from another, so
`Harry Potter Boxed Set (Harry Potter, #1-5)` and `(#1-7)` collapsed together
and the most-rated of the merged group displaced all four from a top-rated
list.

`work_key()` keeps a **range** (`#1-5`) and drops a **single volume number**
(`#2`). Only ranges denote a different product; a volume number is series
metadata about the same work, and keeping it would split an edition carrying
the suffix from one without it — a regression `title_norm()` did not have.
`The Lion, the Witch and the Wardrobe` is numbered both `#1` and `#2`
depending on publication vs chronological order, and must stay merged.

Effect at ≥100 ratings: work keys merging two distinct ranges went **3 → 0**,
and a further 92 keys that mixed a ranged title with a bare one now split.

### Why `authors` is not in the key

7,749 of the 68,921 multi-edition work keys (11.2%) span more than one author
string, which looks like an argument for adding `authors` to the key. It was
measured and rejected. Two kinds of case are mixed together and **cannot be
separated from these fields**:

| | example | splitting on author is |
|---|---|---|
| different books, same title | `Twilight` — Meyer, Erin Hunter, Christie Golden, Elie Wiesel | correct |
| one work, adapter credited | `Pride and Prejudice` — Jane Austen + graded-reader adapters Clare West, Diana Stewart, Evelyn Attwood | **wrong** |

Two heuristics were tried and both failed. Author-token overlap classifies
92.9% as "disjoint authors", but its own highest-rated members are
`Pride and Prejudice`, `1984`, `Animal Farm` and `The Lion, the Witch and the
Wardrobe` — all the adapter case, all misclassified. Rating dominance does no
better: the top author's share of a key's ratings spreads smoothly across the
range (14.1% of keys ≥95%, 36.8% below 60%), with no threshold separating the
two kinds.

Adding `authors` would therefore split adaptations of single works, and those
are concentrated in exactly the heavily-rated classics that dominate rankings.
Against that, the benefit is small and shrinking:

- **`stats_by_author` is structurally immune** — it groups by author *before*
  collapsing, so a collision can never cross authors there. This is the tool
  the `works` unit was added for.
- Grouping absorbs most of the rest: only **756** (work key, publisher) groups
  and **1,913** (work key, year) groups merge more than one author string.
- `top_books_by_rating` is the only tool partitioning on `work_key` alone, and
  exposure falls with the threshold — 11.2% of keys at `min_ratings=100`, 4.0%
  at 5,000, and **0 of the top 50 works at 5,000**.

## Enforced rules

These are guards and validators, not conventions. `tests/test_guards.py`
covers each one.

- **`publish_day` is never read.** `bq.guard()` rejects any query mentioning
  it before the query reaches BigQuery, and a test asserts no SQL string in
  the codebase contains it.
- **Grouping is on `language_normalised`.** The guard rejects the bare
  `language` column; the regex leaves `language_normalised` alone because
  `\b` does not match between `language` and `_normalised`.
- **`SELECT *` is rejected** on a 1.85M-row table.
- **Every ranking takes `min_ratings`, floored at 1**, not 0 — see below.
- **`order_by` and `direction` are whitelisted**, never interpolated. All
  filter values are BigQuery named parameters.
- **Row output is capped** (100, or 200 for per-year series) and every query
  is cost-capped via `maximum_bytes_billed`.

## Caveats are code, not prose

`caveats.py` is a registry keyed by id. Tools name the ids belonging to the
code path they took; they never write caveat text inline. An unknown id raises
rather than silently omitting a warning. Each caveat is tagged with its
source — `[DATA_NOTES.md #n]` where the notes document it, `[measured]` where
this project found it by profiling the loaded tables.

## Four defects found by profiling that DATA_NOTES.md does not mention

1. **451,777 books (24.4%) have no ratings and are stored as `rating = 0.0`.**
   Not books rated zero. A naive `AVG(rating)` is dragged toward zero by a
   quarter of the table. This is why `min_ratings` is floored at 1 rather
   than 0 — the floor excludes them structurally, and `require_min_ratings`
   explains why when it rejects 0.

2. **`language_normalised` is populated for only 13.6% of rows**, and 83% of
   that labelled slice is English. Any language grouping is a statement about
   a small, English-dominated subsample.

3. **A row is an *edition*, and each edition repeats most of its work's rating
   total.** Crichton's *The Lost World* is five rows each holding ~117,000
   ratings and an identical 3.78. The repetition is near-total but not exact —
   62,794 of 68,921 multi-edition works (91%) have editions whose totals
   differ, mean spread 8.6%, so no exact work total is recoverable. So
   `SUM(rating_dist_total)` overcounts —
   7,529,817,002 against 3,148,039,676 deduplicated by work, a 2.4×
   overcount — and `pooled_rating` over-weights works with many editions.
   Every grouped result reports `n_distinct_titles` and `editions_per_title`
   so the size of the effect is visible for that specific group. (It is
   usually mild within a publisher, ~1.05–1.22, and larger across the corpus.)

4. **`publish_day = 1` for 48.25% of rows** (892,696 of 1,850,115), and the
   column has no NULLs. DATA_NOTES.md #1 previously said 73.6%; that was a
   denominator error, now corrected — 73.60% is the placeholder rate *within
   the 1,212,960-row ambiguous subset* of caveat 3, not within the whole
   table. Nothing to do with the transposition fix. The column stays banned
   either way.

## Two averages, always

Every rating aggregate returns both, because they answer different questions
and diverge whenever a group mixes blockbusters with long-tail titles:

- `avg_book_rating` — mean of each book's own mean; every book counts once.
- `pooled_rating` — total stars ÷ total ratings; popular books dominate.

`rating_dist_1..5` sums exactly to `rating_dist_total` on all 1,850,115 rows,
so the pooled figure is exact rather than reconstructed.

## The one cross-table tool

`user_ratings` has no book ID, so `compare_user_vs_book_ratings` joins on
normalised title text, reproducing the cleaning script's `normalise_title` in
SQL. It uses `[^\p{L}\p{N}_\s]` rather than `[^\w\s]` because RE2's `\w` is
ASCII-only while Python's is Unicode-aware — with the Unicode classes it
reproduces the documented coverage of 52,016 of 98,686 titles exactly; with
`\w` it loses 922 matches. Editions are pooled per title before joining.

Confining the join to one tool keeps its 52.7% coverage caveat from leaking
into eleven tools that would otherwise look authoritative.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q        # 42 offline invariant tests
PYTHONPATH=. .venv/bin/python tests/smoke_live.py   # 18 live calls, needs ADC
```
