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

## Telemetry

Every tool is wrapped by `@telemetry.instrument`, sitting beneath `@mcp.tool`.
A test fails if a tool is added without it — the same structural enforcement the
query guards use. One JSON object per call is appended to
`logs/telemetry.jsonl` (gitignored):

```json
{"ts":"...","tool":"stats_by_author","params":{"min_ratings":100,"unit":"works"},
 "outcome":"ok","n_rows":5,"n_queries":2,"bytes_billed":232783872,
 "bytes_processed":231847973,"cache_hit":false,"job_ids":["..."],
 "duration_ms":4198.1,"bq_ms":4197.3,"overhead_ms":0.8,"queries":[...]}
```

Query results are never recorded, and a guard rejection logs `guard_rule` and
`guard_column` — never the SQL that tripped it.

**stdout is the MCP protocol channel.** This server speaks JSON-RPC over stdio,
so a stray byte on stdout corrupts framing and kills the connection silently.
Telemetry writes to a file by path; its only fallback is an explicit
`file=sys.stderr`. `test_no_server_module_can_reach_stdout` walks every package
module's AST and fails on a `stdout` reference, a `print()` without an explicit
stderr target, or any `logging.basicConfig` call — whose default is stderr, but
whose `stream=` kwarg is one edit from stdout.

Configuration: `GOODREADS_TELEMETRY_PATH` moves the log,
`GOODREADS_TELEMETRY=0` disables it entirely.

```bash
goodreads-telemetry                       # summary: calls, error rate, p50/p95,
                                          # bytes billed, guard rules, params used
goodreads-telemetry --tool stats_by_author --json
```

## Deploying to Cloud Run

stdio remains the default and is unchanged — `python -m goodreads_mcp` with no
flag behaves exactly as before, and an existing local Claude Code registration
needs no edit. HTTP is a **second** transport, selected by `--transport http`
or `GOODREADS_TRANSPORT=http`.

```bash
./deploy.sh                      # project example-project, region us-central1
MIN_INSTANCES=0 ./deploy.sh      # override the warm-instance knob (see below)
```

### IAM: what the service account gets, and why

`goodreads-mcp-run@example-project.iam.gserviceaccount.com` — **read-only**,
**no key files**. Credentials come from the Cloud Run metadata server.

| role | scope | why |
|---|---|---|
| `roles/bigquery.jobUser` | project | `bigquery.jobs.create`. Every query is a job. Must be project-scoped — job creation cannot be granted on a dataset. |
| `roles/bigquery.dataViewer` | **dataset `goodreads` only** | `bigquery.tables.getData` / `tables.get` / `datasets.get`. Scoped to one dataset so the SA cannot read anything else in the project. |
| `roles/logging.logWriter` | project | Container stdout → Cloud Logging, where telemetry goes in HTTP mode. Without it a custom runtime SA has its logs silently dropped. |

Deliberately **not** granted: `roles/bigquery.user` (carries
`datasets.create`, `reservations.use` and four `cloudkms.*` permissions) and
`roles/bigquery.dataEditor` (write access). Keeping the SA read-only is also
why telemetry goes to Cloud Logging rather than a BigQuery table.

### Auth: the endpoint is not public

Deployed `--no-allow-unauthenticated`. Unauthenticated requests get 403 at
Google's edge before reaching the container. Connect through the authenticated
local proxy:

```bash
./proxy.sh                                                    # keep running
claude mcp add --transport http goodreads-remote http://127.0.0.1:8080/mcp
```

No header, no token, nothing in `~/.claude.json` — `proxy.sh` injects
credentials and refreshes them itself. It must be running for the server to
connect; without it Claude Code reports `ConnectionRefused`.

**The proxy needs the standalone Cloud SDK.** `gcloud components install
cloud-run-proxy` fails on a distro-packaged gcloud:

```
ERROR: You cannot perform this action because this Google Cloud CLI
installation is managed by an external package manager.
```

Install from <https://cloud.google.com/sdk/> — it coexists with the distro
package and shares `~/.config/gcloud`, so authentication carries over with no
re-login. `proxy.sh` uses `~/google-cloud-sdk/bin/gcloud` and clears
`CLOUDSDK_ROOT_DIR`, which a distro install exports and which would otherwise
point the standalone gcloud at the wrong root.

<details>
<summary>Fallback if you cannot install the standalone SDK</summary>

```bash
claude mcp add --transport http goodreads-remote \
  https://goodreads-mcp-552178111715.us-central1.run.app/mcp \
  --header 'Authorization: Bearer ${GOODREADS_ID_TOKEN}'
export GOODREADS_ID_TOKEN=$(gcloud auth print-identity-token)
```

The `${VAR}` form keeps the token out of `~/.claude.json`. Measured: the token
lasts **60 minutes** and its `aud` is the gcloud OAuth client ID, not the
service URL — the replay weakness Google documents. Re-export and restart
Claude Code when it expires. This is strictly worse than the proxy on both
ergonomics and security.
</details>

**Trade-off:** an extra local process and a gcloud dependency, and it only
works where you are gcloud-authenticated — not Claude.ai web, not a teammate
without a `roles/run.invoker` binding. In exchange there is no token in any
config file, nothing to expire, and revocation is one binding removal.

A static `Authorization: Bearer $(gcloud auth print-identity-token)` header
also works with Claude Code, but those tokens last about an hour and, per
Google's docs, lack an audience claim — worse on both ergonomics and security.

Note `--ingress all` is intentional: "not public" here means IAM returns 403,
not network unreachability. `--ingress internal` would break the proxy, which
reaches the public URL *with* a token.

### The min-instances knob

`deploy.sh` sets `MIN_INSTANCES=1`. It is a knob, not a decision:

| | first-call latency | idle cost |
|---|---|---|
| `MIN_INSTANCES=1` | no cold start | one always-on instance, billed at the idle CPU rate |
| `MIN_INSTANCES=0` | **+3–5 s** on the first call after scale-to-zero | nothing |

The cold-start figure comes from measuring this app's startup locally:
**1,089 ms** to import `goodreads_mcp.server` and a further **1,545 ms** to
construct the BigQuery client — 2,634 ms of Python before a query starts, plus
container pull and start on top. `--cpu-boost` attacks that directly, and the
client is warmed at startup (HTTP mode only) so the first real call does not
pay the 1,545 ms.

Reverting is one variable: `MIN_INSTANCES=0 ./deploy.sh`. Idle cost is on the
order of tens of dollars a month for one small instance — **verify against the
current [Cloud Run pricing](https://cloud.google.com/run/pricing) before
committing; that figure is an estimate, not a measurement.**

### Latency figures measured before deployment are cache-inflated

> **Do not quote the pre-deployment p50 as a production baseline.**

The p50 of 3,557 ms and p95 of 6,457 ms in this repo's telemetry were measured
locally on **2026-08-28** with a **95% BigQuery cache hit rate**, produced by a
smoke script issuing the same calls repeatedly. They understate real latency,
for two compounding reasons:

1. **The cache is per-identity.** Google's docs: *"Temporary, cached results
   tables are maintained per-user, per-project."* Cross-user caching needs
   Enterprise edition. The Cloud Run service account is a different identity
   from your local ADC, so it starts with an empty cache and builds its own.
2. **Real traffic varies parameters.** Cache keys include parameter values.
   Model-driven calls varying `min_ratings`, `limit` and `unit` will miss far
   more often than a smoke script replaying identical calls.

The cache *does* work across Cloud Run instances — every instance runs as the
same service account, so scaling out does not fragment it. Entries expire after
about 24 hours.

**Re-measure from production telemetry before anyone quotes a latency number.**
A cache hit bills 0 bytes, so the existing tooling already reports the real
rate:

```bash
gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=goodreads-mcp AND jsonPayload.tool!=""' \
  --project example-project --limit 1000 --format json \
  | goodreads-telemetry --path -
```

See `RULES.md` §6 — this is the same discipline the dataset figures are held to.

### Telemetry in Cloud Run

HTTP mode switches the sink from the local file to **structured JSON on
stdout**, which Cloud Logging parses into `jsonPayload` and levels by the
`severity` field (`ok` → INFO, `guard_rejected` → WARNING, errors → ERROR).

stdout must stay purely structured for that to work, which is why uvicorn's
access log is disabled — it writes plain-text `INFO: ... 200 OK` lines to
stdout that would land as unstructured entries. Nothing is lost: Cloud Run
logs every request itself, with more structure. A test fails if a non-JSON
line appears on stdout in HTTP mode.

Retention is the `_Default` bucket's 30 days. If SQL access over telemetry is
ever wanted, add a **Logging sink to BigQuery** — that writes as a
Google-managed identity and keeps the runtime SA read-only.

### Health check

`GET /health` returns status, transport and the active
`max_bytes_billed`. Note the path is `/health`, **not** `/healthz` — Google's
frontend intercepts `/healthz` before it reaches Cloud Run, so that path 404s
and never appears in the request log. It deliberately does not touch BigQuery: a probe that
queried would bill on every check and would fail the service during a
BigQuery incident the container could otherwise ride out.

### `GOODREADS_MAX_BYTES_BILLED` survives the transport change

Read from the environment in `bq.py` and applied per job as
`QueryJobConfig(maximum_bytes_billed=...)`. Nothing in either transport path
touches it. It is set explicitly in the Dockerfile and the deploy script rather
than relying on the 20 GiB default, surfaced by `/healthz`, and pinned by a
test.

## The web console (`webchat/`)

A browser interface for the same twelve tools, deployed as a **second** Cloud
Run service, `goodreads-chat`. It exists to make the server's central property
visible: figures arrive with their limits attached.

Two modes reach the tools, and the toggle in the masthead is the only
difference between them:

| | **chat** | **tools** |
|---|---|---|
| what picks the tool | a model, from a question | you, from a list |
| what sets the parameters | the model | a form built from the tool's JSON Schema |
| needs `ANTHROPIC_API_KEY` | yes | **no** |
| prose around the card | the model's, numeral-checked | none |
| the card, caveats, charts, refusals | identical | identical |

The tool mode is the console with the model taken out. Everything downstream of
the tool call is the same code — the same envelope rendering, the same caveats
attached to the same fields, the same n/unit/threshold block, the same charts,
the same refusal cards, the same guard probe under its `bff` badge.

```bash
.venv/bin/pip install -e '.[web]'
./run-local.sh          # starts proxy.sh if needed, then the console; prints the URL
./deploy-chat.sh        # Cloud Run; prints the ?k= URL
```

`run-local.sh` is the whole local path in one command. It reads
`ANTHROPIC_API_KEY` from a gitignored `.env` if there is one — creating the file
with a comment when it is absent, and saying which modes the run will offer
rather than refusing to start — generates a
`CHAT_ACCESS_TOKEN` on first run and saves it back to `.env` so the `?k=` URL
stays stable between runs, starts `proxy.sh` only if nothing is already serving
the MCP endpoint, waits for both to answer their health checks, and prints the
ready-to-click URL.

Ctrl+C shuts down what it started, and only that. `proxy.sh` execs gcloud,
which spawns `cloud-run-proxy` as a child, so the proxy is started with `setsid`
and the whole process group is signalled — killing the script's own pid would
leave the tunnel behind. A proxy that was already running when the script
started is deliberately left up, because something else is using it.

Neither script requires the key. `deploy-chat.sh` mounts the Anthropic secret
only when it exists, because `--set-secrets` naming an absent secret fails the
deploy and mounting an empty one would leave the console advertising a chat mode
that 500s on every turn.

Env overrides: `PORT` (default 8081), `MCP_PORT` (8080), `ENV_FILE`, `LOG_DIR`.
`.env` is parsed rather than sourced — it holds secrets, and `.` would execute
whatever is in it — so only `KEY=value` lines are read, and an already-exported
value wins over the file.

### The no-model mode, and why its forms are generated

The form is built in the browser from each tool's `inputSchema`, served by
`/api/tools` — which is `MCPBridge.catalogue()`, the *same cached `tools/list`
output the model is given*, reshaped and nothing else. So the widget, its
label, its bounds and its default all come from the server, and a `Field(...)`
edited in `server.py` moves the form and the model's tool definition together.
Hand-writing the forms would make the UI a hand-maintained copy of the tool
surface, which is the documentation-instead-of-structure failure the whole
project avoids. `test_no_tool_parameter_name_is_written_into_the_client` fails
if any parameter name appears in `tools.js` outside its preset values.

Two behaviours look like omissions and are not:

* **An empty field is not sent.** The server's default applies, and the card's
  parameter row then shows exactly what was overridden rather than a wall of
  values nobody chose. Each field prints its default beside it and carries it as
  the placeholder, so nothing is hidden.
* **Values are passed verbatim.** `min_ratings=0` and `unit=chapters` reach the
  server unaltered, and `min`/`max` are printed on the field rather than set as
  HTML attributes that would clamp them. The refusal that comes back — with the
  server's own explanation and the caveats behind the constraint — is the most
  instructive thing this console can show, and validating in the browser would
  replace it with silence. The route's only checks are the ones that keep it
  from being a general-purpose proxy: a known tool name, a flat object, scalar
  values.

Both paths end in `MCPBridge.call()` and both build their frame with
`frames._result_frame` — the same function object, asserted by a test, because
two builders would drift. `frames.py` exists so the tool path can build a card
without importing `agent.py`, which constructs an Anthropic client.

`config.verify()` therefore requires only `CHAT_ACCESS_TOKEN`. Without a key the
service starts, the chat button is disabled with the reason in its tooltip, and
`/api/chat` answers 503 naming the mode that does work.

### Why a backend-for-frontend, and why an MCP client

A browser cannot call the server directly: MCP is JSON-RPC and the Cloud Run
service is `--no-allow-unauthenticated`. The console is therefore a BFF holding
two credentials the client never sees — a Google identity token for Cloud Run
and an Anthropic API key.

It connects as an **MCP client** rather than mirroring the tool definitions.
Tool schemas come from `tools/list` and the model's steering text from the
server's own `instructions`, so a docstring edit in `server.py` reaches the UI
on the next deploy. Mirroring would make the UI a hand-maintained copy of the
tool surface, which is the documentation-instead-of-structure failure the whole
project is built to avoid.

The Anthropic **MCP connector** (`mcp_servers=[{type:"url", ...}]`) was rejected
for two independent reasons: it would require either making the MCP service
publicly invokable or handing a Google identity token to a third party, and it
delivers tool results into the model's context rather than to the BFF, which
would make structural rendering of the figures impossible.

### Two services, because they need opposite IAM postures

The console must be reachable by browsers, which carry no Google identity; the
MCP server must stay private. One Cloud Run service has one IAM policy, so one
service cannot be both. Everything else follows from that split:

| | `goodreads-mcp` | `goodreads-chat` |
|---|---|---|
| ingress | `--no-allow-unauthenticated` | `--allow-unauthenticated` + shared token |
| service account | `goodreads-mcp-run` | `goodreads-chat-run` |
| BigQuery | `roles/bigquery.jobUser` | **none** |
| secrets | none | the access token, and an Anthropic key if chat is wanted |
| image | root `Dockerfile` | `webchat/Dockerfile` |

`deploy-chat.sh` adds exactly two IAM bindings: `roles/run.invoker` on
`goodreads-mcp` for the console's service account, and
`roles/secretmanager.secretAccessor` on the two secrets. It grants **no**
BigQuery role — the console reaches BigQuery only as a consequence of a guarded
tool call running under the MCP service's identity — and no
`iam.serviceAccountTokenCreator`, because minting an ID token for its *own*
identity from the metadata server requires no role. That last one is the usual
place this gets over-granted.

### Auth flow

1. **Browser → console.** No Google identity. A shared secret
   (`CHAT_ACCESS_TOKEN`, from Secret Manager) presented as `?k=` on first visit,
   then held in an `HttpOnly` cookie. The token is required: the service refuses
   to start without one, with no override flag, because a public endpoint that
   bills BigQuery on every tool call — and an Anthropic account on every chat
   turn — is not an acceptable default. It is the *only* required secret.
2. **Console → MCP server.** An OIDC identity token minted from the metadata
   server for `audience = <the MCP service's base URL>`, cached until five
   minutes before it expires, sent as `Authorization: Bearer`. Google's edge
   validates the signature, the audience and `roles/run.invoker` before the
   request reaches the container.
3. **Console → Anthropic.** Only in chat mode. `ANTHROPIC_API_KEY` from Secret
   Manager via `--set-secrets`, never `--set-env-vars`, never in the image or
   the repo. Absent it, this leg does not exist and neither does the mode.

Locally the default is the `proxy.sh` path: `GOODREADS_MCP_URL` points at
`127.0.0.1:8080` and the console sends no credential of its own, because
`gcloud run services proxy` injects one. Setting `GOODREADS_MCP_TOKEN`
(from `gcloud auth print-identity-token`) is the direct alternative; setting
both it and an audience is a startup error rather than a silent precedence rule.

Neither credential is ever placed in an SSE frame or a log line, and transport
error text is scrubbed of `Authorization` before it reaches a client.

### Spend ceilings

The console is public-by-URL, so the abuse surface is the bill rather than IAM:
a required access token, ten chat turns per IP per five minutes, twenty-five
turns per session, six tool calls per turn, `--max-instances 3`, and the
server's existing 20 GiB `maximum_bytes_billed` per query.

Tool mode has its own window — forty calls per IP per five minutes
(`CHAT_TOOL_RATE_LIMIT_CALLS`). A form submission bills BigQuery bytes but no
Anthropic tokens, and one call per form is a far tighter loop than one call per
sentence, so sharing the chat window would have made the mode unusable long
before it made it expensive.

### The rendering contract, and how it is enforced

The model never renders a figure. Every number on screen is drawn by
`webchat/static/cards.js` from the tool's own envelope, together with its `n`,
the unit one row counts, the min-ratings threshold and what it excluded, the
caveats, and the query cost. Nothing is computed client-side: if a share is not
in the envelope it is not shown, because a derived figure would be a figure
with no caveats attached to it.

`webchat/numcheck.py` checks that the model kept to it. Every numeral in the
prose is canonicalised and looked up in the set of numerals the server actually
put in front of the session — tool results including caveat prose, tool
parameters, the server's `instructions`, and the user's own question. Anything
else is marked in place in the answer. A rounded figure fails by construction:
`4.4` does not match `4.42`. The check reports rather than blocks; suppressing
the answer would hide the violation instead of showing it.

Caveats attach to **fields**, not to the card. `webchat/attach.py` maps each
caveat id to the figure fields it qualifies, so the duplication caveat puts a
marker on `n_ratings` and `pooled_rating` specifically, and the caveat text sits
in the same card, always expanded — never a footnote, never behind a disclosure
triangle. `test_every_registered_caveat_has_a_field_mapping` fails if a caveat
is added to the server without one.

### `check_column_available`: a demonstration probe

`QueryGuardError` is unreachable from the twelve tools by construction — no tool
interpolates a caller-supplied column into SQL — so a user asking about
`publish_day` gets nothing from the guard, because there is no tool through
which to ask. That is the design working, not a gap (see CLAUDE.md).

So the console carries one tool of its own, `check_column_available`, which runs
the server's real `bq.guard()` against a candidate query it never executes and
reports the verdict, the rule id and the server's own caveat prose for that
column. It is labelled **`bff` / demonstration probe** in the UI, distinct from
the `mcp` badge every real tool carries, and it is deliberately not a thirteenth
MCP tool: adding a tool parameter that reached a banned column would convert a
structural impossibility into a runtime rejection.

### Refusals come from two layers, and the console distinguishes them

Live testing turned up something the offline tests cannot see, because they call
tool functions directly and so bypass the schema:

| layer | fires for | reaches the caller as |
|---|---|---|
| tool schema (FastMCP/pydantic) | anything with a `Field(ge=…)` bound — `min_ratings`, `min_books`, `limit` | a validation error, before the tool body runs |
| `ParamError` → `_fail()` | the unconstrained parameters — `unit`, `order_by`, `direction`, empty `language`, `year_from > year_to` | a structured result carrying the server's full explanation |

So `min_ratings=0` — the most instructive refusal in the server — never reaches
`require_min_ratings()` over MCP, and the validation error says only "Input
should be greater than or equal to 1", not why the floor exists. The console
renders that as its own refusal kind (`schema`) and re-attaches the server's
reasoning from the caveat registry, so the reader still gets the 451,777
unrated books. It re-attaches; it does not write a second explanation.

Both layers are wanted. The body validator is what protects a direct Python
caller and what carries the prose, so neither was removed or loosened to make
the console simpler.

### Design notes

One accent (`#2a78d6` light, `#3987e5` dark — validated for lightness, chroma
and 3:1 contrast against both surfaces) plus one reserved status ink for
refusals and placeholder-inflated rows. Charts are hand-rolled inline SVG: no
library, so every bar carries its own exact value and nothing is read off an
axis. Two measures never share an axis — `stats_by_year` draws volume and rating
as two stacked charts rather than one dual-axis chart. The masthead carries no
dataset figures at all; every number about the data appears inside a tool card,
from that tool's JSON.

Every size in `app.css` comes from one of two scales — six type steps, an
8-step 4px space scale — and every card section shares one horizontal padding,
so the tool name, the parameters, the figure, the n block and the cost all
start on the same vertical line. Two optical values sit deliberately off the
grid (`--pill-pad`, `--code-pad`) because a 10.5px badge looks wrong on layout
spacing; they are named tokens rather than guesses at each use. Colour appears
exactly once in the sheet, in the token blocks. Four tests enforce all of this:
a stray `font-size: 14px`, a `padding: 17px`, a hex colour in a rule, a
`box-shadow`, a removed focus ring, or a token with no dark value each fail the
suite rather than merely looking wrong.

**Contrast is measured, not judged.** Every ink/surface pair meets 4.5:1 and
every control boundary, data mark and focus ring meets 3:1, in both themes.
That drove three token changes: `--ink-3` was darkened (it carries most of the
11–12px metadata and sat at 3.4:1 on `--surface-2`), filled buttons got
`--accent-fill` because white on `--accent` was 4.3:1, and controls got a
dedicated `--edge` at 3:1 — the hairline `--rule` tokens stay decorative and
are correctly exempt.

**Accessibility.** Each chart is `role="img"` with a label built by
`figureDesc()` from the envelope: the measure, the mark count, the unit one row
counts, the n, and what the threshold excluded — so a screen-reader user gets
the same grounding the card shows everyone else. Tables carry `scope="col"` and
a hidden caption; the figure scrolls, so it is focusable; the mode toggle is a
real tablist with roving tabindex and arrow keys; state is never colour alone
(the status dot has text beside it, flagged rows are labelled, unsourced
numerals are underlined); and a polite live region narrates each call.

### Not for indexing

The console is private, token-gated, and its URL carries the access key, so
every response sends `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet`
and `/robots.txt` returns `Disallow: /` — the one route deliberately readable
without the key, since a crawler that cannot fetch it never learns to stay
away. The page repeats the directive in a `<meta name="robots">` that survives
being saved.

The key gets three separate protections. `Referrer-Policy: no-referrer` on
every response means it cannot reach a third party's logs; the client deletes
`?k=` from the address bar with `history.replaceState` once the cookie is set,
so it leaves the URL bar, the history entry and any screenshot; and no page
ever writes it into an href — the app shell creates no `<a>` elements at all,
and the locked page's placeholder is the literal string `<key>`. A test asserts
all three.

A CSP (`default-src 'self'`, no `unsafe-inline`, `img-src 'self' data:` for the
inline SVG favicon) makes "zero external requests" enforceable rather than
merely true today: no fonts, no CDN, no analytics, and an added one fails in
the browser console instead of shipping quietly.

A missing key gets a styled 401 that explains what `?k=` is and where the key
lives, and an unknown path gets a styled 404 naming what missed — both in the
console's own design, from `webchat/pages.py`. An unknown `/api/` path returns
JSON instead, because that is what a `fetch()` there can read.

Conversation history is held server-side in memory, keyed by an `HttpOnly`
cookie, rather than posted back by the client each turn. That is a correctness
choice: a client that supplied the history could forge tool results into the
model's context, and fabricated figures in history is exactly the failure this
project exists to prevent. The cost is that an instance recycle loses the
transcript — the console says so rather than continuing against an empty one.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q        # offline invariant tests, no network
PYTHONPATH=. .venv/bin/python tests/smoke_live.py   # 18 live calls + probe, needs ADC
```

`tests/test_guards.py` covers the dataset's rules; `tests/test_webchat.py`
covers the console's claims — that every caveat can be attached to the figure it
qualifies, that no numeral in the model's prose escapes the checker, that no
credential can reach a client, and that the no-model mode is the same path and
not a parallel one: the same frame builder, forms with no hard-coded parameter
in them, and values that reach the server exactly as typed.
