# DATA_NOTES.md

Findings from cleaning the "Goodreads Book Datasets 10M" Kaggle dump
(`bahramjannesarr/goodreads-book-datasets-10m`) and loading it into BigQuery.

**Read this before writing any query tool.** Several fields look usable but
are not, and one obvious join does not exist. Tools built without this
context will return confident, wrong numbers.

---

## Where the data lives

| | |
|---|---|
| Project | `example-project` |
| Dataset | `goodreads` (location: US) |
| Tables | `goodreads.books`, `goodreads.user_ratings` |

Verified row counts after load:

- `books` — **1,850,115**
- `user_ratings` — **357,396**

Both match the cleaning report exactly; no rows were dropped by the load.

---

## Table: `goodreads.books`

| column | type | notes |
|---|---|---|
| `id` | INTEGER | Primary key. **Sparse** — runs to ~5,000,000 with large gaps. Never use `MAX(id)` as a row count. |
| `name` | STRING | Title. Often carries a series suffix, e.g. `(Harry Potter, #6)`. |
| `authors` | STRING | Single string, not a list. Multi-author books are inconsistently delimited. |
| `isbn` | STRING | **Must stay STRING.** Leading zeros are significant; ISBN-10 may end in `X`. 99.7% populated. |
| `rating` | FLOAT | Mean rating 0–5. |
| `publish_year` | INTEGER | **The only reliable temporal field.** |
| `publish_month` | INTEGER | Usable, but see the January caveat below. |
| `publish_day` | INTEGER | **Effectively meaningless — do not use.** See below. |
| `publisher` | STRING | Free text. Not normalised: `Penguin`, `Penguin Books`, `Penguin Classics` are distinct values. |
| `language` | STRING | Raw value as scraped. Inconsistent (`eng`, `en-US`, `en-GB`). |
| `language_normalised` | STRING | Derived. Collapsed to a base ISO code. **Group by this, not `language`.** |
| `pages_number` | INTEGER | 11,217 implausible values were nulled. |
| `rating_dist_1` … `rating_dist_5` | INTEGER | Count of ratings at each star level. Parsed from strings like `"5:1546466"`. |
| `rating_dist_total` | INTEGER | Total ratings. Parsed from `"total:2298124"`. |
| `counts_of_review` | INTEGER | Review count. |
| `count_of_text_reviews` | INTEGER | **Present in only 10 of 23 source files — mostly NULL.** |
| `description` | STRING | **Missing entirely from 6 source files.** Populated for 1,171,240 of 1,850,115 rows (63%). |
| `source_file` | STRING | Which chunk the row came from. Provenance/debugging aid. |

---

## Table: `goodreads.user_ratings`

| column | type | notes |
|---|---|---|
| `user_id` | INTEGER | Only **4,154 distinct users**, averaging ~86 ratings each. |
| `book_title` | STRING | Raw title string. **There is no book ID in this table.** |
| `book_title_normalised` | STRING | Lowercased, series suffix stripped, punctuation removed. Best available join key. |
| `rating_label` | STRING | Original text, e.g. `it was amazing`. |
| `rating` | INTEGER | Mapped 1–5 from the label. |

Label mapping used:

| label | value |
|---|---|
| `it was amazing` | 5 |
| `really liked it` | 4 |
| `liked it` | 3 |
| `it was ok` | 2 |
| `did not like it` | 1 |

4,765 placeholder rows (`This user doesn't have any rating`) were dropped.

---

## Critical caveats

### 1. `publish_day` is 48.25% placeholder — do not build tools on it

`publish_day = 1` for **892,696 of 1,850,115 rows (48.25%)**, with no NULLs.
These are not books published on the 1st; they are records where the true date
was unknown and a placeholder was entered.

**Never** write a tool answering "which day of the month do publishers
prefer" or anything similar. The answer is an artifact.

> **Corrected 2026-08-28.** This section previously read 73.6%. That figure was
> a **denominator error**, not a stale measurement. 73.6% is the placeholder
> rate *within the ambiguous subset* — the rows where `publish_month` and
> `publish_day` were both ≤ 12 and the transposition of caveat 3 could not be
> resolved by inspection (see below). Measured against the loaded table:
>
> | quantity | rows | share |
> |---|---|---|
> | `publish_day = 1` | 892,696 | 48.25% of all rows |
> | ambiguous subset (`publish_month` ≤ 12 AND `publish_day` ≤ 12) | 1,212,960 | 65.56% of all rows |
> | `publish_day = 1` within the ambiguous subset | 892,696 | **73.60% of that subset** |
>
> Every placeholder row falls inside the ambiguous subset, which is why the two
> numerators are identical and only the denominator differs. The original
> analysis divided by 1,212,960 and reported the result as a whole-table share.
>
> Note this also means the *earlier* correction attempt was wrong in a second
> way: the discrepancy has nothing to do with the month/day transposition fix.
> The transposition fix does not move this number at all. Both figures were
> computed post-fix; they simply answer different questions.
>
> The conclusion is unchanged — at 48.25% placeholder the field is still
> unusable. Only the stated rate and its cause are corrected.

### 2. `publish_month` has known January inflation

January holds ~17.7% of rows against a uniform expectation of 8.3%, for the
same placeholder reason (unknown date → January 1). The remaining months show
plausible publishing seasonality (autumn and December peaks, February trough).

`publish_month` is usable for coarse seasonality **if the January inflation is
stated**. Prefer `publish_year` for any serious time-series work.

### 3. Month/day were swapped in 18 of 23 source files

The raw dump had `PublishMonth` and `PublishDay` transposed in 18 files but
correct in 5 (`book1800k-1900k` through `book4000k-5000k` — the same five that
used a capitalised `PagesNumber` header, suggesting the exporter changed
partway through).

This was resolved per row during cleaning: where one value exceeded 12 it must
be the day. Rows where both values were ≤12 (65.6%) were resolved using the
majority verdict of their source file. No file showed contradictory evidence.

**This is already fixed in BigQuery.** Recorded here for methodology only.

### 4. `books` and `user_ratings` cannot be joined on an ID

`user_ratings` carries no book ID — only a title string. The only join
available is on `book_title_normalised` against a normalised `name`.

Measured coverage: **52,016 of 98,686 rated titles (52.7%)** match a book row.

Any tool joining these tables **must** state that it covers roughly half the
rated titles and is matched by title, not identity. Treating the join as
complete will understate rating counts in a way that looks authoritative.

Default guidance: treat the two tables as largely independent. `books` is the
rich table (1.85M rows, many attributes); `user_ratings` is a small, separate
dataset of 4,154 users.

### 5. `publisher` is unnormalised free text

Any "top publishers" tool will fragment the same imprint across many spellings.
Either state this in the tool output or apply grouping/fuzzy matching first.

### 6. NULLs must be excluded explicitly in aggregates

`count_of_text_reviews` (mostly NULL), `description` (37% NULL) and
`pages_number` (11,217 nulled) will silently skew averages. Filter with
`WHERE <col> IS NOT NULL` and report the row count the statistic is based on.

### 7. Ratings-per-book is heavily skewed

Popular titles carry millions of ratings; the long tail carries almost none.
Any "highest rated" tool **must** apply a minimum-ratings threshold (e.g.
`rating_dist_total >= 100`) or it will return obscure books with a single
5-star rating at the top.

---

## Query guidance for MCP tools

- Always `SELECT` named columns; never `SELECT *` on a 1.85M-row table.
- Apply `LIMIT` on anything returning rows to the model.
- Prefer aggregates over row dumps — this is a stats server, not a browser.
- For "best/worst" rankings, always require a minimum `rating_dist_total`.
- Group by `language_normalised`, not `language`.
- Report the underlying `n` alongside any average.
- State the relevant caveat in the tool's own output when one applies, so the
  answer carries its limitation with it rather than relying on the model
  remembering this file.
