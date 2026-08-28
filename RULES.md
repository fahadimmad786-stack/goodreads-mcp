# RULES.md

Analysis conduct rules for the Goodreads BigQuery dataset.

`DATA_NOTES.md` records **what the defects are**. This file says **what you may
do about them**. It governs every figure that leaves this project: MCP tool
output, ad-hoc `bq` queries, charts, and sentences in reports.

A caveat is not a licence. Several rules below are absolute — no wording,
footnote, or disclaimer makes the underlying figure correct.

**All figures in this file were measured against the live tables on
2026-08-28.** Provenance is given for each. Do not copy figures here from
`DATA_NOTES.md`, `README.md` or `caveats.py` — figures in all three have
turned out to be wrong. Re-measure.

---

## 1. Hard bans

Never correct, regardless of caveats.

### 1.1 `publish_day` is unusable

`publish_day = 1` for **892,696 of 1,850,115 rows (48.25%)**, with **zero
NULLs**. These are not books published on the 1st; the true date was unknown
and a placeholder was written.

No analysis may read this column — not for day-of-month questions, not as a
tiebreaker, not to construct a full `DATE`. `bq.guard()` rejects any query
mentioning it, and that guard is not to be worked around.

> Measured: `COUNTIF(publish_day = 1)`, `COUNTIF(publish_day IS NULL)` over `books`.

### 1.2 Never group by `language`; use `language_normalised`

Raw `language` fragments the same language across `eng`, `en-US`, `en-GB`.
Every grouping, filter and join on language uses `language_normalised`.

Note both columns are equally *sparse* — raw is populated for 251,746 rows and
normalised for 251,733, a difference of 13 rows (all raw value `"--"`).
Switching to the raw column to "get better coverage" gains nothing and breaks
the grouping. See §3.4 for what the low coverage means.

> Measured: `COUNTIF(language IS NOT NULL)` = 251,746; `COUNTIF(language_normalised IS NOT NULL)` = 251,733.

### 1.3 Never `AVG(rating)` without excluding unrated books

**451,777 books (24.42%)** have `rating_dist_total = 0`, and **all 451,777** of
them store `rating = 0.0`. They are not books rated zero — they are books with
no ratings. Including them drags any mean toward zero across a quarter of the
table.

Every rating average must filter `rating_dist_total >= 1` at minimum, and in
practice a real threshold (§1.5).

> Measured: `COUNTIF(rating_dist_total = 0)` = 451,777; `COUNTIF(rating_dist_total = 0 AND rating = 0.0)` = 451,777.

### 1.4 Never present `SUM(rating_dist_total)` as a rating count

A row in `books` is an **edition**, and each edition repeats most of its work's
rating pool. Summing over editions double-counts:

| over books with ≥100 ratings | ratings |
|---|---|
| `SUM(rating_dist_total)` across edition rows | 7,529,817,002 |
| summed after collapsing to one row per work | 3,148,039,676 |
| overcount factor | **2.392×** |

`SUM(rating_dist_total)` may be reported only as *"ratings summed across
edition rows"*. It may never be called "ratings", "reviews", or "readers".

> Measured: work key per §4.2, `SUM(SUM(rt))` vs `SUM(MAX(rt))` grouped by work key, `rating_dist_total >= 100`.

### 1.5 Never rank by rating without a minimum-ratings threshold

Ratings per book are extreme: the median book has **5** ratings, the 90th
percentile **524**, the maximum **7,094,687**. Without a threshold, any
"highest rated" list returns obscure books holding a handful of 5-star
ratings.

Every ranking states its threshold and how many books it excluded. The floor is
1, never 0 (that would readmit §1.3). The project default is 100, which retains
**342,370 of 1,850,115 books (18.51%)**.

> Measured: `PERCENTILE_DISC(rating_dist_total, 0.5/0.9)` — **exact**, not `APPROX_QUANTILES`, which returned 527 for p90 on the same data. See §6.4.

---

## 2. Three things every figure must carry

A figure without all three is not publishable, in any medium.

**2.1 The n behind it.** Every average, rate and ratio states the row count it
rests on. "Average rating 4.02" is not a finding; "average rating 4.02 across
342,370 books with ≥100 ratings" is.

**2.2 The unit — editions or works.** A row in `books` is an edition, not a
book and not a work. Every count says which it is. "1,850,115 books" is wrong;
it is 1,850,115 **edition rows**. Never let a reader assume.

**2.3 What the threshold excluded.** State the filter and its cost together:
how many rows were in scope, how many the threshold removed, how many were
unrated. A ranking over 18.51% of the table that does not say so implies
coverage it does not have.

The MCP server enforces all three structurally — `queries.envelope()` makes `n`
mandatory and every tool reports `excluded`. Ad-hoc work has no such
enforcement; you supply them by hand (§5).

---

## 3. Field-specific rules

### 3.1 `publish_year` is the only reliable temporal field

Use it for any time series. Range is 1192–2022; treat the early tail as
data entry noise rather than evidence.

### 3.2 `publish_month` requires the January caveat, stated inline

January holds **17.72%** of rows with a non-null month against a uniform
expectation of 8.3%, because unknown dates were recorded as January 1. The
January figure is an artifact.

`publish_month` is admissible only for coarse seasonality, only with the
inflation stated in the same sentence or chart, and never as the basis of a
trend claim. Prefer `publish_year`.

> Measured: `COUNTIF(publish_month = 1) / COUNT(*)` where `publish_month IS NOT NULL`.

### 3.3 `publisher` is unnormalised free text

**79,423 distinct values.** One imprint fragments across many spellings:

| publisher string | rows |
|---|---|
| Penguin Books | 6,723 |
| Penguin Classics | 1,769 |
| Penguin | 1,380 |
| Penguin Books Ltd | 672 |
| Penguin Group | 359 |
| Penguin Global | 316 |

A "top publishers" result ranks **spellings, not publishers**, and every row is
a lower bound on that imprint's true output. Say so, or merge the variants
explicitly and show the merge rule.

> Measured: `COUNT(DISTINCT publisher)`; `GROUP BY publisher WHERE LOWER(publisher) LIKE '%penguin%'`.

### 3.4 `language_normalised` coverage is low and English-dominated

Populated for **251,733 of 1,850,115 rows (13.61%)**, across 118 distinct
codes. Within the labelled subset, English is **209,596 books — 83.26%**.
Non-English groups thin out fast: Italian 1,156, Portuguese 406.

Any language-grouped result describes a small, English-dominated subsample. It
is never a statement about the corpus, and small-group averages are noisy.
Never report a language share as a share of all books.

> Measured: `COUNTIF(language_normalised IS NOT NULL)`, `COUNTIF(language_normalised = 'en'/'it'/'pt')`, `COUNT(DISTINCT language_normalised)`.

### 3.5 NULLs are excluded explicitly, and the cost is stated

Three columns are sparse enough to distort any aggregate that ignores them:

| column | NULL rows | share NULL |
|---|---|---|
| `count_of_text_reviews` | 1,440,418 | 77.86% |
| `description` | 678,875 | 36.69% |
| `pages_number` | 11,216 | 0.61% |

Filter with an explicit `IS NOT NULL` — never rely on aggregate functions
skipping NULLs silently — and report the count the statistic rests on.

> Measured: `COUNTIF(<col> IS NULL)` per column over `books`.

### 3.6 `id` is sparse — never use it as a count

`id` runs to **4,846,451** against **1,850,115** rows. `MAX(id)` is not a row
count and `id` ranges are not samples. Count with `COUNT(*)`.

> Measured: `MAX(id)` vs `COUNT(*)`.

### 3.7 `isbn` must stay STRING

Populated for 1,844,121 rows (99.68%). Leading zeros are significant and
ISBN-10 may end in `X`. Never cast to a numeric type, in SQL, pandas, or a
spreadsheet — it silently destroys valid identifiers.

### 3.8 `rating_dist_1..5` reconciles exactly

`rating_dist_1 + … + rating_dist_5 = rating_dist_total` on **all 1,850,115
rows — zero mismatches**. A pooled (star-weighted) average is therefore exact,
not reconstructed. If a future load breaks this, pooled averages stop being
trustworthy; re-check before relying on one.

> Measured: `COUNTIF(rating_dist_1+…+rating_dist_5 != rating_dist_total)` = 0.

---

## 4. Work-level deduplication

### 4.1 `n_ratings` under `unit="works"` is a floor, not an exact total

Editions repeat their work's rating pool, but **not exactly**. Of the **68,921**
works with more than one edition at ≥100 ratings, **62,794 (91.1%)** have
editions whose totals differ, mean relative spread **8.56%**.

So no exact work total is recoverable from this data: summing overcounts,
taking the maximum undercounts. The collapse takes the **maximum**. Report
`n_ratings` under `works` as a lower bound, never as "the number of ratings
this work has".

> Measured: per work key at `rating_dist_total >= 100`, `COUNTIF(MAX(rt) != MIN(rt))` and `AVG((MAX(rt)-MIN(rt))/MAX(rt))`.

### 4.2 What the work key merges, and what it does not

Two title normalisers exist and are **not interchangeable**:

| | used for | series suffix |
|---|---|---|
| `title_norm()` | the `user_ratings` join **only** | stripped entirely |
| `work_key()` | all work-level dedup | **range kept** (`#1-5`), volume number dropped (`#2`) |

`title_norm()` reproduces the cleaning script's `book_title_normalised` and must
not change — the join coverage below depends on it exactly.

`work_key()` keeps a **range** because a range denotes a different product, and
drops a **volume number** because that is series metadata about the same work.
Effect at ≥100 ratings: keys merging two distinct ranges went **3 → 0**, and a
further **92** keys that mixed a ranged title with a bare one now split.

It still errs in both directions, and every work-level figure inherits this:

- **Over-merges.** 7,749 of 68,921 multi-edition keys (11.24%) span more than
  one author string — see §4.3.
- **Under-merges.** Editions titled differently stay separate: `Calvin And
  Hobbes: It's a Magical World`, `Calvin & Hobbes: It's a Magical World` and
  `It's a Magical World (Calvin and Hobbes, #11)` are three works, not one.

A single work key covers as many as **36** edition rows.

> Measured: work key = `title_norm` with a trailing `#a-b` range re-appended; range/bare/multi-range key counts by `GROUP BY … HAVING COUNT(DISTINCT range) > 1`.

### 4.3 `authors` is deliberately excluded from the work key

**Do not add it without re-running the measurement that justified leaving it
out.** The reasoning, not just the conclusion:

7,749 keys span multiple author strings, which looks like an argument for
including `authors`. It is not, because that population mixes two cases that
**cannot be separated from these fields**:

| case | example | splitting on author would be |
|---|---|---|
| different books, same title | `Twilight` — Meyer, Erin Hunter, Christie Golden, Elie Wiesel | correct |
| one work, adapter credited | `Pride and Prejudice` — Jane Austen plus graded-reader adapters Clare West, Diana Stewart, Evelyn Attwood | **wrong** |

Two heuristics were tried and both failed. Author-token overlap labels 92.9% of
them "disjoint authors", but its own highest-rated members are `Pride and
Prejudice`, `1984`, `Animal Farm` and `The Lion, the Witch and the Wardrobe` —
all the adapter case, all misclassified. Rating dominance does no better: the
top author's share spreads smoothly (14.1% of keys ≥95%, 36.8% below 60%) with
no separating threshold.

Meanwhile the benefit is small and shrinking:

- `stats_by_author` is **structurally immune** — it groups by author before
  collapsing, so a collision can never cross authors there. That is the tool
  the `works` unit exists for.
- Grouping absorbs most of the rest: only **756** (work key, publisher) groups
  and **1,913** (work key, year) groups merge more than one author string.
- Exposure falls with the threshold, and does not reach answers:

| `min_ratings` | keys spanning authors | of multi-edition keys |
|---|---|---|
| 100 | 7,749 | 11.24% |
| 1,000 | 2,108 | 6.86% |
| 5,000 | 560 | 4.04% |

**0 of the top 50 works at `min_ratings=5000` span multiple author strings.**

> Measured: `COUNT(DISTINCT authors)` per work key at each threshold; per (work key, publisher) and (work key, year); top-50 by rating after collapsing to the max-rated edition per work key.

### 4.4 The cross-table join is by title, and covers about half

`user_ratings` carries no book ID. The only join is `book_title_normalised`
against `title_norm(name)`, and it matches **52,016 of 98,686 rated titles
(52.71%)**.

Any joined figure must state that it is matched by title rather than identity
and reaches roughly half the rated titles. Titles that match may still match
the wrong work. The two tables are otherwise independent populations —
`user_ratings` is **357,396 ratings from 4,154 users** and does not generalise
to the books table or to Goodreads.

> Measured: `DISTINCT book_title_normalised` LEFT JOIN `DISTINCT title_norm(name)`.

---

## 5. Ad-hoc `bq` queries

**The server enforces these rules in code. `bq` enforces nothing.**

`bq.run()` guards every query — it rejects `publish_day`, the bare `language`
column and `SELECT *`, parameterises all values, and caps bytes billed. None of
that applies at the `bq` command line. A `bq` query is unguarded by
construction.

So:

- **Use `bq` to check the server's homework, not to produce reported figures.**
  Verifying a tool's number, profiling a new defect, testing a candidate
  predicate — all good uses.
- **A `bq` figure that reaches a report must satisfy every rule in this file by
  hand.** Every ban in §1, all three carriers in §2, the relevant field rules in
  §3, and the work-level rules in §4 if any dedup is involved.
- **Say which figures came from `bq`** rather than from a tool, so a reader
  knows which ones had no enforcement behind them.
- Prefer promoting a recurring ad-hoc query into a tool. A figure worth
  reporting twice is worth guarding.

---

## 6. Correcting this file

**6.1 Every figure is measured, not estimated, and states what produced it.**
Give the predicate or the query, as the `> Measured:` lines above do. A figure
without a stated derivation cannot be checked and does not belong here.

**6.2 When correcting a figure, record the cause, not just the new value.** The
wrong number is rarely the whole error — the reasoning that produced it usually
generalises to other figures. Two worked examples from this project:

- `publish_day` was documented as 73.6% placeholder; it is 48.25%. The cause was
  a **denominator error** — 73.60% is the placeholder rate *within the
  1,212,960-row ambiguous subset*, not the table. Recording only "48.25%" would
  have lost the fact that the subset figure is itself correct and still useful.
- Work-key over-merging was reported as "1,550 keys (2.25%)". The cause was a
  **classifier that measured the wrong population** — it counted keys merging
  any distinct series reference, mostly single volume numbers that merge
  correctly. The true range over-merge was 3 keys. Recording only the new
  number would have left the broken classifier in place to be reused.

**6.3 Distrust any figure whose derivation you cannot reproduce — including one
in this file.** Re-measure before quoting. Figures in `DATA_NOTES.md`,
`README.md` and `caveats.py` have all been wrong at some point; this file has no
special standing.

**6.4 Do not quote `APPROX_*` results as exact.** `APPROX_QUANTILES` returned a
90th percentile of 527 where `PERCENTILE_DISC` gives 524 on the same data.
Approximate aggregates are fine for a scan and wrong for a published figure —
use the exact function when the number will be quoted, and label it if you
cannot.

**6.5 Correct every copy.** A figure usually appears in more than one of
`DATA_NOTES.md`, `README.md`, `caveats.py` and this file. Grep for the old value
before considering a correction finished.

---

## Pre-flight checklist

Run before any figure leaves this project — tool output, chart, or sentence.

1. **Bans.** Does it touch `publish_day`, bare `language`, an unfiltered
   `AVG(rating)`, a `SUM(rating_dist_total)` called a rating count, or an
   unthresholded ranking? If yes, it does not ship. (§1)
2. **n.** Is the row count behind it stated? (§2.1)
3. **Unit.** Does it say editions or works? (§2.2)
4. **Exclusions.** Is the threshold stated with what it removed? (§2.3)
5. **Field rules.** Any month, publisher, language, NULL-bearing, `id` or `isbn`
   figure carrying its rule? (§3)
6. **Dedup.** If work-level: is `n_ratings` described as a floor, and is the
   key's over/under-merge acknowledged where it could matter? (§4)
7. **Provenance.** Tool or ad-hoc `bq`? If `bq`, has every rule been applied by
   hand, and is that noted? (§5)
8. **Reproducible.** Can you state the query that produced it, and did you
   re-measure rather than copy it from a doc? (§6)
