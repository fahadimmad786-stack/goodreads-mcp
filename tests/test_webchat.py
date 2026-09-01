"""
Invariants of the console BFF. No network, no BigQuery, no Anthropic calls.

What is worth testing here is not the UI but the claims the UI makes: every
caveat can be attached to the figure it qualifies, no numeral in the model's
prose escapes the checker, no credential can reach a client, and -- since the
console has two ways to reach a tool -- that the model-free path is genuinely
the same path, rendering identical frames from forms nobody hand-wrote.
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
from webchat import agent, attach, config, frames, guard_probe, mcp_client, numcheck  # noqa: E402
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


def test_startup_does_not_need_an_anthropic_key(monkeypatch):
    """
    The key buys a mode, not the service. Without it the console still starts,
    still serves, and still answers tool calls -- so it must not be a startup
    requirement, and the access token must still be one.
    """
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    config.verify()
    assert config.chat_enabled() is False
    assert config.public_settings()["chat_enabled"] is False


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


# --- the no-model mode -----------------------------------------------------
#
# The console offers two paths to the same tools. What is worth asserting is
# not that the second one works but that it is the *same* one: identical
# frames, identical caveat attachment, forms nobody wrote by hand, and
# parameters that reach the server exactly as typed -- because the refusal is
# the interesting result and correcting a value on the way in would hide it.


import asyncio  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

WEBCHAT = pathlib.Path(__file__).resolve().parent.parent / "webchat"
TOOLS_JS = (WEBCHAT / "static" / "tools.js").read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """JavaScript with its comments removed, for tests that read the code."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", source)


@pytest.fixture(scope="module")
def schemas():
    """
    The real `tools/list` schemas, in process. No network, no BigQuery --
    listing tools never executes one.
    """
    from fastmcp import Client

    from goodreads_mcp.server import mcp

    async def go():
        async with Client(mcp) as c:
            return {t.name: t.inputSchema for t in await c.list_tools()}

    return asyncio.run(go())


class _StubBridge:
    """The MCP bridge with the transport removed; the schemas are the real ones."""

    def __init__(self, schemas, outcome=None):
        self._catalogue = [
            {"name": name, "description": "", "schema": schema, "origin": "mcp"}
            for name, schema in sorted(schemas.items())
        ]
        self._catalogue.append(
            {
                "name": guard_probe.TOOL_NAME,
                "description": guard_probe.TOOL_DESCRIPTION,
                "schema": guard_probe.TOOL_SCHEMA,
                "origin": "bff",
            }
        )
        self._outcome = outcome
        self.calls = []

    @property
    def auth_mode(self):
        return "proxy"

    async def catalogue(self, refresh=False):
        return self._catalogue

    async def connect_check(self):
        return {"mcp": "ok", "tools": len(self._catalogue), "auth": "proxy"}

    async def describe(self, refresh=False):
        return [], ""

    async def call(self, name, params):
        self.calls.append((name, params))
        if self._outcome is not None:
            return self._outcome
        return ToolOutcome(tool=name, params=params, kind="ok", envelope={"data": []})


@pytest.fixture
def tool_client(schemas):
    """A console whose MCP side is stubbed, so the routes can be exercised offline."""
    app = create_app()
    bridge = _StubBridge(schemas)
    app.state.bridge = bridge
    with TestClient(app) as c:
        c.headers.update({"x-chat-access": ACCESS})
        yield c, bridge


# --- the service runs with no model at all ---------------------------------


def test_the_console_serves_with_no_anthropic_key(monkeypatch):
    """
    The whole point of the mode: a keyless deployment starts, serves the page,
    and reports the tool surface. Only the chat route is gone.
    """
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    app = create_app()
    with TestClient(app) as c:
        assert app.state.agent is None, "an Anthropic client was built with no key"
        assert c.get(f"/?k={ACCESS}").status_code == 200
        body = c.get("/api/health", headers={"x-chat-access": ACCESS}).json()
        assert body["chat_enabled"] is False
        assert body["model"] is None


def test_chat_refuses_cleanly_without_a_key_and_names_the_mode_that_works(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    with TestClient(create_app()) as c:
        r = c.post("/api/chat", json={"text": "hi"}, headers={"x-chat-access": ACCESS})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["error"]
    assert "tool mode" in r.json()["error"]


def test_the_tool_routes_work_without_a_key(monkeypatch, schemas):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    app = create_app()
    app.state.bridge = _StubBridge(schemas)
    with TestClient(app) as c:
        r = c.post(
            "/api/run",
            json={"tool": "stats_by_author", "params": {"unit": "works"}},
            headers={"x-chat-access": ACCESS},
        )
    assert r.status_code == 200
    assert r.json()["type"] == "tool_result"


def test_the_anthropic_sdk_is_not_imported_by_the_tool_path():
    """
    Structural, not incidental: `frames.py` exists so tool mode can build a
    card without importing the module that constructs an Anthropic client.
    """
    tree = ast.parse((WEBCHAT / "frames.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(m.startswith("anthropic") or m.endswith("agent") for m in imported)


# --- both modes render the same frame --------------------------------------


def test_both_modes_build_their_frames_with_the_same_function():
    """Not "equivalent" -- the same object. Two builders would drift."""
    assert agent._result_frame is frames._result_frame
    assert agent._origin is frames._origin


def test_a_form_run_returns_the_frame_chat_mode_would_have_returned(schemas):
    """
    The claim the mode rests on: everything downstream is unchanged. So the
    route's JSON must equal, field for field, what the streaming path emits for
    the same outcome -- only the call id differs.
    """
    envelope = _call("top_books_by_rating", min_ratings=0)
    outcome = ToolOutcome(
        tool="top_books_by_rating",
        params={"min_ratings": 0},
        kind="param_error",
        envelope=envelope,
        message=envelope["error"],
        caveats=list(envelope["caveats"]),
    )
    app = create_app()
    app.state.bridge = _StubBridge(schemas, outcome=outcome)
    with TestClient(app) as c:
        got = c.post(
            "/api/run",
            json={"tool": "top_books_by_rating", "params": {"min_ratings": 0}},
            headers={"x-chat-access": ACCESS},
        ).json()

    expected = frames._result_frame(got["id"], outcome)
    assert got == json.loads(json.dumps(expected, default=str))
    assert got["type"] == "tool_refusal"
    assert "451,777" in got["message"]
    # Attached, in the server's own order, exactly as the streaming path sends.
    assert [c["id"] for c in got["caveats"]] == [
        c["id"] for c in expected["caveats"]
    ]
    assert "unrated_books" in [c["id"] for c in got["caveats"]]


def test_the_guard_probe_is_offered_in_tool_mode_and_still_labelled_bff(tool_client):
    client, _ = tool_client
    catalogue = client.get("/api/tools").json()["tools"]
    probe = next(t for t in catalogue if t["name"] == guard_probe.TOOL_NAME)
    assert probe["origin"] == "bff"
    assert "demonstration probe" in probe["description"].lower()
    assert probe["schema"]["properties"]["column"]["description"]


def test_running_the_probe_from_a_form_reaches_the_real_guard(schemas):
    """
    The real bridge, with its discovery cache pre-seeded rather than fetched,
    so the genuine `catalogue()` and `call()` run offline. The probe needs no
    network: `bq.guard()` is a pure text check.
    """
    bridge = mcp_client.MCPBridge()
    bridge._tools = [
        {"name": name, "description": "", "input_schema": schema}
        for name, schema in sorted(schemas.items())
    ] + [guard_probe.anthropic_tool()]
    app = create_app()
    app.state.bridge = bridge
    with TestClient(app) as c:
        got = c.post(
            "/api/run",
            json={"tool": guard_probe.TOOL_NAME, "params": {"column": "publish_day"}},
            headers={"x-chat-access": ACCESS},
        ).json()
    assert got["kind"] == "probe"
    assert got["origin"] == "bff"
    assert got["envelope"]["rule"] == "publish_day_banned"
    assert got["envelope"]["executed"] is False


# --- the form is generated, not written ------------------------------------


def test_the_catalogue_hands_over_the_schemas_the_model_is_given(schemas, tool_client):
    """
    One fetch, one description. If the form were built from a second source it
    could disagree with the model's tools about what a parameter accepts.
    """
    client, _ = tool_client
    catalogue = {t["name"]: t["schema"] for t in client.get("/api/tools").json()["tools"]}
    for name, schema in schemas.items():
        assert catalogue[name] == schema


def test_no_tool_parameter_name_is_written_into_the_client(schemas):
    """
    THE test for "don't hand-write forms". Every widget, label, bound and
    default is read from the schema at runtime, so no parameter name may appear
    in tools.js outside the preset values -- which are starting points for the
    form, not the form itself.
    """
    # Comments are stripped first: the header explains the discipline by
    # example ("min_ratings=0 ... reach the server unaltered"), which is prose
    # about the rule, not an instance of breaking it.
    body = _strip_comments(TOOLS_JS).split("const PRESETS = [", 1)
    without_presets = body[0] + body[1].split("];", 1)[1]
    names = {n for schema in schemas.values() for n in (schema.get("properties") or {})}
    leaked = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", without_presets))
    assert not leaked, f"tools.js hard-codes tool parameters: {leaked}"


def test_the_form_generator_covers_every_type_the_server_emits(schemas):
    """
    A parameter whose type the widget builder does not handle would silently
    render as a text box with no bounds shown. Assert the set of types actually
    emitted is the set the client dispatches on.
    """
    emitted = set()
    for schema in schemas.values():
        for spec in (schema.get("properties") or {}).values():
            for branch in [spec, *(spec.get("anyOf") or [])]:
                if isinstance(branch.get("type"), str):
                    emitted.add(branch["type"])
    assert emitted <= {"integer", "number", "string", "boolean", "null"}
    for name in emitted - {"null"}:
        assert f"'{name}'" in TOOLS_JS, f"tools.js does not handle type {name}"


def test_the_form_generator_reads_every_constraint_keyword_the_server_emits(schemas):
    """
    `exclusiveMinimum` is the one that catches this: bucket_size carries it and
    not `minimum`, so a reader that only knew `minimum` would print no lower
    bound for the one parameter that has an unusual one.
    """
    emitted = set()
    for schema in schemas.values():
        for spec in (schema.get("properties") or {}).values():
            emitted.update(spec.keys())
            for branch in spec.get("anyOf") or []:
                emitted.update(branch.keys())
    for keyword in emitted - {"title", "type", "anyOf"}:
        assert keyword in TOOLS_JS, f"tools.js ignores the schema keyword {keyword}"


def test_every_tool_parameter_carries_a_description_for_the_form_to_show(schemas):
    """
    The form promises the server's own words for each parameter. A `Field`
    added without a description would render a blank line, so fail here
    instead.
    """
    missing = [
        f"{tool}.{name}"
        for tool, schema in schemas.items()
        for name, spec in (schema.get("properties") or {}).items()
        if not (spec.get("description") or "").strip()
    ]
    assert not missing, f"tool parameters with no description: {missing}"


def test_every_preset_names_a_real_tool_and_real_parameters(schemas):
    """
    The presets are the only tool knowledge in the client, so they are the only
    thing that can go stale. A preset naming a dropped parameter would be
    silently ignored by the form.
    """
    block = TOOLS_JS.split("const PRESETS = [", 1)[1].split("];", 1)[0]
    known = dict(schemas)
    known[guard_probe.TOOL_NAME] = guard_probe.TOOL_SCHEMA
    for entry in re.finditer(r"tool: '([a-z_]+)',\s*\n?\s*params: \{([^}]*)\}", block):
        tool, params = entry.group(1), entry.group(2)
        assert tool in known, f"preset names an unknown tool: {tool}"
        properties = known[tool].get("properties") or {}
        for key in re.findall(r"(\w+):", params):
            assert key in properties, f"preset for {tool} sets unknown parameter {key}"


# --- parameters reach the server as typed ----------------------------------


def test_a_refused_value_is_passed_through_untouched(tool_client):
    """
    The mode's central discipline. `min_ratings=0` must arrive at the server as
    0: substituting the default would replace the most instructive refusal in
    the tool surface with a plausible-looking answer.
    """
    client, bridge = tool_client
    client.post("/api/run", json={"tool": "top_books_by_rating", "params": {"min_ratings": 0}})
    client.post("/api/run", json={"tool": "stats_by_author", "params": {"unit": "chapters"}})
    assert bridge.calls == [
        ("top_books_by_rating", {"min_ratings": 0}),
        ("stats_by_author", {"unit": "chapters"}),
    ]


def test_an_omitted_parameter_is_not_sent_at_all(tool_client):
    """
    An empty field means "use the server's default", which is expressed by
    sending nothing -- so the card's parameter row shows what was overridden
    rather than a wall of values nobody chose.
    """
    client, bridge = tool_client
    client.post("/api/run", json={"tool": "stats_by_year", "params": {}})
    assert bridge.calls == [("stats_by_year", {})]


def test_run_refuses_a_tool_it_does_not_know(tool_client):
    client, bridge = tool_client
    r = client.post("/api/run", json={"tool": "drop_tables", "params": {}})
    assert r.status_code == 400 and r.json()["error"] == "no such tool"
    assert bridge.calls == []


def test_run_takes_scalars_only(tool_client):
    """
    Structural, not a judgement on any value: a form field is a scalar, so a
    nested object is something this UI cannot have produced.
    """
    client, bridge = tool_client
    for params in ({"a": {"nested": 1}}, {"a": [1, 2]}, "not-an-object"):
        r = client.post("/api/run", json={"tool": "stats_by_year", "params": params})
        assert r.status_code == 400, params
    r = client.post(
        "/api/run",
        json={"tool": "stats_by_year", "params": {"language": "x" * (config.MAX_PARAM_CHARS + 1)}},
    )
    assert r.status_code == 400
    assert bridge.calls == []


def test_the_tool_routes_are_behind_the_access_token(schemas):
    fresh = TestClient(create_app())
    fresh.app.state.bridge = _StubBridge(schemas)
    assert fresh.get("/api/tools").status_code == 401
    assert fresh.post("/api/run", json={"tool": "stats_by_year"}).status_code == 401


def test_tool_mode_has_its_own_spend_window(tool_client, monkeypatch):
    """
    A form submission bills BigQuery but no Anthropic tokens, and one call per
    form is a tighter loop than one call per sentence -- so the two windows are
    separate, and the tool one is still a window.
    """
    client, _ = tool_client
    client.app.state.tool_limiter = RateLimiter(limit=2, window_s=600)
    for _ in range(2):
        assert client.post("/api/run", json={"tool": "stats_by_year", "params": {}}).status_code == 200
    blocked = client.post("/api/run", json={"tool": "stats_by_year", "params": {}})
    assert blocked.status_code == 429
    assert "tool calls" in blocked.json()["error"]
