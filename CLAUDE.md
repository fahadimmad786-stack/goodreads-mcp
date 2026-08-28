# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read RULES.md first

**`RULES.md` governs every figure that leaves this project** — MCP tool output, ad-hoc `bq` queries, charts, and sentences in reports alike. Read it before producing or quoting any number from this dataset, not just before editing the server.

The split: `DATA_NOTES.md` records *what* the defects are, `RULES.md` says *what you may do about them*. It carries hard bans that no caveat or disclaimer makes acceptable (`publish_day`, bare `language`, unfiltered `AVG(rating)`, `SUM(rating_dist_total)` presented as a rating count, unthresholded rankings), the three things every figure must carry (its n, its unit, what the threshold excluded), and a pre-flight checklist to run before a figure ships.

It matters most **outside** the server. `bq.run()` enforces these rules in code; the `bq` CLI enforces nothing, so an ad-hoc query is unguarded by construction. Use `bq` to check the server's homework, not to produce reported figures — and if a `bq` figure does reach a report, every rule has to be applied by hand.

## Commands

```bash
# Setup
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
gcloud auth application-default login          # BigQuery uses ADC

# Tests
.venv/bin/python -m pytest tests/ -q           # 50 offline invariant tests, no network
.venv/bin/python -m pytest tests/test_guards.py::test_work_key_preserves_ranges_and_drops_volume_numbers -q
.venv/bin/python tests/smoke_live.py           # 18 live calls + a cache-miss probe, needs ADC

# Run the server (stdio is the default; local registration unchanged)
.venv/bin/python -m goodreads_mcp
.venv/bin/python -m goodreads_mcp --transport http --port 8080   # Cloud Run mode
./deploy.sh                                    # Cloud Run; see README for the min-instances knob

# Telemetry: one JSON line per tool call, at logs/telemetry.jsonl (gitignored)
.venv/bin/goodreads-telemetry                  # summarise the log
.venv/bin/goodreads-telemetry --json --tool stats_by_author
GOODREADS_TELEMETRY=0 .venv/bin/python -m goodreads_mcp    # disable
```

`pytest` only collects `tests/`; `smoke_live.py` is a script, not a pytest module, and bills real BigQuery queries.

Ad-hoc BigQuery exploration is easiest with the `bq` CLI, which bypasses the query guards described below:

```bash
bq query --nouse_legacy_sql --format=prettyjson 'SELECT ... FROM `example-project.goodreads.books`'
```

Env overrides: `GOODREADS_BQ_PROJECT`, `GOODREADS_BQ_DATASET`, `GOODREADS_BQ_LOCATION`, `GOODREADS_MAX_BYTES_BILLED` (default 20 GiB).

## Architecture

An MCP server (FastMCP) exposing twelve **aggregate-only** tools over two BigQuery tables: `goodreads.books` (1,850,115 rows) and `goodreads.user_ratings` (357,396 rows from 4,154 users). There is deliberately no row-browsing tool.

The organising idea: **the dataset's defects are handled structurally, not by documentation.** A caller who never reads `DATA_NOTES.md` still cannot get a wrong number. Four layers, each enforcing something the layer above cannot bypass:

- **`bq.py`** — the only path to BigQuery. `run()` calls `guard()` on every query, so guards cannot be bypassed by writing a new tool. All values are named parameters; every job is cost-capped.
- **`queries.py`** — shared SQL fragments, parameter validators, and `envelope()`, the shape every tool returns. Anything true of more than one tool lives here so it is stated once.
- **`caveats.py`** — a registry keyed by id. Tools name the ids for the code path they took; they never write caveat prose inline. An unknown id raises rather than silently dropping a warning. Each entry is tagged `[DATA_NOTES.md #n]` or `[measured]` (found by profiling, absent from the notes).
- **`server.py`** — the tools. Each validates params, builds SQL from `queries.py` fragments, and returns `envelope(...)`.

`clean_goodreads.py` is the one-shot script that produced the BigQuery tables from the Kaggle dump. It does not run at serve time, but SQL here sometimes has to reproduce its logic exactly — see the title keys below.

### Invariants worth knowing before editing

**Two title keys, and they are not interchangeable.** This is the easiest thing to get wrong.

| | used for | series suffix |
|---|---|---|
| `title_norm()` | the `user_ratings` join **only** | stripped entirely |
| `work_key()` | all work-level dedup, `n_distinct_titles` | **range kept** (`#1-5`), volume number dropped (`#2`) |

`title_norm()` must reproduce the cleaning script's `book_title_normalised` exactly or the documented 52,016-title join coverage breaks — do not change it. `work_key()` exists because stripping the whole suffix merges different products (the four Harry Potter boxed sets). `test_only_the_join_uses_title_norm` fails if any non-join path reaches `title_norm`.

**`envelope()` is mandatory and `n` is keyword-only.** No average leaves this server without the count it rests on, plus what the threshold excluded.

**`min_ratings` is floored at 1, never 0.** 451,777 books have no ratings and are stored as `rating = 0.0`; at 0 they enter every average.

**Grouped tools take `unit="editions" | "works"`.** A row in `books` is an edition. `stats_by_author` defaults to `"works"` (most-read author is a question about works); everything else defaults to `"editions"`. Both branches emit identical column names so `order_by` and the envelope are unaffected. `_grouped()` in `server.py` branches on it; `_unit_caveats()` maps the choice onto caveat ids.

**Telemetry wraps every tool.** `@telemetry.instrument` sits *beneath* `@mcp.tool` on all twelve; a test fails if a tool is added without it. It records params as passed, outcome, row count, BigQuery job ids, bytes billed/processed, cache hit, and a wall-time vs BigQuery-time split. Guard rejections log the rule and column only — never the SQL. Results are never logged.

### Traps

- **stdout is the MCP protocol channel — never write to it.** The server speaks JSON-RPC over stdio, so one stray byte on stdout corrupts framing and kills the connection silently: no error, no traceback. All diagnostics go to a file or `sys.stderr`. `test_no_server_module_can_reach_stdout` walks every package module's AST and fails on a `stdout` reference, a `print()` without `file=sys.stderr`, or any `logging.basicConfig` call. `telemetry_cli` is exempt — separate process, never imports the server.

- **Two transports, one stdout rule.** stdio is the default and writes telemetry to a file; HTTP (Cloud Run) writes structured JSON to stdout for Cloud Logging. The ban is not relaxed for HTTP — it is scoped by the import graph: `telemetry_stdout` is imported *only* by `set_transport("http")`, so under stdio the stdout-writing code is never loaded. A subprocess test asserts that directly, and `activate()` refuses if the transport is stdio. Also note uvicorn's access log defaults to **stdout** and is disabled in HTTP mode — it would otherwise put plain text into the structured log stream.

- **`guard()` regex-matches the SQL text**, so a query string mentioning `publish_day` or the bare `language` column throws `QueryGuardError` — *including inside a SQL comment*. Use `language_normalised`; `\b` deliberately does not match between `language` and `_normalised`.
- **Several tests assert on source text** via `inspect.getsource(server)`. Renaming a helper or reformatting a `caveats.collect(...)` call can fail a test without changing behaviour. That is intentional — it is how "every tool reporting `pooled_rating` states the duplication caveat" is enforced — but expect it during refactors.
- **FastMCP wraps tool functions.** To call one directly, use `getattr(tool, "fn", tool)`, as `tests/smoke_live.py` does.
- **Editions repeat their work's ratings.** Under `unit="editions"`, `SUM(rating_dist_total)` overcounts ~2.4×, because each edition carries most of its work's rating pool. Under `"works"` the collapse takes the *maximum* edition total, so `n_ratings` is a floor, not an exact work total.

### DATA_NOTES.md

The dataset-defect reference, and the source for `[DATA_NOTES.md #n]` caveats. It is authoritative but has contained errors; corrections are recorded inline in the file and in the README's measured-defects list. Two that matter:

- `publish_day` is **48.25%** placeholder, not the 73.6% originally stated — that was a denominator error (73.60% is the rate within the 1,212,960-row ambiguous subset), not a stale measurement.
- The 13.6% `language_normalised` coverage is a property of the source dump, not of cleaning: `normalise_language()` nulls only 13 rows.

When profiling turns up a defect the notes do not mention, add it to `caveats.py` tagged `[measured]` and to the README's measured-defects list rather than silently working around it.
