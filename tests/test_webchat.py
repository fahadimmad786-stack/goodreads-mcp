"""
Invariants of the chat BFF. No network, no BigQuery, no Anthropic calls.

What is worth testing here is not the UI but the three claims the UI makes:
every caveat can be attached to the figure it qualifies, no numeral in the
model's prose escapes the checker, and no credential can reach a client.
"""

from __future__ import annotations

import ast
import os
import pkgutil

import pytest

# Both are mandatory at import time, so they are set before the package loads.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
os.environ.setdefault("CHAT_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("CHAT_COOKIE_SECURE", "0")

from starlette.testclient import TestClient  # noqa: E402

import goodreads_mcp  # noqa: E402
from goodreads_mcp import caveats, server  # noqa: E402
from webchat import agent, attach, config, guard_probe, mcp_client, numcheck  # noqa: E402
from webchat.app import create_app  # noqa: E402
from webchat.mcp_client import ToolOutcome  # noqa: E402
from webchat.session import RateLimiter, SessionStore  # noqa: E402

ACCESS = os.environ["CHAT_ACCESS_TOKEN"]


def _call(name, **kw):
    fn = getattr(server, name)
    return getattr(fn, "fn", fn)(**kw)


# --- caveats stay attached -------------------------------------------------


def test_every_registered_caveat_has_a_field_mapping():
    """
    THE important one for the UI's central claim. Adding a caveat to the server
    without saying which figures it qualifies fails here, rather than silently
    rendering an unattached caveat in the console.
    """
    unmapped = set(caveats._REGISTRY) - set(attach.FIELDS)
    assert not unmapped, f"caveats with no field mapping: {sorted(unmapped)}"


def test_field_mapping_names_no_caveat_the_server_does_not_have():
    stale = set(attach.FIELDS) - set(caveats._REGISTRY)
    assert not stale, f"FIELDS names caveats that no longer exist: {sorted(stale)}"


def test_every_rendered_caveat_resolves_back_to_its_id():
    """
    The id is recovered by exact match on rendered text. If collect() ever
    stops rendering deterministically, every caveat in the console silently
    loses its field attachment -- so assert the round trip for all of them.
    """
    for cid in caveats._REGISTRY:
        rendered = caveats.collect(cid)
        structured = attach.structure(rendered)
        assert structured[0]["id"] == cid
        assert structured[0]["source"]
        assert structured[0]["text"]


def test_unrecognised_caveat_text_is_kept_not_dropped():
    out = attach.structure(["[measured] something the registry has never heard of"])
    assert len(out) == 1
    assert out[0]["id"] is None
    assert out[0]["fields"] == []
    assert "never heard of" in out[0]["text"]


def test_structure_preserves_order():
    rendered = caveats.collect("dual_average", "unrated_books", "rating_skew")
    ids = [c["id"] for c in attach.structure(rendered)]
    assert ids == ["dual_average", "unrated_books", "rating_skew"]


def test_the_duplication_caveat_attaches_to_the_figures_it_overcounts():
    """n_ratings and pooled_rating are the two figures it actually distorts."""
    fields = attach.FIELDS["edition_duplication"]
    assert "n_ratings" in fields and "pooled_rating" in fields


# --- the rendering contract is checked, not trusted ------------------------


def test_exact_quotation_passes_the_checker():
    envelope = {"data": [{"authors": "X", "n_ratings": 118436207, "pooled_rating": 4.47}]}
    sourced = numcheck.collect(envelope)
    assert numcheck.check("It reaches 118,436,207 ratings at 4.47.", sourced) == []


def test_a_rounded_figure_is_reported():
    """Rounding is one of the things the contract bans, so it must not pass."""
    sourced = numcheck.collect({"n_ratings": 118436207, "pooled_rating": 4.47})
    found = [f["value"] for f in numcheck.check("about 118 million, near 4.5", sourced)]
    assert "118" in found and "4.5" in found


def test_an_invented_figure_is_reported():
    sourced = numcheck.collect({"n_books": 10})
    found = numcheck.check("There are 4,732 works.", sourced)
    assert len(found) == 1 and found[0]["value"] == "4,732"


def test_numbers_inside_caveat_prose_count_as_sourced():
    """
    The model is allowed to repeat a caveat's own figures -- the server said
    them. Anything else in the same sentence is still checked.
    """
    envelope = {"caveats": caveats.collect("unrated_books")}
    sourced = numcheck.collect(envelope)
    assert numcheck.check("451,777 books carry no ratings.", sourced) == []
    assert numcheck.check("999,999 books carry no ratings.", sourced) != []


def test_magnitude_suffix_matches_either_written_form():
    sourced = numcheck.numerals_in_text("a 1.85M-row table")
    assert numcheck.check("the 1.85M-row table", sourced) == []
    assert numcheck.check("1,850,000 rows", sourced) == []
    # But not the exact row count, which nothing here stated.
    assert numcheck.check("1,850,115 rows", sourced) != []


def test_offsets_point_at_the_numeral():
    text = "the answer is 4,732 works"
    found = numcheck.check(text, set())
    assert text[found[0]["start"] : found[0]["end"]].strip() == "4,732"


def test_prose_with_no_numerals_always_passes():
    assert numcheck.check("The top row leads by a wide margin.", set()) == []


def test_checker_walks_nested_structures_and_keys():
    sourced = numcheck.collect({"star_share_pct": {"5_star": 61.3}, "rows": [[1234]]})
    assert numcheck.check("61.3 percent of 1,234, all 5 star", sourced) == []


def test_the_contract_is_stated_in_the_system_prompt():
    """
    The checker reports violations; the prompt is what prevents them. Both have
    to exist -- a checker with no instruction just decorates every answer.
    """
    text = agent.CONTRACT.lower()
    assert "do not write a numeral" in text
    assert "rounds" in text or "rounding" in text
    assert guard_probe.TOOL_NAME in agent.CONTRACT


# --- refusals are results --------------------------------------------------


def test_a_param_error_from_the_real_server_is_rendered_as_a_refusal():
    """
    min_ratings=0 is the live refusal path: the server returns a structured
    result, not an exception. Assert the console classifies it as a refusal and
    keeps the server's explanation and caveats intact.
    """
    result = _call("top_books_by_rating", min_ratings=0)
    assert "error" in result and "451,777" in result["error"]

    outcome = ToolOutcome(
        tool="top_books_by_rating",
        params={"min_ratings": 0},
        kind="param_error",
        envelope=result,
        message=result["error"],
        caveats=list(result["caveats"]),
    )
    frame = agent._result_frame("call_1", outcome)
    assert frame["type"] == "tool_refusal"
    assert frame["kind"] == "param_error"
    assert "451,777" in frame["message"]
    assert frame["caveats"] and all(c["id"] for c in frame["caveats"])


def test_guard_rejections_classify_as_refusals_not_crashes():
    from goodreads_mcp import bq

    for sql in (
        "SELECT publish_day FROM t",
        "SELECT language FROM t",
        "SELECT * FROM t",
    ):
        with pytest.raises(bq.QueryGuardError) as exc:
            bq.guard(sql)
        assert mcp_client.classify_error(str(exc.value)) == "guard"


def test_a_transport_failure_is_not_mistaken_for_a_guard_rejection():
    assert mcp_client.classify_error("All connection attempts failed") == "transport"
    assert mcp_client.classify_error("503 Service Unavailable") == "transport"


def test_a_successful_result_carries_structured_caveats_not_prose():
    outcome = ToolOutcome(
        tool="stats_by_author",
        params={"unit": "works"},
        kind="ok",
        envelope={
            "data": [],
            "n": {},
            "caveats": caveats.collect("edition_duplication", "dual_average"),
            "query_meta": {},
        },
    )
    frame = agent._result_frame("c", outcome)
    assert frame["type"] == "tool_result"
    ids = [c["id"] for c in frame["envelope"]["caveats"]]
    assert ids == ["edition_duplication", "dual_average"]
    assert all(isinstance(c, dict) for c in frame["envelope"]["caveats"])


# --- the guard probe -------------------------------------------------------


def test_the_probe_runs_the_real_guard_and_reports_its_rule():
    result = guard_probe.probe("publish_day")
    assert result["rejected"] is True
    assert result["rule"] == "publish_day_banned"
    assert result["guard_column"] == "publish_day"
    assert result["executed"] is False
    assert "placeholder" in result["message"]
    assert result["caveats"][0]["id"] == "publish_day_unusable"


def test_the_probe_also_covers_the_bare_language_column():
    result = guard_probe.probe("language")
    assert result["rejected"] is True and result["rule"] == "bare_language"


def test_the_probe_does_not_claim_a_tool_exists_for_an_allowed_column():
    result = guard_probe.probe("pages_number")
    assert result["rejected"] is False
    assert "no tool returns raw column values" in result["message"]


def test_the_probe_refuses_anything_that_is_not_a_bare_identifier():
    """It reports on columns; it is not a route for arbitrary SQL."""
    for candidate in ("1; DROP TABLE books", "name, publish_day", "* FROM t --"):
        result = guard_probe.probe(candidate)
        assert result["verdict"] == "not_a_column"
        assert result["candidate_sql"] is None


def test_the_probe_never_builds_a_bigquery_client():
    """
    The probe is a text check. If it ever constructed a client it would need
    credentials this service deliberately does not have.
    """
    from goodreads_mcp import bq

    bq.client.cache_clear()
    guard_probe.probe("publish_day")
    guard_probe.probe("pages_number")
    assert bq.client.cache_info().currsize == 0


def test_the_probe_is_labelled_as_a_demonstration_everywhere_it_appears():
    assert "demonstration probe" in guard_probe.TOOL_DESCRIPTION.lower()
    assert "demonstration probe" in agent.CONTRACT.lower()
    readme = (
        __import__("pathlib").Path(__file__).resolve().parent.parent / "README.md"
    ).read_text(encoding="utf-8")
    assert "demonstration probe" in readme.lower()


def test_the_probe_is_not_an_mcp_tool():
    """
    It must never look like part of the server's tool surface: no @mcp.tool
    anywhere in the package, and the server exposes no tool of that name.
    """
    src = __import__("inspect").getsource(server)
    assert guard_probe.TOOL_NAME not in src
    assert agent._origin(guard_probe.TOOL_NAME) == "bff"
    assert agent._origin("stats_by_year") == "mcp"


# --- credentials never leave the server ------------------------------------


def test_transport_errors_are_scrubbed_of_authorisation_headers():
    dirty = "error sending request: Authorization: Bearer ya29.secret-token-value"
    clean = mcp_client._safe(dirty)
    assert "Bearer" not in clean and "Authorization" not in clean


def test_no_frame_type_carries_a_credential_field():
    """
    Structural: the frames the client receives are built only from these
    functions, and none of them may reference a token.
    """
    for fn in (agent._result_frame,):
        src = __import__("inspect").getsource(fn)
        for banned in ("token", "Authorization", "Bearer", "api_key"):
            assert banned not in src, f"{fn.__name__} mentions {banned}"


def test_the_token_source_sends_nothing_in_proxy_mode():
    """proxy.sh injects the credential; sending one of our own would be wrong."""
    import asyncio

    source = mcp_client.TokenSource(audience=None, static_token=None)
    assert source.mode == "proxy"
    assert asyncio.run(source.header()) == {}


def test_a_static_token_is_used_verbatim_and_only_in_the_header():
    import asyncio

    source = mcp_client.TokenSource(audience=None, static_token="tok123")
    assert asyncio.run(source.header()) == {"Authorization": "Bearer tok123"}


# --- the server package stays independent of this one ----------------------


def test_the_mcp_server_never_imports_the_web_console():
    """
    The stdio path must be unaffected by this package existing. If a server
    module imported it, the stdout ban, the dependency set and the Cloud Run
    image would all change.
    """
    for mod in pkgutil.iter_modules(goodreads_mcp.__path__):
        path = os.path.join(goodreads_mcp.__path__[0], f"{mod.name}.py")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("webchat"), f"{mod.name} imports {name}"


def test_this_package_imports_only_pure_parts_of_the_server():
    """
    Two imports from goodreads_mcp, both credential-free: the caveat registry
    and the query guard. Anything else would put server internals -- or a
    BigQuery client -- inside the public-facing service.
    """
    import pathlib

    allowed = {"goodreads_mcp.caveats", "goodreads_mcp.bq", "goodreads_mcp"}
    root = pathlib.Path(__file__).resolve().parent.parent / "webchat"
    seen = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "goodreads_mcp"
            ):
                seen.add(node.module)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("goodreads_mcp"):
                        seen.add(a.name)
    assert seen <= allowed, f"unexpected server imports: {sorted(seen - allowed)}"


# --- access control and spend ceilings ------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def test_the_console_is_closed_without_the_access_token(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 401
    assert "access key" in r.text


def test_the_access_token_opens_it_and_is_remembered_in_a_cookie(client):
    r = client.get(f"/?k={ACCESS}")
    assert r.status_code == 200
    assert "goodreads-stats console" in r.text
    assert config.AUTH_COOKIE in r.cookies or config.AUTH_COOKIE in client.cookies


def test_a_wrong_token_does_not_open_it(client):
    fresh = TestClient(create_app())
    r = fresh.get("/?k=not-the-token", follow_redirects=False)
    assert r.status_code == 401


def test_chat_requires_the_access_token():
    fresh = TestClient(create_app())
    r = fresh.post("/api/chat", json={"text": "hello"})
    assert r.status_code == 401


def test_chat_rejects_an_over_long_message(client):
    r = client.post(
        "/api/chat",
        json={"text": "x" * (config.MAX_INPUT_CHARS + 1)},
        headers={"x-chat-access": ACCESS},
    )
    assert r.status_code == 400


def test_chat_rejects_an_empty_message(client):
    r = client.post("/api/chat", json={"text": "   "}, headers={"x-chat-access": ACCESS})
    assert r.status_code == 400


def test_public_health_reveals_nothing_about_the_backend():
    fresh = TestClient(create_app())
    body = fresh.get("/api/health").json()
    assert body == {"status": "ok"}
    assert "mcp_url" not in body


def test_startup_refuses_without_an_access_token(monkeypatch):
    """
    The one configuration mistake that would matter: an open endpoint billing
    the Anthropic account. There is deliberately no override flag.
    """
    monkeypatch.setattr(config, "ACCESS_TOKEN", None)
    with pytest.raises(config.ConfigError, match="CHAT_ACCESS_TOKEN"):
        config.verify()


def test_startup_refuses_without_an_anthropic_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    with pytest.raises(config.ConfigError, match="ANTHROPIC_API_KEY"):
        config.verify()


def test_the_two_mcp_auth_modes_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(config, "MCP_AUDIENCE", "https://example.run.app")
    monkeypatch.setattr(config, "MCP_TOKEN", "tok")
    with pytest.raises(config.ConfigError, match="Pick one"):
        config.verify()


def test_the_rate_limiter_closes_its_window():
    limiter = RateLimiter(limit=2, window_s=600)
    assert limiter.check("1.2.3.4")[0] is True
    assert limiter.check("1.2.3.4")[0] is True
    allowed, retry_in = limiter.check("1.2.3.4")
    assert allowed is False and retry_in > 0
    # Per key, not global.
    assert limiter.check("5.6.7.8")[0] is True


def test_sessions_are_capped_and_expire():
    store = SessionStore(max_sessions=3, ttl_s=600)
    ids = [store.new().id for _ in range(5)]
    assert len(store) <= 3
    assert store.get(ids[0]) is None          # evicted
    assert store.get(ids[-1]) is not None     # most recent survives
    assert store.get("no-such-session") is None


def test_a_session_runs_out_of_turns():
    store = SessionStore()
    s = store.new()
    s.turns = config.MAX_TURNS_PER_SESSION
    assert s.turns_left == 0


def test_the_turn_budget_is_declared_to_the_model():
    assert str(config.MAX_TOOL_CALLS_PER_TURN) in agent.CONTRACT


# --- the schema layer above ParamError -------------------------------------
#
# Found in live testing: `Annotated[int, Field(ge=1)]` is validated by FastMCP
# before the tool body runs, so over MCP the most instructive refusal in the
# server -- min_ratings=0 -- never reaches require_min_ratings(). The tests
# above call tool functions directly and so never see this. It must render as a
# refusal, not as a transport failure.


def test_a_schema_validation_error_is_a_refusal_not_a_transport_failure():
    text = (
        "1 validation error for call[top_books_by_rating]\n"
        "min_ratings\n"
        "  Input should be greater than or equal to 1 "
        "[type=greater_than_equal, input_value=0, input_type=int]"
    )
    assert mcp_client.classify_error(text) == "schema"


def test_validation_messages_are_tidied_but_not_rewritten():
    text = (
        "1 validation error for call[top_books_by_rating]\n"
        "min_ratings\n"
        "  Input should be greater than or equal to 1 [type=greater_than_equal]\n"
        "For further information visit https://errors.pydantic.dev/2.13/v/greater_than_equal"
    )
    tidy = mcp_client.tidy_validation(text)
    assert "Input should be greater than or equal to 1" in tidy
    assert "[type=" not in tidy
    assert "pydantic.dev" not in tidy


def test_a_schema_refusal_carries_the_servers_reason_for_the_constraint():
    """
    The validation error says "greater than or equal to 1" and not why. The
    console re-attaches the server's own caveat prose, from the registry -- it
    does not write a second explanation of its own.
    """
    texts = mcp_client._param_caveat_text({"min_ratings": 0, "limit": 20})
    assert texts, "no caveats attached to a min_ratings refusal"
    joined = " ".join(texts)
    assert "451,777" in joined
    structured = attach.structure(texts)
    assert [c["id"] for c in structured] == ["unrated_books", "rating_skew"]


def test_param_caveat_map_names_only_real_caveats():
    stale = {i for ids in attach.PARAM_CAVEATS.values() for i in ids} - set(
        caveats._REGISTRY
    )
    assert not stale, f"PARAM_CAVEATS names caveats that do not exist: {sorted(stale)}"


def test_the_unconstrained_parameters_still_reach_the_body_validator():
    """
    These have no Field bound, so ParamError -- and the server's full
    explanation -- is genuinely reachable through them.
    """
    for name, kwargs in (
        ("stats_by_author", {"unit": "chapters"}),
        ("stats_by_author", {"order_by": "n_books; DROP TABLE books"}),
        ("stats_by_author", {"year_from": 2010, "year_to": 1990}),
    ):
        result = _call(name, **kwargs)
        assert "error" in result, f"{kwargs} did not produce a structured refusal"
        assert result["caveats"]


def test_every_refusal_kind_has_a_cost_line_in_the_ui():
    """
    A refusal kind with no entry in cards.js renders with a fallback that says
    nothing about whether the query was billed.
    """
    import pathlib
    import re

    cards = (
        pathlib.Path(__file__).resolve().parent.parent
        / "webchat" / "static" / "cards.js"
    ).read_text(encoding="utf-8")
    block = cards.split("const REFUSAL_COST = {", 1)[1].split("};", 1)[0]
    declared = set(re.findall(r"^\s*(\w+):", block, re.MULTILINE))
    assert {"schema", "param_error", "guard", "transport", "budget"} <= declared


# --- the renderers read real column names ---------------------------------


def test_every_column_the_cards_read_exists_in_the_servers_sql():
    """
    cards.js addresses figures by column name. A renamed SQL alias in
    server.py or queries.py would leave the console rendering em-dashes with
    no error anywhere -- exactly the silent failure this project avoids
    elsewhere. Verified against the live envelopes once; pinned here offline.
    """
    import inspect
    import pathlib
    import re

    from goodreads_mcp import queries

    cards = (
        pathlib.Path(__file__).resolve().parent.parent
        / "webchat" / "static" / "cards.js"
    ).read_text(encoding="utf-8")

    referenced: set[str] = set()
    # cat: 'authors'  /  value: 'n_ratings'  /  x: 'publish_year'  /  y: 'n_books'
    for match in re.finditer(r"\b(?:cat|value|x|y):\s*'([a-z_0-9]+)'", cards):
        referenced.add(match.group(1))
    # extra: ['n_books', 'pooled_rating', ...]
    for block in re.finditer(r"extra:\s*\[([^\]]*)\]", cards):
        referenced.update(re.findall(r"'([a-z_0-9]+)'", block.group(1)))
    # The nested containers the dict-shaped tools return. Listed rather than
    # scraped: `d.<name>` also matches string methods.
    referenced.update({
        "histogram", "summary", "star_share_pct", "by_band",
        "page_count_quartiles", "star_distribution",
    })

    assert len(referenced) > 20, "the reference scan found almost nothing; check the patterns"

    server_text = inspect.getsource(server) + inspect.getsource(queries)
    # Names produced by the client itself rather than by SQL.
    client_side = {"star", "pct_of_ratings", "data", "summary"}
    missing = sorted(
        name for name in referenced
        if name not in client_side and name not in server_text
    )
    assert not missing, f"cards.js reads names the server never produces: {missing}"
