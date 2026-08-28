"""
The dataset's hard rules, tested as rules rather than as tool behaviour.

These run without network access: they exercise the guard and validation
layers directly. The live behaviour of each tool is covered by
tests/smoke_live.py, which does need BigQuery.
"""

from __future__ import annotations

import inspect
import re

import pytest

from goodreads_mcp import bq, caveats, queries, server


# --- publish_day may never be read (DATA_NOTES.md #1) ----------------------


def test_guard_rejects_publish_day():
    with pytest.raises(bq.QueryGuardError, match="publish_day"):
        bq.guard("SELECT publish_day FROM t")


def test_guard_rejects_publish_day_anywhere_in_query():
    with pytest.raises(bq.QueryGuardError):
        bq.guard("SELECT name FROM t WHERE publish_day = 1")


def test_no_source_line_reads_publish_day():
    """Not one SQL string in the server mentions the column."""
    for module in (server, queries):
        src = inspect.getsource(module)
        # The word appears only inside a caveat/doc string, never in SQL.
        for line in src.splitlines():
            stripped = line.strip()
            if "publish_day" in stripped and not stripped.startswith(("#", '"', "'", "*")):
                pytest.fail(f"{module.__name__}: publish_day in {stripped!r}")


# --- grouping is on language_normalised (DATA_NOTES.md guidance) -----------


def test_guard_rejects_bare_language_column():
    with pytest.raises(bq.QueryGuardError, match="language_normalised"):
        bq.guard("SELECT language, COUNT(*) FROM t GROUP BY language")


def test_guard_allows_language_normalised():
    bq.guard("SELECT language_normalised, COUNT(*) FROM t GROUP BY language_normalised")


def test_guard_rejects_select_star():
    with pytest.raises(bq.QueryGuardError, match="SELECT \\*"):
        bq.guard("SELECT * FROM t")


# --- the minimum-ratings threshold ----------------------------------------

RANKING_TOOLS = [
    "top_books_by_rating",
    "stats_by_language",
    "stats_by_year",
    "stats_by_publisher",
    "stats_by_author",
    "top_titles_by_user_ratings",
    "page_count_stats",
    "publish_month_seasonality",
    "rating_distribution",
]


@pytest.mark.parametrize("name", RANKING_TOOLS)
def test_every_ranking_tool_takes_a_minimum_ratings_threshold(name):
    fn = getattr(server, name)
    fn = getattr(fn, "fn", fn)  # unwrap the FastMCP tool object
    assert "min_ratings" in inspect.signature(fn).parameters


def test_join_tool_takes_a_threshold_for_each_side():
    fn = getattr(server.compare_user_vs_book_ratings, "fn", server.compare_user_vs_book_ratings)
    params = inspect.signature(fn).parameters
    assert "min_user_ratings" in params and "min_book_ratings" in params


def test_min_ratings_floor_is_one_not_zero():
    """
    At 0 the 451,777 unrated books (rating = 0.0) enter every average.
    """
    with pytest.raises(queries.ParamError, match="451,777"):
        queries.require_min_ratings(0)
    with pytest.raises(queries.ParamError):
        queries.require_min_ratings(-5)
    assert queries.require_min_ratings(1) == 1


def test_limit_is_capped():
    assert queries.clamp_limit(10_000) == queries.MAX_LIMIT
    assert queries.clamp_limit(10_000, cap=200) == 200
    with pytest.raises(queries.ParamError):
        queries.clamp_limit(0)


def test_order_by_is_whitelisted_not_interpolated():
    with pytest.raises(queries.ParamError):
        queries.require_order_by("n_books; DROP TABLE books")
    assert queries.require_order_by("pooled_rating") == "pooled_rating"


def test_direction_is_whitelisted():
    with pytest.raises(queries.ParamError):
        queries.require_direction("desc; --")
    assert queries.require_direction("ASC") == "asc"


def test_year_range_must_be_ordered():
    with pytest.raises(queries.ParamError):
        queries.book_filters(None, 2010, 1990)


def test_filters_are_parameterised_not_inlined():
    where, params, applied = queries.book_filters("en", 1990, 2010)
    assert "@language_code" in where and "'en'" not in where
    assert params == {"language_code": "en", "year_from": 1990, "year_to": 2010}
    assert applied["language_normalised"] == "en"


# --- averages always carry their n ----------------------------------------


def test_rating_aggs_reports_both_averages_and_both_counts():
    for expected in ("n_books", "n_ratings", "avg_book_rating", "pooled_rating"):
        assert expected in queries.RATING_AGGS


def test_envelope_requires_n_and_caveats():
    e = queries.envelope([], n={"n_books": 3}, caveats=["x"])
    assert e["n"] == {"n_books": 3} and e["caveats"] == ["x"]
    with pytest.raises(TypeError):
        queries.envelope([], caveats=["x"])  # n is keyword-only and required


# --- caveats --------------------------------------------------------------


def test_unknown_caveat_id_is_an_error_not_a_silent_omission():
    with pytest.raises(KeyError):
        caveats.collect("no_such_caveat")


def test_caveats_carry_their_source():
    text = "\n".join(caveats.all_caveats())
    assert "[measured]" in text and "[DATA_NOTES.md" in text


def test_the_three_undocumented_defects_are_registered():
    """These are absent from DATA_NOTES.md and are the ones most likely to
    produce a confident wrong number."""
    for cid in ("unrated_books", "language_coverage", "title_editions"):
        assert caveats.collect(cid)[0].startswith("[measured]")


def test_title_normalisation_uses_unicode_classes():
    """
    RE2's \\w is ASCII-only while Python's is Unicode-aware. Using \\w here
    loses 922 title matches against the documented coverage figure.
    """
    expr = queries.title_norm("name")
    assert r"\p{L}" in expr and r"\p{N}" in expr
    assert r"[^\w\s]" not in expr


# --- edition duplication (measured; absent from DATA_NOTES.md) -------------


def test_grouped_aggregates_expose_edition_duplication():
    """
    A row is an edition and each edition repeats its work's full rating total,
    so n_ratings overcounts. Every grouped result must carry the measure of
    how badly, for that group -- under either unit.
    """
    for aggs in (queries.RATING_AGGS, queries.RATING_AGGS_WORKS):
        assert "n_distinct_titles" in aggs
        assert "editions_per_title" in aggs
    # The works branch must also say what it collapsed, or the reader cannot
    # tell a 1.0 editions_per_title from a group that never had duplicates.
    assert "n_edition_rows" in queries.RATING_AGGS_WORKS


def test_unit_caveats_always_include_edition_duplication():
    """
    _unit_caveats is the only route by which a tool names the duplication
    caveat, so it must emit it for every valid unit -- and add work_dedup on
    top when the collapse actually ran.
    """
    for unit in queries.UNITS:
        assert "edition_duplication" in server._unit_caveats(unit), unit
    assert "work_dedup" in server._unit_caveats("works")
    assert "work_dedup" not in server._unit_caveats("editions")


def test_every_tool_reporting_pooled_ratings_states_the_duplication():
    """
    Source-level check that no tool emits pooled_rating without the
    duplication caveat. Tools reach it either literally or via
    _unit_caveats(), which the test above pins to always contain it.
    """
    src = inspect.getsource(server)
    blocks = src.split("caveats.collect(")[1:]
    checked = 0
    for block in blocks:
        head = block[: block.index(")")]
        if "dual_average" in head:
            checked += 1
            assert "edition_duplication" in head or "_unit_caveats" in head, (
                f"a tool reports pooled_rating without the duplication caveat: {head!r}"
            )
    assert checked >= 6, f"expected to check several tools, saw {checked}"


# --- work-level deduplication (unit=) ---------------------------------------


def test_require_unit_accepts_both_units_and_normalises_case():
    assert queries.require_unit("editions") == "editions"
    assert queries.require_unit("WORKS") == "works"
    assert queries.require_unit("  Works  ") == "works"


def test_require_unit_rejects_anything_else():
    for bad in ("edition", "work", "", "titles", "books"):
        with pytest.raises(queries.ParamError):
            queries.require_unit(bad)


def test_both_units_emit_the_same_column_names():
    """
    order_by is whitelisted against one set of keys and the envelope sums
    n_books regardless of unit, so the two branches must agree on names.
    """
    import re

    def aliases(sql):
        return set(re.findall(r"AS (\w+)", sql))

    shared = {"n_books", "n_distinct_titles", "editions_per_title",
              "n_ratings", "avg_book_rating", "pooled_rating"}
    assert shared <= aliases(queries.RATING_AGGS)
    assert shared <= aliases(queries.RATING_AGGS_WORKS)
    for key in queries.GROUP_ORDER_KEYS:
        assert key in aliases(queries.RATING_AGGS_WORKS), key


def test_work_dedup_picks_one_representative_edition_deterministically():
    """
    The representative must be a whole row, not a column-wise MAX, or the
    rating and the star counts could come from different editions.
    """
    cte = queries.work_dedup_cte("authors", "authors", "TRUE", "TRUE")
    assert "ARRAY_AGG" in cte
    assert "ORDER BY rating_dist_total DESC, id ASC LIMIT 1" in cte
    assert "GROUP BY authors, " in cte
    # Grouping must include the normalised title, else it is not a work key.
    assert "REGEXP_REPLACE" in cte.split("GROUP BY authors,")[1]


def test_work_dedup_does_not_sum_edition_totals():
    """
    Editions repeat their work's rating pool, so SUM would overcount. The
    works branch must read the representative's total, never a sum of totals.
    """
    assert "SUM(rep.rt)" in queries.RATING_AGGS_WORKS
    assert "SUM(rating_dist_total)" not in queries.RATING_AGGS_WORKS


def test_every_unit_aware_tool_validates_its_unit_argument():
    """
    An unvalidated unit would fall through to the editions branch silently,
    so a typo would return edition counts labelled as works.
    """
    src = inspect.getsource(server)
    for part in src.split("@mcp.tool\n")[1:]:
        name = part[4:part.index("(")]
        if "unit:" not in part.split(") -> dict")[0]:
            continue
        assert "require_unit(unit)" in part, f"{name} does not validate unit"
        assert '"unit": unit' in part, f"{name} does not echo unit in filters"
        assert "_unit_caveats(unit)" in part, f"{name} does not caveat its unit"


def test_stats_by_author_defaults_to_works_and_publisher_to_editions():
    """
    The default encodes the question each tool is usually asked: most-read
    author is a question about works, most prolific publisher about editions.
    """
    import inspect as _i

    def default_unit(name):
        fn = getattr(server, name)
        fn = getattr(fn, "fn", fn)
        return _i.signature(fn).parameters["unit"].default

    assert default_unit("stats_by_author") == "works"
    assert default_unit("stats_by_publisher") == "editions"
    assert default_unit("stats_by_language") == "editions"
    assert default_unit("stats_by_year") == "editions"
    assert default_unit("top_books_by_rating") == "editions"


# --- work_key: the dedup identity, distinct from the join key ---------------


def test_work_key_and_title_norm_are_different_keys():
    """
    title_norm reproduces the cleaning script's book_title_normalised and
    must not change, or the documented 52,016-title join coverage breaks.
    work_key is free to differ and does.
    """
    assert queries.work_key("name") != queries.title_norm("name")
    # work_key is built on top of title_norm, not a reimplementation of it.
    assert queries.title_norm("name") in queries.work_key("name")


def test_only_the_join_uses_title_norm():
    """
    Every work-level code path must use work_key. A stray title_norm in a
    dedup path would silently re-merge the boxed sets.
    """
    src = inspect.getsource(server)
    tool = server.compare_user_vs_book_ratings
    join_fn = inspect.getsource(getattr(tool, "fn", tool))
    outside_join = src.replace(join_fn, "")
    assert 'title_norm("name")' in join_fn, "the join must keep title_norm"
    assert 'title_norm("name")' not in outside_join, (
        "a non-join tool uses title_norm; work-level dedup must use work_key"
    )


def test_work_key_preserves_ranges_and_drops_volume_numbers():
    """
    The rule that makes the key correct in both directions. A range denotes a
    different product (a boxed set); a volume number is series metadata about
    the same work, and splitting on it would break the many editions that
    omit the suffix -- and Narnia, numbered both #1 and #2.
    """
    key = queries.work_key("name")
    # The range alternation must require two numbers around a dash.
    assert r"(\d+\s*[-–]\s*\d+)" in key, "work_key must capture a RANGE only"
    # An en dash as well as a hyphen, since the dump uses both.
    assert "–" in key
    # The captured range is appended after punctuation stripping, so its
    # '#' and '-' survive; appending before would strip them back out.
    assert key.index("[^\\p{L}\\p{N}_\\s]") < key.index(r"(\d+\s*[-–]\s*\d+)")


def test_edition_metrics_measure_the_same_work_identity_as_the_collapse():
    """
    n_distinct_titles previews what unit="works" would collapse to. If it used
    a different key than the collapse, the preview would contradict the result.
    """
    assert queries.work_key("name") in queries.RATING_AGGS
    assert queries.title_norm("name") not in queries.RATING_AGGS.replace(
        queries.work_key("name"), ""
    )


# --- telemetry --------------------------------------------------------------
#
# stdout is the MCP protocol channel. A byte written there corrupts JSON-RPC
# framing and kills the connection silently, so the first test below is the
# important one.


import ast as _ast
import contextlib
import io
import json as _json
import os
import pkgutil
import sys
import tempfile

from goodreads_mcp import telemetry


@contextlib.contextmanager
def _telemetry_to(tmpdir):
    """Point telemetry at a scratch file and guarantee it is enabled."""
    path = os.path.join(tmpdir, "telemetry.jsonl")
    prev_path = os.environ.get("GOODREADS_TELEMETRY_PATH")
    prev_on = os.environ.get("GOODREADS_TELEMETRY")
    os.environ["GOODREADS_TELEMETRY_PATH"] = path
    os.environ["GOODREADS_TELEMETRY"] = "1"
    try:
        yield path
    finally:
        for key, val in (("GOODREADS_TELEMETRY_PATH", prev_path),
                         ("GOODREADS_TELEMETRY", prev_on)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return [_json.loads(line) for line in fh if line.strip()]


def _call(name, **kw):
    fn = getattr(server, name)
    return getattr(fn, "fn", fn)(**kw)


def test_every_tool_is_instrumented():
    """
    A tool cannot be added without telemetry -- same structural enforcement as
    the query guards. Checks the source pairing and the runtime marker.
    """
    src = inspect.getsource(server)
    plain = re.findall(r"@mcp\.tool\n(?!@telemetry\.instrument)", src)
    assert not plain, f"{len(plain)} @mcp.tool without @telemetry.instrument"

    declared = re.findall(r"@mcp\.tool\n@telemetry\.instrument\ndef (\w+)", src)
    assert len(declared) >= 12, f"expected 12+ tools, found {len(declared)}"
    for name in declared:
        fn = getattr(server, name)
        assert getattr(fn, "_telemetry_instrumented", False), name


def test_tool_calls_write_nothing_to_stdout():
    """
    THE critical one. Captured at the file-descriptor level, not by swapping
    sys.stdout, so a C-level or subprocess write would still be caught.
    """
    with tempfile.TemporaryDirectory() as tmp, _telemetry_to(tmp) as path:
        saved_fd = os.dup(1)
        capture = os.open(os.path.join(tmp, "stdout.bin"), os.O_RDWR | os.O_CREAT)
        try:
            os.dup2(capture, 1)
            # A parameter failure: exercises the full instrumented path,
            # including a telemetry write, without needing BigQuery.
            _call("top_books_by_rating", min_ratings=0)
            sys.stdout.flush()
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            os.close(capture)
        with open(os.path.join(tmp, "stdout.bin"), "rb") as fh:
            written = fh.read()
        assert written == b"", f"tool call wrote to stdout: {written[:200]!r}"
        assert _read(path), "telemetry line was not written"


def test_no_server_module_can_reach_stdout():
    """
    Package-wide, AST-level. stdout is the protocol channel, so no module the
    server imports may reference it or print() without an explicit stderr
    target -- and none may call logging.basicConfig, whose default is stderr
    but whose stream= kwarg is one edit away from stdout.

    telemetry_cli is exempt: it is a separate process whose whole job is to
    print a report, and it never imports the server.
    """
    import goodreads_mcp

    exempt = {"goodreads_mcp.telemetry_cli", "goodreads_mcp.telemetry_stdout"}
    checked = []
    for mod in pkgutil.iter_modules(goodreads_mcp.__path__):
        name = f"goodreads_mcp.{mod.name}"
        if name in exempt:
            continue
        path = os.path.join(goodreads_mcp.__path__[0], f"{mod.name}.py")
        if not os.path.exists(path):
            continue
        checked.append(name)
        with open(path, encoding="utf-8") as fh:
            tree = _ast.parse(fh.read(), filename=path)

        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute) and node.attr == "stdout":
                raise AssertionError(f"{name} references stdout at line {node.lineno}")
            if not isinstance(node, _ast.Call):
                continue
            func = _ast.unparse(node.func)
            assert "basicConfig" not in func, f"{name}:{node.lineno} calls {func}"
            if func == "print":
                target = next(
                    (_ast.unparse(k.value) for k in node.keywords if k.arg == "file"),
                    None,
                )
                assert target == "sys.stderr", (
                    f"{name}:{node.lineno} print() targets {target or 'stdout'}"
                )
    assert "goodreads_mcp.server" in checked and "goodreads_mcp.telemetry" in checked


def test_log_line_is_valid_json_with_the_required_fields():
    with tempfile.TemporaryDirectory() as tmp, _telemetry_to(tmp) as path:
        _call("top_books_by_rating", min_ratings=0)   # in-band param failure
        (line,) = _read(path)                          # parses => valid JSON
    for field in ("ts", "tool", "params", "outcome", "n_rows", "n_queries",
                  "bytes_billed", "bytes_processed", "cache_hit", "job_ids",
                  "duration_ms", "bq_ms", "overhead_ms"):
        assert field in line, field
    assert line["tool"] == "top_books_by_rating"
    assert line["params"] == {"min_ratings": 0}, "params should be as passed"
    assert line["outcome"] == "other_error"
    assert line["error_type"] == "ParamError"


def test_guard_rejection_is_logged_with_its_rule_and_not_the_sql():
    """
    A guard rejection must record which rule fired and the offending column,
    and must never record the statement that tripped it.
    """
    with tempfile.TemporaryDirectory() as tmp, _telemetry_to(tmp) as path:

        @telemetry.instrument
        def offending_tool():
            bq.guard("SELECT publish_day FROM `t` WHERE x = 1")

        with pytest.raises(bq.QueryGuardError):
            offending_tool()
        (line,) = _read(path)

    assert line["outcome"] == "guard_rejected"
    assert line["guard_rule"] == "publish_day_banned"
    assert line["guard_column"] == "publish_day"
    blob = _json.dumps(line)
    assert "SELECT" not in blob and "FROM" not in blob, "SQL leaked into the log"


def test_guard_rules_are_all_distinctly_identified():
    """Every guard branch must carry a rule id, or telemetry cannot attribute it."""
    seen = set()
    for sql in ("SELECT publish_day FROM t",
                "SELECT language FROM t",
                "SELECT * FROM t"):
        with pytest.raises(bq.QueryGuardError) as ei:
            bq.guard(sql)
        assert ei.value.rule, sql
        seen.add(ei.value.rule)
    assert seen == {"publish_day_banned", "bare_language", "select_star"}


def test_telemetry_can_be_disabled():
    with tempfile.TemporaryDirectory() as tmp, _telemetry_to(tmp) as path:
        os.environ["GOODREADS_TELEMETRY"] = "0"
        _call("top_books_by_rating", min_ratings=0)
        assert not os.path.exists(path), "log written while disabled"


def test_telemetry_failure_does_not_break_a_tool_call():
    """A telemetry problem must degrade to a stderr note, never fail the call."""
    with tempfile.TemporaryDirectory() as tmp:
        # A regular file where a directory is needed: mkdir raises inside the
        # writer, which must swallow it.
        blocker = os.path.join(tmp, "blocker")
        open(blocker, "w").close()
        os.environ["GOODREADS_TELEMETRY_PATH"] = os.path.join(blocker, "sub", "t.jsonl")
        os.environ["GOODREADS_TELEMETRY"] = "1"
        try:
            out = _call("top_books_by_rating", min_ratings=0)
            assert "error" in out, "the tool's own result should be unaffected"
        finally:
            os.environ.pop("GOODREADS_TELEMETRY_PATH", None)
            os.environ.pop("GOODREADS_TELEMETRY", None)


# --- transport-scoped stdout ban --------------------------------------------
#
# Under HTTP, stdout is not the protocol channel and Cloud Run wants logs
# there. The ban is NOT relaxed for that: it is scoped by the import graph, so
# under stdio the stdout-writing code is absent from the process. These tests
# pin that scoping, which is what makes the AST exemption above narrow enough
# to be safe.

import subprocess
import textwrap
import time


def test_transport_defaults_to_stdio_the_restrictive_mode():
    """A missing set_transport() must fail safe, not open stdout."""
    src = inspect.getsource(telemetry)
    assert '_TRANSPORT = "stdio"' in src, "the module default must be stdio"


def test_stdout_sink_refuses_to_arm_under_stdio():
    """
    Even a direct import cannot arm it. activate() re-checks the transport, so
    the guarantee does not rest on the import site alone.
    """
    from goodreads_mcp import telemetry_stdout

    telemetry.set_transport("stdio")
    with pytest.raises(RuntimeError, match="cannot be used under stdio"):
        telemetry_stdout.activate()


def test_requesting_the_stdout_sink_under_stdio_raises():
    prev = os.environ.get("GOODREADS_TELEMETRY_SINK")
    os.environ["GOODREADS_TELEMETRY_SINK"] = "stdout"
    try:
        with pytest.raises(RuntimeError, match="cannot be used under stdio"):
            telemetry.set_transport("stdio")
    finally:
        if prev is None:
            os.environ.pop("GOODREADS_TELEMETRY_SINK", None)
        else:
            os.environ["GOODREADS_TELEMETRY_SINK"] = prev
        telemetry.set_transport("stdio")


def test_stdout_sink_is_never_imported_under_stdio():
    """
    The strong claim: not merely unused, but absent from the process.

    Run in a clean interpreter, because any other test importing the module
    would pollute sys.modules and make an in-process check meaningless. The
    subprocess also doubles as a second stdout check -- its entire stdout must
    be the single OK line.
    """
    with tempfile.TemporaryDirectory() as tmp:
        code = textwrap.dedent(
            """
            import os, sys
            os.environ["GOODREADS_TELEMETRY"] = "1"
            os.environ["GOODREADS_TELEMETRY_PATH"] = sys.argv[1]
            os.environ.pop("GOODREADS_TELEMETRY_SINK", None)

            from goodreads_mcp import telemetry, server
            telemetry.set_transport("stdio")
            server.top_books_by_rating(min_ratings=0)   # in-band failure, no BigQuery

            assert "goodreads_mcp.telemetry_stdout" not in sys.modules, \\
                "the stdout sink was imported under stdio transport"
            sys.stdout.write("OK")
            """
        )
        log = os.path.join(tmp, "t.jsonl")
        proc = subprocess.run(
            [sys.executable, "-c", code, log],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout == "OK", f"unexpected stdout: {proc.stdout[:300]!r}"
        assert os.path.exists(log), "telemetry did not reach the file sink"


def test_stdout_sink_emits_one_valid_json_object_per_line_under_http():
    """
    Cloud Logging parses one JSON object per line into jsonPayload and
    promotes `severity`. Both are load-bearing, so both are asserted.
    """
    from goodreads_mcp import telemetry_stdout

    telemetry.set_transport("http")
    try:
        buf = io.StringIO()
        real, sys.stdout = sys.stdout, buf
        try:
            telemetry_stdout._write(
                {"tool": "stats_by_author", "outcome": "guard_rejected",
                 "duration_ms": 12.5, "guard_rule": "publish_day_banned"}
            )
            telemetry_stdout._write(
                {"tool": "stats_by_year", "outcome": "ok", "duration_ms": 3.0}
            )
        finally:
            sys.stdout = real
    finally:
        telemetry.set_transport("stdio")

    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2
    first, second = (_json.loads(l) for l in lines)
    assert first["severity"] == "WARNING"      # a rejection is a caller mistake
    assert second["severity"] == "INFO"
    assert first["guard_rule"] == "publish_day_banned"
    assert "message" in first


def test_http_transport_selects_the_stdout_sink():
    telemetry.set_transport("http")
    try:
        from goodreads_mcp import telemetry_stdout
        assert telemetry._SINK is telemetry_stdout._write
    finally:
        telemetry.set_transport("stdio")
        assert telemetry._SINK is telemetry.write_to_file


# --- deployment invariants ---------------------------------------------------


def test_stdio_remains_the_default_transport():
    """The existing local Claude Code registration must keep working unchanged."""
    assert server._parse_args([]).transport == "stdio"
    assert server._parse_args(["--transport", "http"]).transport == "http"


def test_http_binds_all_interfaces_and_honours_PORT():
    """Cloud Run requires listening on 0.0.0.0:$PORT."""
    assert server._parse_args([]).host == "0.0.0.0"
    prev = os.environ.get("PORT")
    os.environ["PORT"] = "9137"
    try:
        assert server._parse_args([]).port == 9137
    finally:
        if prev is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = prev
    assert server._parse_args([]).port == 8080 or prev is not None


def test_streamable_http_not_sse():
    """SSE is the legacy transport; the remote case must use streamable HTTP."""
    src = inspect.getsource(server.main)
    assert 'transport="http"' in src
    assert "sse" not in src.replace("# ", "").lower() or 'transport="sse"' not in src
    assert "stateless_http=True" in src, "Cloud Run cannot guarantee session affinity"


def test_max_bytes_billed_survives_the_transport_change():
    """
    The cost ceiling is read from the environment and applied per job, so no
    transport can bypass it. Pinned because it is the only thing standing
    between a bad query and a large bill.
    """
    src = inspect.getsource(bq.run)
    assert "maximum_bytes_billed=MAX_BYTES_BILLED" in src
    assert bq.MAX_BYTES_BILLED > 0
    # Nothing in the transport path may reference or rebind it.
    assert "MAX_BYTES_BILLED" not in inspect.getsource(server.main)


def test_health_route_is_registered_and_does_not_query_bigquery():
    """A probe that queried would bill on every check and fail during a BQ incident."""
    assert hasattr(server, "health")
    src = inspect.getsource(server.health)
    assert "bq.run" not in src and "client()" not in src


def test_dockerfile_bakes_no_credentials_and_sets_the_cost_ceiling():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "Dockerfile"), encoding="utf-8") as fh:
        raw = fh.read()
    # Comments may *mention* the variable to explain why it is absent; what
    # matters is that no directive sets it.
    directives = "\n".join(
        l for l in raw.splitlines() if l.strip() and not l.lstrip().startswith("#")
    )
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in directives
    assert "COPY" not in directives or "credentials" not in directives.lower()
    dockerfile = directives
    assert "GOODREADS_MAX_BYTES_BILLED" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile, "buffered stdout loses telemetry"
    assert "USER 10001" in dockerfile, "must not run as root"


def test_http_mode_keeps_stdout_purely_structured():
    """
    Under HTTP, stdout is the Cloud Logging channel: every line must be a
    single JSON object or the entry lands as unstructured text. uvicorn's
    access log defaults to stdout and would break that, so it is disabled --
    this test is what stops it coming back.

    Uses /health only, so it needs no BigQuery access.
    """
    import socket
    import urllib.request

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]

    env = {**os.environ, "GOODREADS_TRANSPORT": "http", "PORT": str(port),
           "GOODREADS_TELEMETRY_SINK": "stdout", "GOODREADS_TELEMETRY": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "goodreads_mcp"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        deadline = time.time() + 60
        body = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    body = _json.loads(r.read())
                break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    break
                time.sleep(0.4)
        assert body is not None, f"server never came up: {proc.stderr.read()[-1500:]}"
        assert body["status"] == "ok" and body["transport"] == "http"
        # The cost ceiling must survive the transport change.
        assert body["max_bytes_billed"] == bq.MAX_BYTES_BILLED
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=30)

    offenders = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            _json.loads(line)
        except _json.JSONDecodeError:
            offenders.append(line[:120])
    assert not offenders, f"non-JSON lines on stdout under HTTP: {offenders}"
