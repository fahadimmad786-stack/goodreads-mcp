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
from webchat.session import RateLimiter, Session, SessionStore  # noqa: E402

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
    Four imports from goodreads_mcp, all credential-free: the caveat registry,
    the query guard, and the telemetry summariser with the module that names
    its log path. Anything else would put server internals -- or a BigQuery
    client -- inside the public-facing service. (`telemetry` reaches `bq`
    only lazily, inside the decorator the console never applies.)
    """
    import pathlib

    allowed = {
        "goodreads_mcp.caveats", "goodreads_mcp.bq", "goodreads_mcp",
        "goodreads_mcp.telemetry_cli", "goodreads_mcp.telemetry",
    }
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


def _no_model_key(monkeypatch):
    """
    Clear EVERY model key, not just one.

    There are two now, and a test that cleared only Anthropic's would pass on a
    machine with no Gemini key and fail on the developer's, who has one
    exported. "No key" has to mean no key.
    """
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    monkeypatch.setattr(config, "CHAT_PROVIDER", None)


def test_startup_does_not_need_a_model_key(monkeypatch):
    """
    A key buys a mode, not the service. Without one the console still starts,
    still serves, and still answers tool calls -- so it must not be a startup
    requirement, and the access token must still be one.
    """
    _no_model_key(monkeypatch)
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

    static = pathlib.Path(__file__).resolve().parent.parent / "webchat" / "static"
    cards = (static / "cards.js").read_text(encoding="utf-8")
    defects = (static / "defects.js").read_text(encoding="utf-8")

    referenced: set[str] = set()
    # defects.js addresses the overview envelope by dotted path; every leaf
    # must be a name the server produces. The LIVE map is the only place
    # these paths are written.
    live = defects.split("const LIVE = {", 1)[1].split("\n};", 1)[0]
    for path in re.findall(r"'([a-z_0-9.]+)'", live):
        referenced.add(path.rsplit(".", 1)[-1])
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
    assert not missing, f"the renderers read names the server never produces: {missing}"


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
import shutil  # noqa: E402
import subprocess  # noqa: E402

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


def test_the_console_serves_with_no_model_key(monkeypatch):
    """
    The whole point of the mode: a keyless deployment starts, serves the page,
    and reports the tool surface. Only the chat route is gone.
    """
    _no_model_key(monkeypatch)
    app = create_app()
    with TestClient(app) as c:
        assert app.state.agent is None, "a model client was built with no key"
        assert c.get(f"/?k={ACCESS}").status_code == 200
        body = c.get("/api/health", headers={"x-chat-access": ACCESS}).json()
        assert body["chat_enabled"] is False
        assert body["model"] is None
        assert body["provider"] is None


def test_chat_refuses_cleanly_without_a_key_and_names_the_mode_that_works(monkeypatch):
    _no_model_key(monkeypatch)
    with TestClient(create_app()) as c:
        r = c.post("/api/chat", json={"text": "hi"}, headers={"x-chat-access": ACCESS})
    assert r.status_code == 503
    error = r.json()["error"]
    # Both keys named: someone without an Anthropic account can still turn
    # chat on, and the refusal is the only place that gets said.
    assert "ANTHROPIC_API_KEY" in error and "GEMINI_API_KEY" in error
    assert "tool mode" in error


def test_the_tool_routes_work_without_a_key(monkeypatch, schemas):
    _no_model_key(monkeypatch)
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


# --- page identity, anti-indexing, and the styled error pages --------------
#
# The console is private and its URL carries an access key, so two things have
# to hold on every response: nothing invites a crawler, and nothing leaks the
# key. Both are cheap to assert and expensive to notice by hand.


def _css_without_comments(css: str) -> str:
    """
    The declarations alone. Needed because this sheet's comments explain the
    rules they enforce -- "no gradients, no shadows" is prose about the design,
    not an instance of breaking it.
    """
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _rules_only(css: str) -> str:
    """Everything after the token blocks: the rules that consume the scales."""
    return _css_without_comments(css).split("* { box-sizing", 1)[1]


STATIC = WEBCHAT / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_CSS = (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def anon():
    """A client with no access token -- what a stranger with the URL gets."""
    with TestClient(create_app()) as c:
        yield c


def test_robots_txt_disallows_everything_and_needs_no_token(anon):
    """
    A crawler that cannot read robots.txt never learns to stay away, so this
    is the one route that must answer without the access key.
    """
    r = anon.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in r.text
    assert "Disallow: /" in r.text


def test_every_response_carries_the_noindex_header(anon):
    """robots.txt asks a crawler not to fetch; this tells one that did not to index."""
    for path in ("/", "/robots.txt", "/api/health", "/static/app.css", "/nope"):
        tag = anon.get(path).headers.get("X-Robots-Tag", "")
        assert "noindex" in tag and "nofollow" in tag, path


def test_the_page_carries_a_noindex_meta_as_well_as_the_header():
    """The header covers the fetch; the meta survives the page being saved."""
    assert '<meta name="robots" content="noindex, nofollow' in INDEX_HTML


def test_every_response_forbids_sending_a_referrer(anon):
    """
    The URL can carry ?k=<access token>. Without this, any outbound navigation
    would put the token in a third party's Referer log.
    """
    for path in ("/", "/robots.txt", "/api/health", "/nope"):
        assert anon.get(path).headers.get("Referrer-Policy") == "no-referrer", path


def test_the_page_has_a_real_title_description_and_favicon():
    assert "<title>goodreads-stats console</title>" in INDEX_HTML
    assert '<meta name="description"' in INDEX_HTML
    # Inline data: URI -- the page must make no external request for an icon.
    assert 'rel="icon" href="data:image/svg+xml,' in INDEX_HTML
    assert '<meta name="theme-color"' in INDEX_HTML


def test_the_page_requests_nothing_from_outside_this_origin():
    """
    No analytics, no CDN, no web font. Asserted over the markup, the stylesheet
    and every module, because one careless `https://` is all it takes.
    """
    sources = {"index.html": INDEX_HTML, "app.css": APP_CSS}
    for path in STATIC.glob("*.js"):
        sources[path.name] = path.read_text(encoding="utf-8")
    sources["pages.py"] = (WEBCHAT / "pages.py").read_text(encoding="utf-8")

    for name, text in sources.items():
        for line in text.splitlines():
            if "http://" not in line and "https://" not in line:
                continue
            # The SVG namespace is an identifier, not a fetch; the locked page
            # shows the URL *shape* as escaped text inside <code>.
            allowed = "www.w3.org/2000/svg" in line or "&lt;this-host&gt;" in line
            assert allowed, f"{name} reaches outside the origin: {line.strip()[:90]}"


def test_the_content_security_policy_forbids_third_party_anything(anon):
    csp = anon.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # The inline SVG favicon is the only data: source needed.
    assert "img-src 'self' data:" in csp
    # No escape hatch: an inline <style> or <script> must stay impossible.
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


# --- the two error pages ---------------------------------------------------


def test_an_unknown_page_is_a_styled_404_not_a_bare_status(anon):
    r = anon.get("/does/not/exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/app.css" in r.text          # the console's own design
    assert "does/not/exist" in r.text           # names what missed
    assert "404" in r.text


def test_an_unknown_api_path_is_json_because_that_is_what_fetch_can_read(anon):
    r = anon.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": "no such endpoint"}


def test_the_404_page_escapes_the_path_it_names(anon):
    """The path is caller-controlled text and lands in the markup."""
    r = anon.get("/%3Cscript%3Ealert(1)%3C/script%3E")
    assert r.status_code == 404
    assert "<script>alert(1)" not in r.text
    assert "&lt;script&gt;" in r.text


def test_the_locked_page_explains_the_access_key_instead_of_just_refusing(anon):
    r = anon.get("/")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/app.css" in r.text
    assert "?k=" in r.text                       # says what the parameter is
    assert "chat-access-token" in r.text         # says where the key lives
    assert "HttpOnly" in r.text                  # says what happens next


def test_no_response_ever_writes_the_access_token_into_a_link(anon):
    """
    The whole point of the gate is that the reader does not have the key. The
    placeholder is the literal string `<key>`, never a real one -- checked on
    the locked page, the 404 page, and the app shell itself.
    """
    authorised = TestClient(create_app())
    pages_seen = [
        anon.get("/").text,
        anon.get("/nope").text,
        authorised.get(f"/?k={ACCESS}").text,
    ]
    for text in pages_seen:
        assert ACCESS not in text, "a page echoed the access token"
        for href in re.findall(r'href="([^"]*)"', text):
            assert "k=" not in href, f"a link carries an access key: {href[:60]}"


def test_the_app_shell_creates_no_links_at_all():
    """
    Structural backstop for the test above: the only way a token could reach an
    href at runtime is if the client built one, and it never does.
    """
    for path in STATIC.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "createElement('a'" not in text, path.name
        assert ".href =" not in text, path.name


def test_the_client_strips_the_key_from_the_address_bar_after_the_cookie_is_set():
    """
    The cookie is the durable credential; leaving ?k= in the URL after that
    leaves it in history, in screenshots, and in anything copied from the bar.
    """
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "searchParams.delete('k')" in app_js
    assert "history.replaceState" in app_js


# --- accessibility invariants ---------------------------------------------


def test_every_chart_is_labelled_with_its_measure_its_n_and_its_unit():
    """
    A chart is role="img", so it is a single node to a screen reader and its
    label has to carry what the card shows visually. `figureDesc` is what puts
    the n, the unit and the threshold into that label; a chart call that
    forgot it would silently narrate less than the card shows.
    """
    cards = (STATIC / "cards.js").read_text(encoding="utf-8")
    charts = (STATIC / "charts.js").read_text(encoding="utf-8")

    # Every chart the cards draw passes a description.
    calls = len(re.findall(r"\b(?:hbars|vbars|lineSeries)\(\{", cards))
    described = cards.count("desc: figureDesc(")
    assert calls == described, f"{calls} chart calls but {described} descriptions"

    # And the description is built from the envelope's own grounding.
    body = cards.split("function figureDesc(", 1)[1].split("\n}", 1)[0]
    for token in ("env.n", "filters", "unit", "excluded", "min_ratings"):
        assert token in body, f"figureDesc ignores {token}"

    # charts.js sets it as a label, not merely a tooltip.
    assert "setAttribute('aria-label'" in charts
    assert "role: 'img'" in charts


def test_data_tables_carry_column_scope_and_a_caption():
    cards = (STATIC / "cards.js").read_text(encoding="utf-8")
    assert "setAttribute('scope', 'col')" in cards
    assert "node('caption', 'sr-only'" in cards


def test_the_scrollable_figure_is_reachable_from_the_keyboard():
    """A region that scrolls must be operable without a mouse (WCAG 2.1.1)."""
    cards = (STATIC / "cards.js").read_text(encoding="utf-8")
    assert "figure.tabIndex = 0" in cards
    assert "overflow-x: auto" in APP_CSS


def test_focus_is_always_visible_and_never_switched_off():
    """
    The one thing that silently ruins keyboard use. Asserted as an invariant of
    the stylesheet: a focus ring is defined, and nothing anywhere removes one.
    """
    assert ":focus-visible {" in APP_CSS
    assert "outline: 2px solid var(--focus)" in APP_CSS
    assert "outline: none" not in APP_CSS
    assert "outline: 0" not in APP_CSS


def test_the_mode_toggle_is_a_keyboard_operable_tablist():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'role="tablist"' in INDEX_HTML
    assert 'role="tab"' in INDEX_HTML
    assert 'role="tabpanel"' in INDEX_HTML
    # Roving tabindex plus arrow keys is the tablist contract.
    assert "ArrowRight" in app_js and "ArrowLeft" in app_js
    assert "tabIndex = selected ? 0 : -1" in app_js


def test_state_is_never_signalled_by_colour_alone():
    """
    The status dot has a text status beside it, the flagged row is labelled as
    well as tinted, and an unsourced numeral is underlined as well as inked.
    """
    assert 'id="status" role="status"' in INDEX_HTML
    assert 'id="status-dot" aria-hidden="true"' in INDEX_HTML
    assert "content: \" placeholder-inflated\"" in APP_CSS
    assert "border-bottom: 1px dashed var(--flag)" in APP_CSS


def test_the_page_offers_a_skip_link_and_a_live_region():
    assert 'class="skip-link" href="#thread"' in INDEX_HTML
    assert 'id="live" role="status" aria-live="polite"' in INDEX_HTML


def test_motion_is_dropped_when_the_reader_asks_for_less():
    assert "prefers-reduced-motion: reduce" in APP_CSS


# --- the design system holds together --------------------------------------


def test_both_themes_define_every_colour_token():
    """
    A token defined only in the light block renders as nothing in dark mode.
    Assert the dark override covers exactly the colours, and no colour is
    introduced for the first time inside the media query.
    """
    light = APP_CSS.split(":root {", 1)[1].split("\n}", 1)[0]
    dark = APP_CSS.split("prefers-color-scheme: dark", 1)[1].split("\n  }", 1)[0]

    colour = re.compile(r"^\s*(--[\w-]+):\s*(#|rgba?\()", re.MULTILINE)
    light_colours = {m.group(1) for m in colour.finditer(light)}
    dark_colours = {m.group(1) for m in colour.finditer(dark)}

    assert light_colours, "no colour tokens found; check the parse"
    orphan = dark_colours - light_colours
    assert not orphan, f"defined only in the dark block: {sorted(orphan)}"
    # Every ink and surface is re-stated for dark; the two accents that do not
    # change are the soft washes, which are alpha over whatever sits beneath.
    for token in ("--ink", "--ink-2", "--ink-3", "--ground", "--surface", "--edge", "--accent"):
        assert token in dark_colours, f"{token} has no dark value"


def test_the_stylesheet_uses_its_own_scales_rather_than_loose_pixels():
    """
    The design pass's central claim. Sizes come from the type scale or the
    space scale, so a stray `font-size: 14px` or `padding: 17px` is a
    regression rather than a preference.
    """
    body = _rules_only(APP_CSS)
    stray_font = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", body)
    assert not stray_font, f"font sizes bypassing the scale: {stray_font}"

    strays = []
    for prop, value in re.findall(r"\b(padding|margin|gap):\s*([^;]+);", body):
        for token in value.split():
            # 0, 1px hairlines and 2px optical nudges are allowed literals.
            if re.fullmatch(r"\d+px", token) and int(token[:-2]) > 2:
                strays.append(f"{prop}: {value.strip()}")
    assert not strays, f"spacing bypassing the scale: {sorted(set(strays))}"


def test_no_shadow_gradient_or_second_accent_creeps_in():
    """The restraint is the design. Assert it rather than trusting review."""
    declarations = _css_without_comments(APP_CSS)
    assert "box-shadow" not in declarations
    assert "gradient" not in declarations
    # Every colour in the sheet is declared once, in the token blocks.
    rules = _rules_only(APP_CSS)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules), \
        "a colour is used outside the token block"
    assert len(re.findall(r"#[0-9a-fA-F]{6}\b", declarations)) > 20


# --- the self-hosted typeface ---------------------------------------------
#
# The console sets everything in one face, served from this origin. The rules
# worth pinning are the ones whose failure is silent: a missing licence, a
# font that never reaches the wheel, or a CDN creeping back in.


FONTS = STATIC / "fonts"


def test_the_typefaces_are_self_hosted_and_subset_small():
    faces = sorted(FONTS.glob("*.woff2"))
    assert len(faces) == 4, "expected regular and semibold cuts of the sans and the mono"
    assert {f.name for f in faces} == {
        "noto-sans-regular.woff2", "noto-sans-medium.woff2",
        "jetbrains-mono-regular.woff2", "jetbrains-mono-semibold.woff2",
    }
    for face in faces:
        # Subset to Latin plus the punctuation the UI uses. A full build is
        # ~200x this; if one is dropped in unsubset, fail.
        assert face.stat().st_size < 60_000, f"{face.name} is not subset"


def test_each_font_licence_ships_with_its_font():
    """
    Both licences require their text to travel with the font. Copied verbatim
    from the system packages rather than retyped.
    """
    ofl = FONTS / "OFL.txt"
    assert ofl.exists(), "no OFL.txt beside the fonts"
    text = ofl.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text
    assert "JetBrains Mono" in text

    noto = FONTS / "NOTO-LICENSE.txt"
    assert noto.exists(), "no NOTO-LICENSE.txt beside the fonts"
    text = noto.read_text(encoding="utf-8")
    assert "Noto Sans" in text
    assert "Apache License" in text


def test_the_stylesheet_loads_the_font_from_this_origin_only():
    assert "@font-face" in APP_CSS
    for src in re.findall(r"src:\s*url\((.*?)\)", APP_CSS):
        assert src.strip('"\'').startswith("/static/fonts/"), src
    # A figure must never wait on a font; once per @font-face.
    assert _css_without_comments(APP_CSS).count("font-display: swap") == 4


def test_the_font_is_packaged_for_the_deployed_image():
    """
    `static/*` does not match a subdirectory, so this is exactly the kind of
    bug that only shows up in the deployed console as a silent fallback to a
    system monospace.
    """
    pyproject = (WEBCHAT.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'webchat = ["static/*", "static/fonts/*"]' in pyproject


def test_two_faces_prose_in_the_sans_data_in_the_mono():
    """
    The typographic commitment: prose reads in a sans, data reads in the mono,
    so what is said and what is measured are told apart by face alone. The
    body is set in the sans; the mono is applied to the data selectors by
    name; nothing asks for a face this sheet does not ship.
    """
    declarations = _css_without_comments(APP_CSS)
    assert '--sans: "Noto Sans"' in declarations
    assert '--mono: "JetBrains Mono"' in declarations
    body = re.search(r"\nbody \{[^}]*\}", declarations).group(0)
    assert "var(--sans)" in body, "the body is not set in the sans"
    # The data selectors share one mono rule, so the decision is stated once.
    mono_rule = re.search(r"\n[^{}]*\.params,[^{}]*\{\s*font-family: var\(--mono\);\s*\}", declarations)
    assert mono_rule, "no shared mono rule for the data selectors"
    for selector in (".params", ".qmeta", ".tool-name", ".grounds b", ".tile .value", "table"):
        assert selector.split()[0] in declarations, selector
    # No synthetic obliques: no italic file is shipped, so nothing may ask for one.
    assert "font-style: italic" not in declarations
    # And no third face: every font-family names one of the two stacks.
    families = set(re.findall(r"font-family:\s*([^;]+);", declarations))
    assert families <= {"var(--sans)", "var(--mono)", '"Noto Sans"', '"JetBrains Mono"'}, families


# --- the page-load moment --------------------------------------------------


def test_the_load_animation_is_scoped_to_the_shell_not_the_results():
    """
    A card arrives while its neighbour is being read. Animating a figure under
    the cursor is an interface fighting its reader, so the reveal covers the
    furniture that exists at load and nothing else.
    """
    reveal = APP_CSS.split("@keyframes settle", 1)[1].split("@media", 1)[0]
    for selector in (".card", ".figure", "table", ".caveat"):
        assert selector not in reveal, f"the load reveal reaches {selector}"
    assert ".masthead" in reveal and ".composer" in reveal
    # Staggered, and hidden through the delay rather than flashing first.
    assert reveal.count("animation-delay") >= 4
    assert "backwards" in APP_CSS


def test_the_accent_and_the_status_ink_stay_the_only_two_meanings():
    """
    Colour carries meaning exactly twice here. The two must be separable for a
    colour-blind reader; measured with the palette validator, pinned here.
    """
    light = APP_CSS.split(":root {", 1)[1].split("\n}", 1)[0]
    dark = APP_CSS.split("prefers-color-scheme: dark", 1)[1].split("\n  }", 1)[0]
    for block in (light, dark):
        assert "--accent:" in block and "--flag:" in block
    # Three accent tokens, because a mark, a text and a filled control answer
    # to three different contrast rules.
    for token in ("--accent:", "--accent-ink:", "--accent-fill:", "--on-accent:"):
        assert token in light, token


# --- descriptions reflow; the source's line wrapping is not layout ----------


def _paragraphs_of_via_node(text: str) -> list[str]:
    """Run tools.js's `paragraphsOf` under node on one input."""
    match = re.search(r"export function paragraphsOf\(text\) \{.*?\n\}\n", TOOLS_JS, re.S)
    assert match, "tools.js no longer defines paragraphsOf"
    script = (
        match.group(0).replace("export function", "function")
        + "\nprocess.stdout.write(JSON.stringify(paragraphsOf("
        + json.dumps(text)
        + ")));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_descriptions_join_wrapped_lines_and_keep_only_paragraph_breaks():
    """
    A docstring is hard-wrapped at ~80 columns. Rendered with pre-wrap, those
    breaks landed mid-sentence in the tool card ("the join is on / normalised
    title text"). The client joins lines within a paragraph and keeps a blank
    line as the one break that means something.
    """
    text = (
        "Shape, coverage and known defects.\n\n"
        "Call this before answering anything substantive. It reports live row and\n"
        "population counts for every column that has a coverage problem.\n"
        "   \n"
        "  Third paragraph,   oddly   spaced.  \n"
    )
    assert _paragraphs_of_via_node(text) == [
        "Shape, coverage and known defects.",
        "Call this before answering anything substantive. It reports live row and "
        "population counts for every column that has a coverage problem.",
        "Third paragraph, oddly spaced.",
    ]
    assert _paragraphs_of_via_node("") == []
    assert _paragraphs_of_via_node(None) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_real_description_reflows_without_a_mid_sentence_break():
    """
    Over the live tool surface: no rendered paragraph of any tool or parameter
    description may still contain a newline, and none may be lost.
    """
    from fastmcp import Client

    from goodreads_mcp.server import mcp

    async def go():
        async with Client(mcp) as c:
            return [(t.description or "", t.inputSchema) for t in await c.list_tools()]

    for description, schema in asyncio.run(go()) + [
        (guard_probe.TOOL_DESCRIPTION, guard_probe.TOOL_SCHEMA)
    ]:
        texts = [description] + [
            spec.get("description") or "" for spec in (schema.get("properties") or {}).values()
        ]
        for text in texts:
            paras = _paragraphs_of_via_node(text)
            assert all("\n" not in p for p in paras)
            # Every word survives, in order: joining lines loses nothing.
            assert " ".join(paras).split() == text.split()
            # And a blank line in the source is the only thing that makes a paragraph.
            assert len(paras) == len([p for p in re.split(r"\n[ \t]*\n", text) if p.strip()])


def test_the_description_rendering_does_not_preserve_source_whitespace():
    """
    Both description paths go through `paragraphsOf`, and the stylesheet no
    longer asks the browser to honour the docstring's line breaks.
    """
    body = _strip_comments(TOOLS_JS)
    assert "paragraphsOf(tool.description)" in body
    assert "paragraphsOf(field.description)" in body
    assert not re.search(r"node\('p', '', tool\.description", body)
    rule = re.search(r"\.tool-describe p \{[^}]*\}", APP_CSS)
    assert rule and "pre-wrap" not in rule.group(0)


# --- the Defects view ------------------------------------------------------


DEFECTS_JS = (STATIC / "defects.js").read_text(encoding="utf-8")


def _live_map() -> dict[str, list[str]]:
    """The caveat-id -> envelope-path map, parsed out of defects.js."""
    block = DEFECTS_JS.split("const LIVE = {", 1)[1].split("\n};", 1)[0]
    out: dict[str, list[str]] = {}
    for entry in re.finditer(r"\n  (\w+): \[(.*?)\]", block, re.S):
        out[entry.group(1)] = re.findall(r"'([a-z_0-9.]+)'", entry.group(2))
    return out


def test_the_defects_view_quantifies_only_real_caveats():
    """
    Every id the view places live figures beside must be a registry id, and
    the three headline defects must be among them.
    """
    live = _live_map()
    assert live, "LIVE map not found; check the parse"
    unknown = set(live) - set(caveats._REGISTRY)
    assert not unknown, f"defects.js quantifies caveats the server does not have: {sorted(unknown)}"
    for headline in ("unrated_books", "edition_duplication", "publish_day_unusable"):
        assert headline in live, headline
    # Every measured caveat -- the ones DATA_NOTES.md does not mention -- is
    # quantified, since those are the ones a reader has no other warning of.
    measured = {i for i, c in caveats._REGISTRY.items() if c.source == "measured"}
    assert measured <= set(live), f"measured caveats with no live figure: {sorted(measured - set(live))}"


def test_the_three_headline_defects_are_hero_tiles():
    """The user must not be able to miss them: each has its own tile."""
    for label in ("unrated editions", "edition overcount of n_ratings", "publish_day placeholder rows"):
        assert label in DEFECTS_JS, label
    assert "tile hero" in DEFECTS_JS


def test_dataset_overview_states_the_publish_day_share_as_a_field_with_its_n():
    """
    The guard forbids counting publish_day live, so the profiled figure is
    sent as a structured block -- count, total, share, provenance -- rather
    than left as prose only. The two statements of the number must agree.
    """
    src = __import__("inspect").getsource(server)
    block = src.split('"publish_day_placeholder": {', 1)[1].split("},", 1)[0]
    for key in ("n_rows", "n_rows_total", "pct_of_rows", "measured"):
        assert f'"{key}"' in block, key
    n_rows = int(re.search(r'"n_rows": (\d+)', block).group(1))
    pct = re.search(r'"pct_of_rows": ([\d.]+)', block).group(1)
    caveat = caveats._REGISTRY["publish_day_unusable"].text
    assert f"{n_rows:,}" in caveat, "the block and the caveat disagree on the count"
    assert f"{pct}%" in caveat, "the block and the caveat disagree on the share"
    # And the unrated share travels beside the unrated count.
    assert '"unrated": pct(b["n_books_unrated"])' in src


def test_the_defects_view_renders_from_the_overview_envelope_only():
    """
    One data source: the shared dataset_overview call. No second fetch, no
    figure computed in the client -- a share appears only if the server sent
    it.
    """
    body = _strip_comments(DEFECTS_JS)
    assert "overview()" in body
    assert "fetch(" not in body
    for arithmetic in (" / ", " * 100", "toFixed("):
        assert arithmetic not in body, f"defects.js computes a figure: {arithmetic!r}"


# --- the Telemetry view ----------------------------------------------------


TELEMETRY_JS = (STATIC / "telemetry.js").read_text(encoding="utf-8")


def test_the_telemetry_route_reuses_the_summariser_rather_than_reimplementing_it(
    tool_client, monkeypatch, tmp_path
):
    """
    The route is `goodreads-telemetry --json` behind HTTP: the same load() and
    summarise(). Write three lines the CLI would accept and assert the route
    returns exactly what summarise() returns for them, plus the scope label.
    """
    from goodreads_mcp import telemetry_cli

    log = tmp_path / "telemetry.jsonl"
    lines = [
        {"ts": "2026-09-01T00:00:00+00:00", "tool": "stats_by_author", "params": {"unit": "works"},
         "outcome": "ok", "duration_ms": 120.0, "bytes_billed": 1024, "bytes_processed": 1024,
         "cache_hit": False},
        {"ts": "2026-09-01T00:00:01+00:00", "tool": "stats_by_author", "params": {},
         "outcome": "ok", "duration_ms": 80.0, "bytes_billed": 0, "bytes_processed": 0,
         "cache_hit": True},
        {"ts": "2026-09-01T00:00:02+00:00", "tool": "top_books_by_rating",
         "params": {"min_ratings": 0}, "outcome": "other_error", "duration_ms": 5.0,
         "error_type": "ParamError"},
    ]
    log.write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setenv("GOODREADS_TELEMETRY_PATH", str(log))

    client, _ = tool_client
    body = client.get("/api/telemetry").json()
    assert body["scope"] == "local-session"
    assert body["exists"] is True
    assert body["malformed"] == 1

    rows, _ = telemetry_cli.load(log, None, None)
    expected = telemetry_cli.summarise(rows)
    for key, value in expected.items():
        assert body[key] == value, key
    assert body["calls"] == 3
    assert body["per_tool"]["stats_by_author"]["calls"] == 2
    # The share of calls per parameter value is the CLI's own pct().
    assert body["params_pct"]["unit"]["'works'"] == telemetry_cli.pct(1, 3)


def test_a_missing_telemetry_log_is_an_empty_state_not_an_error(tool_client, monkeypatch, tmp_path):
    monkeypatch.setenv("GOODREADS_TELEMETRY_PATH", str(tmp_path / "absent.jsonl"))
    client, _ = tool_client
    r = client.get("/api/telemetry")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "scope": "local-session",
        "path": str(tmp_path / "absent.jsonl"),
        "exists": False,
        "calls": 0,
    }


def test_the_telemetry_route_is_behind_the_access_token(anon):
    assert anon.get("/api/telemetry").status_code == 401


def test_the_telemetry_view_is_labelled_local_and_computes_no_rate_itself():
    """
    The scope label is on the view, and the client lays the summary out
    without re-deriving any rate or percentile from raw lines -- it never
    receives them.
    """
    body = _strip_comments(TELEMETRY_JS)
    assert "local-session" in TELEMETRY_JS
    assert "Cloud Logging" in TELEMETRY_JS
    assert "/api/telemetry" in body
    for key in ("error_rate", "p50_ms", "p95_ms", "bytes_billed", "cache_hit_rate",
                "guard_rejections_by_rule", "guard_rejections_by_column", "params_pct"):
        assert key in body, f"the view ignores {key}"
    # An empty log is a stated absence.
    assert "no local telemetry log" in TELEMETRY_JS
    # The route is a thin wrapper: the app calls the CLI's functions by name.
    app_src = (WEBCHAT / "app.py").read_text(encoding="utf-8")
    for call in ("telemetry_cli.load(", "telemetry_cli.summarise(", "telemetry_cli.pct(", "telemetry_cli.log_path("):
        assert call in app_src, call


# --- the query inspector ---------------------------------------------------


def test_every_result_card_offers_the_sql_behind_it_behind_a_disclosure():
    """
    query_meta.statements is the SQL with its bound parameters, one entry per
    job. The card shows it closed, so the figure stays the thing on screen; a
    server that predates the field produces no disclosure rather than an
    empty one.
    """
    cards = (STATIC / "cards.js").read_text(encoding="utf-8")
    body = _strip_comments(cards)
    assert "qm.statements" in body
    assert "node('details', 'query')" in body
    assert "if (!statements.length) return null;" in body
    # Both the SQL and the bound values are shown; the SQL in a pre, so its
    # own line breaks -- which are structure here, not source wrapping -- hold.
    assert "node('pre', 'sql', st.sql)" in body
    assert "st.params" in body
    # Reached from the success path only: a refusal has no statement to show.
    success = body.split("export function renderToolCard(", 1)[1].split("\n}", 1)[0]
    assert "queryDetails(" in success


def test_a_frame_from_a_real_envelope_shape_carries_its_statements(schemas):
    """End to end through the frame builder: statements survive into the card's input."""
    from goodreads_mcp import queries

    meta = queries.merge_meta(
        {"bytes_billed": 3, "cache_hit": True, "bq_ms": 1.0,
         "sql": "SELECT COUNT(*) AS n FROM books WHERE rating_dist_total >= @min_ratings",
         "params": {"min_ratings": 100}},
    )
    envelope = queries.envelope([], n={"n_books": 0}, caveats=[], meta=meta)
    outcome = ToolOutcome(tool="stats_by_author", params={}, kind="ok", envelope=envelope)
    frame = frames._result_frame("c", outcome)
    assert frame["envelope"]["query_meta"]["statements"][0]["params"] == {"min_ratings": 100}
    assert "@min_ratings" in frame["envelope"]["query_meta"]["statements"][0]["sql"]


# --- the charts and the cards, actually drawn ------------------------------
#
# Everything above reads webchat/static/*.js as text, which settles "does this
# call site pass a description" but cannot settle "does a bar with a negative
# value draw at all" -- that is geometry, and only running the code answers it.
# tests/render_probe.mjs supplies the small DOM the two modules touch, draws
# the figures and prints the coordinates; the assertions stay here.


@pytest.fixture(scope="module")
def drawn():
    """The charts and one card, drawn by their own code under node."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the machine, not the code
        pytest.skip("node is needed to run the console's own chart code")
    probe = pathlib.Path(__file__).resolve().parent / "render_probe.mjs"
    run = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, timeout=120, check=False,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _pairs(chart, series):
    """Each input value beside the bar and the label it drew."""
    return list(zip([r["v"] for r in series], chart["bars"], chart["values"], strict=True))


@pytest.mark.parametrize("chart", ["hbars_signed", "vbars_signed"])
def test_a_negative_value_draws_a_bar_the_other_side_of_a_zero_line(drawn, chart):
    """
    The bug this pins: `width = v / max * plot` is negative for a negative
    value, so the rect collapses and the row reads as missing rather than as a
    fall. compare_user_vs_book_ratings returns 24 negative divergences and one
    positive; before the fix exactly one bar was drawn.
    """
    fig = drawn[chart]
    zero = fig["zero_at"]
    assert fig["zero_lines"] == 1, "a signed series must be drawn against a zero line"

    # Which way the screen coordinate runs for a positive value: rightwards
    # along x for horizontal bars, upwards -- so downwards in y -- for vertical.
    rising = 1 if chart.startswith("hbars") else -1

    directions = set()
    for value, bar, _label in _pairs(fig, drawn["series"]["signed"]):
        if value == 0:
            continue
        assert bar["size"] > 0, f"{value} drew no bar"
        ends = (bar["pos"], bar["end"])
        assert min(ends) == pytest.approx(zero) or max(ends) == pytest.approx(zero), \
            f"{value} does not start at the zero line"
        free = bar["end"] if bar["pos"] == pytest.approx(zero) else bar["pos"]
        drew = 1 if free > zero else -1
        want = rising if value > 0 else -rising
        assert drew == want, f"{value} drew on the wrong side of zero"
        directions.add("positive" if value > 0 else "negative")
    assert directions == {"negative", "positive"}, "only one direction rendered"


@pytest.mark.parametrize("chart", ["hbars_signed", "vbars_signed"])
def test_the_signed_scale_is_symmetric_about_zero(drawn, chart):
    """
    Equal magnitudes draw equal bars whichever side of zero they fall on, so
    -1.2 is visibly four times -0.3 and twice 0.6. A per-side scale would make
    the largest fall and the largest rise the same length.
    """
    per_unit = {
        value: bar["size"] / abs(value)
        for value, bar, _ in _pairs(drawn[chart], drawn["series"]["signed"])
        if value != 0
    }
    assert len(set(round(v, 6) for v in per_unit.values())) == 1, per_unit


def test_a_signed_chart_says_so_in_its_label(drawn):
    """
    The chart is role="img": one node to a screen reader, so the label has to
    carry the zero line too, or the reading is of magnitudes with no signs.
    """
    for chart in ("hbars_signed", "vbars_signed"):
        assert "zero line" in drawn[chart]["label"]
    for chart in ("hbars_positive", "vbars_positive"):
        assert "zero line" not in drawn[chart]["label"]


@pytest.mark.parametrize("chart", ["hbars_positive", "vbars_positive"])
def test_an_all_positive_series_keeps_the_full_width_scale(drawn, chart):
    """
    The signed treatment must not cost the ordinary case half its plot: with
    nothing below zero there is nothing to divide the plot around, so the bars
    stay anchored at the edge and the largest fills the plot.
    """
    fig = drawn[chart]
    assert fig["zero_lines"] == 0
    edges = {bar["end"] if chart.startswith("v") else bar["pos"] for bar in fig["bars"]}
    assert len(edges) == 1, "positive bars should all start from the same baseline"
    sizes = [bar["size"] for bar in fig["bars"]]
    assert max(sizes) > 3 * min(sizes), "the largest bar should still fill the plot"


def test_a_value_label_sits_at_the_free_end_of_its_bar(drawn):
    """A number over the fill, or across the zero line, would be unreadable."""
    for value, bar, label in _pairs(drawn["hbars_signed"], drawn["series"]["signed"]):
        if value < 0:
            assert label["anchor"] == "end" and label["x"] <= bar["pos"]
        else:
            assert label["anchor"] == "start" and label["x"] >= bar["end"]

    fig = drawn["vbars_signed"]
    for value, bar, label in _pairs(fig, drawn["series"]["signed"]):
        if value < 0:
            assert label["y"] > bar["end"], "a downward bar's label belongs below it"
        else:
            assert label["y"] < bar["pos"], "an upward bar's label belongs above it"


def test_the_line_chart_rules_zero_when_its_values_cross_it():
    """
    lineSeries scales to its own min and max, so a negative value does render
    -- but with only compact ticks to read it against, the sign of a dip is
    left to arithmetic. Zero gets a rule when it is inside the domain.
    """
    charts = _strip_comments((STATIC / "charts.js").read_text(encoding="utf-8"))
    assert "if (yLo < 0 && yHi > 0) {" in charts
    assert "class: 'axis zero'" in charts


def test_two_caveats_on_one_field_are_separated_so_they_cannot_read_as_one(drawn):
    """
    Two markers set side by side read as one number: a field carrying caveats
    2 and 3 said "caveat 23", which is a different caveat and, past ten
    registered caveats, an existing one. The separator goes in wherever
    markers land -- headers, cells and the grounds block all decorate through
    the same function -- and the caveat list below carries the same glyph.
    """
    m = drawn["markers"]
    pooled = [h for h in m["headers"] if h.startswith("pooled_rating")]
    assert pooled == ["pooled_rating1,2"], m["headers"]
    assert any(c.endswith("1,2") for c in m["cells"]), m["cells"]
    assert any("1,2" in g for g in m["grounds"]), m["grounds"]
    # Never the run-together form, anywhere on the card.
    assert not any("12" in place for place in m["headers"] + m["cells"])
    # One separator per adjacent pair, and the list still marks each caveat once.
    assert m["separators"] == [",", ",", ","]
    assert m["caveat_marks"] == ["1", "2", "3"]


def test_the_separator_is_written_once_and_styled_where_markers_land():
    """
    A call site that wrote its own separator would drift from the others, so
    the marker run is built in one place and the style follows the class.
    """
    cards = _strip_comments((STATIC / "cards.js").read_text(encoding="utf-8"))
    assert cards.count("MARKER_SEP") == 2, "the separator is declared once and used once"
    assert "node('span', 'mk sep', MARKER_SEP)" in cards
    assert ".mk.sep" in _rules_only(APP_CSS)


# --- the category gutter ---------------------------------------------------
#
# An SVG clips at its viewBox, so a label wider than the gutter loses its
# leading characters instead of overflowing. The widths below are the ones
# Chromium actually lays out for the chart's own text style -- JetBrains Mono
# at 11.5px, measured with getComputedTextLength: every character is 7.0 user
# units, and the ellipsis 8.0. charts.js estimates with a rounded-up advance
# so its gutter is always a little generous; these are the true widths, so a
# test using them fails if the estimate ever undershoots.

CAT_INK = 7.0
ELLIPSIS_INK = 8.0


def _ink(text: str) -> float:
    """How wide `text` really draws, in viewBox units."""
    return sum(ELLIPSIS_INK if c == "…" else CAT_INK for c in text)


def _hbars(drawn):
    return {name: fig for name, fig in drawn.items() if name.startswith("hbars")}


def test_no_category_label_is_cut_off_at_the_chart_edge(drawn):
    """
    The defect: the gutter was a fixed 232 units and `City of Ashes (The
    Mortal Instruments #2)` needs 280, so it drew from x=-30 and arrived as
    `ity of Ashes (The Mortal Instrum…` -- a different book. The labels are
    anchored `end`, so their left edge is the anchor minus their width, and it
    has to stay inside the viewBox.
    """
    for name, fig in _hbars(drawn).items():
        for label in fig["cats"]:
            assert label["anchor"] == "end", name
            left = label["x"] - _ink(label["text"])
            assert left >= 0, f"{name}: {label['text']!r} starts at {left}, outside the chart"


def test_the_gutter_grows_to_fit_the_longest_label(drawn):
    """
    Sized to the labels the chart has, not fixed: a 40-character title gets a
    gutter the old fixed one could not have held, and every row shares it, so
    the labels still align down one edge.
    """
    long_fig = drawn["hbars_long"]
    anchors = {label["x"] for label in long_fig["cats"]}
    assert len(anchors) == 1, "labels should share one right edge"
    assert anchors.pop() > 232, "the gutter did not grow past the old fixed one"

    shown = [label["text"] for label in long_fig["cats"]]
    verbatim = [r["cat"] for r in drawn["series"]["long"] if r["cat"] in shown]
    # The old gutter truncated at 34 characters, and held 31 without clipping.
    assert max(len(t) for t in verbatim) > 34, \
        "a title that fits the cap must be drawn whole"


def test_the_gutter_shrinks_for_short_labels_rather_than_holding_space(drawn):
    """Three-letter language codes should not reserve a title's worth of gutter."""
    short = drawn["hbars_short"]["cats"][0]["x"]
    assert short < drawn["hbars_long"]["cats"][0]["x"]
    # But not to nothing: a floor keeps one chart looking like the next.
    assert short >= 100


def test_a_label_past_the_cap_truncates_and_keeps_its_full_text_for_hover(drawn):
    """
    Past the cap the chart would be mostly text, so the label truncates -- at
    a real ellipsis, with the whole string in a <title> so a pointer can still
    read it. The table under every chart carries it untruncated regardless.
    """
    by_text = drawn["hbars_long"]["cats"]
    cut = [label for label in by_text if label["title"] is not None]
    assert len(cut) == 1, "only the label past the cap should be truncated"
    assert cut[0]["text"].endswith("…"), "truncation must use a real ellipsis"
    assert "..." not in cut[0]["text"], "three dots is not an ellipsis"
    full = max((r["cat"] for r in drawn["series"]["long"]), key=len)
    assert cut[0]["title"] == full, "the <title> must carry the whole label"
    # And an untruncated label carries no title, so hover means "there is more".
    assert all(label["title"] is None for label in by_text if not label["text"].endswith("…"))


def test_the_gutter_cap_leaves_the_bars_room(drawn):
    """
    A gutter with no cap would let one long title squeeze the bars to nothing.
    The cap is what stops that, so it is worth asserting the bars still get
    the larger share of the width.
    """
    fig = drawn["hbars_long"]
    gutter = fig["cats"][0]["x"]
    longest = max(bar["size"] for bar in fig["bars"])
    assert gutter <= 340
    assert longest > gutter, "the bars should still outweigh the labels"


def test_the_size_the_charts_measure_with_is_the_size_the_stylesheet_sets():
    """
    charts.js sizes the gutter arithmetically, so its idea of the label font
    has to be the stylesheet's. A change to --t-meta with no change here would
    silently start clipping again.
    """
    charts = (STATIC / "charts.js").read_text(encoding="utf-8")
    declared = float(re.search(r"const CAT_SIZE = ([\d.]+);", charts).group(1))
    token = float(re.search(r"--t-meta:\s*([\d.]+)px", APP_CSS).group(1))
    assert declared == token, f"charts.js measures at {declared}px, --t-meta is {token}px"

    # And the advance it assumes must be no smaller than the real one, or the
    # estimate undershoots and the gutter comes up short.
    advance = float(re.search(r"const CAT_ADVANCE = ([\d.]+);", charts).group(1))
    assert advance >= CAT_INK / token, "the assumed advance is narrower than the face's"

    # The label rule really is the mono, which is what makes one advance per
    # character true at all.
    rule = re.search(r"\.chart text \{([^}]*)\}", APP_CSS).group(1)
    assert "var(--t-meta)" in rule and "var(--mono)" in rule


def _drawn_charts(drawn):
    return {name: fig for name, fig in drawn.items() if name.startswith(("hbars", "vbars"))}


def test_every_truncated_category_label_carries_its_full_text(drawn):
    """
    Both orientations, one rule: a label the chart shortened says so with a
    real ellipsis and carries the whole string in a <title>, so a pointer can
    read what was cut. A label drawn whole carries none, which is what makes
    the tooltip mean "there is more here" rather than decoration.

    hbars can widen its gutter to fit; vbars cannot -- its labels get a slot,
    plotW/n, and widening one would narrow its neighbour -- so on that side the
    tooltip is the whole of the fix.
    """
    shortened = set()
    for name, fig in _drawn_charts(drawn).items():
        for label, full in zip(fig["cats"], fig["cat_inputs"], strict=True):
            if label["text"] == full:
                assert label["title"] is None, \
                    f"{name}: {full!r} is drawn whole but carries a tooltip"
                continue
            shortened.add(name)
            assert label["text"].endswith("…"), f"{name}: {label['text']!r}"
            assert "..." not in label["text"], "three dots is not an ellipsis"
            assert label["title"] == full, f"{name}: tooltip does not carry {full!r}"
            assert full.startswith(label["text"][:-1]), \
                f"{name}: {label['text']!r} is not the head of {full!r}"

    orientations = {name.split("_")[0] for name in shortened}
    assert orientations == {"hbars", "vbars"}, \
        f"only {orientations} exercised truncation; both must be"


def test_a_rotated_label_keeps_both_its_transform_and_its_tooltip(drawn):
    """
    The rotated variant sets a transform on the same element the <title> hangs
    off, which is the one place the two could have collided.
    """
    rotated = drawn["vbars_rotated"]["cats"]
    assert all(label["transform"].startswith("rotate(-38 ") for label in rotated)
    assert any(label["title"] for label in rotated)
    # Upright labels are not transformed at all.
    assert all(label["transform"] is None for label in drawn["vbars_positive"]["cats"])


# --- two providers, one loop ----------------------------------------------
#
# The console can be driven by Anthropic or by Gemini -- the second because
# Google AI Studio's free tier makes chat mode work with no paid credits. What
# has to stay true is that only the model call differs: the frames, the cards,
# the caveat attachment and the numeral checker are downstream of `Reply` and
# cannot tell the two apart.


def _provider_env(monkeypatch, *, anthropic=None, gemini=None, chat_provider=None):
    """Set exactly the keys a case is about, clearing the others."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", anthropic)
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini)
    monkeypatch.setattr(config, "CHAT_PROVIDER", chat_provider)


def test_a_key_selects_its_provider_and_no_key_leaves_chat_off(monkeypatch):
    from webchat import provider

    _provider_env(monkeypatch, anthropic="a-key")
    assert provider.chosen() == "anthropic" and config.chat_enabled()

    _provider_env(monkeypatch, gemini="g-key")
    assert provider.chosen() == "gemini" and config.chat_enabled()

    _provider_env(monkeypatch)
    assert provider.chosen() is None
    assert not config.chat_enabled(), "no key must leave chat mode off"


def test_two_keys_are_decided_by_chat_provider_defaulting_to_anthropic(monkeypatch):
    """
    A default that fell out of dict order would change the model behind
    someone's console without anything being edited.
    """
    from webchat import provider

    _provider_env(monkeypatch, anthropic="a-key", gemini="g-key")
    assert provider.chosen() == "anthropic"

    _provider_env(monkeypatch, anthropic="a-key", gemini="g-key", chat_provider="gemini")
    assert provider.chosen() == "gemini"

    # Naming a provider with no key is a configuration error, not a fallback.
    _provider_env(monkeypatch, anthropic="a-key", chat_provider="gemini")
    with pytest.raises(provider.ProviderError):
        provider.chosen()


@pytest.mark.parametrize(
    "name,key,other",
    [("anthropic", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"),
     ("gemini", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")],
)
def test_each_provider_constructs_without_the_others_key(monkeypatch, schemas, name, key, other):
    """Neither SDK may need the other's credential to be built."""
    from webchat import provider

    monkeypatch.setattr(config, key, "test-key-not-real")
    monkeypatch.setattr(config, other, None)
    monkeypatch.setattr(config, "CHAT_PROVIDER", None)

    built = provider.select(_StubBridge(schemas))
    assert built.name == name
    assert built.model, "a provider must name the model it will use"
    assert isinstance(built, provider.Provider), "must satisfy the interface"


def test_the_agent_holds_one_loop_and_names_no_provider():
    """
    The loop is the thing that must not be duplicated: two loops could drift
    into two renderings, and the console's claim is that a card comes from the
    tool's own envelope regardless of what fetched it.
    """
    import inspect

    from webchat import agent

    source = _strip_comments(inspect.getsource(agent))
    body = source.split("async def run_turn", 1)[1]
    for name in ("anthropic", "Anthropic", "gemini", "Gemini", "genai"):
        assert name not in body, f"the loop names {name}; it must not"
    # And it reaches the model only through the interface.
    for call in ("self.provider.stream(", "self.provider.record_reply(",
                 "self.provider.record_results(", "self.provider.record_refusal("):
        assert call in body, call


def test_the_frame_path_is_the_same_object_for_both_providers():
    """
    Everything downstream of a tool call is shared by construction. Asserted on
    the import graph rather than by eye: a provider that imported `frames` or
    `attach` could start building its own cards.
    """
    for name in ("provider_anthropic", "provider_gemini"):
        tree = ast.parse((WEBCHAT / f"{name}.py").read_text(encoding="utf-8"))
        reached = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                reached.add((node.module or "").split(".")[-1])
            elif isinstance(node, ast.Import):
                reached.update(a.name.split(".")[-1] for a in node.names)
        for forbidden in ("frames", "attach", "numcheck"):
            assert forbidden not in reached, \
                f"{name} imports {forbidden}; rendering must stay in the loop"


def test_neither_sdk_is_imported_until_a_provider_is_chosen():
    """
    Tool mode has no key and must load no model SDK; a Gemini deployment must
    not load the Anthropic package either. `select()` imports inside its branch,
    so the module graph has to stay clean above it.
    """
    for module in ("webchat.provider", "webchat.config", "webchat.frames",
                   "webchat.mcp_client", "webchat.session"):
        tree = ast.parse(
            (WEBCHAT / f"{module.split('.')[-1]}.py").read_text(encoding="utf-8")
        )
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        # `google.auth` is fine and unrelated -- mcp_client mints the Cloud Run
        # OIDC token with it. It is the model SDKs that must stay out.
        for sdk in ("anthropic", "google.genai"):
            assert not any(name == sdk or name.startswith(sdk + ".")
                           for name in imported), \
                f"{module} imports {sdk} at module scope"


# --- the schema translation ------------------------------------------------


def test_the_json_schema_dialect_drops_nothing_from_the_real_tool_surface(schemas):
    """
    The claim behind `parameters_json_schema`: our schemas already are standard
    JSON Schema, so the translation is close to identity. Exercised against the
    real `tools/list` output, not a fixture, so a new parameter with a new
    keyword is caught here.
    """
    from webchat import gemini_schema

    tools = [
        {"name": name, "description": "", "input_schema": schema}
        for name, schema in sorted(schemas.items())
    ]
    decls, losses = gemini_schema.declarations(tools, dialect="json_schema")

    assert losses == [], "the json_schema path must lose nothing"
    assert len(decls) == len(tools)
    for decl, tool in zip(decls, tools, strict=True):
        assert decl["parameters_json_schema"] == tool["input_schema"], \
            "the schema must reach Gemini exactly as the server published it"
        assert "parameters" not in decl, "the two fields are mutually exclusive"


def test_the_openapi_fallback_reports_every_keyword_it_cannot_carry(schemas):
    """
    The fallback dialect is genuinely smaller, and the one keyword our surface
    loses is named rather than discovered in production. `exclusiveMinimum` on
    `bucket_size` is the whole of it today; a second one appearing is a change
    worth failing on.
    """
    from webchat import gemini_schema

    tools = [
        {"name": name, "description": "", "input_schema": schema}
        for name, schema in sorted(schemas.items())
    ]
    _, losses = gemini_schema.declarations(tools, dialect="openapi")

    assert losses == [("rating_distribution", "/bucket_size", "exclusiveMinimum")], losses
    # And the report says what it means, because a dropped constraint is still
    # enforced by the server -- it just becomes a refusal card instead.
    described = gemini_schema.describe_losses(losses)
    assert "exclusiveMinimum" in described and "refusal card" in described


def test_the_openapi_fallback_keeps_everything_else_it_can(schemas):
    """Losing one keyword must not mean losing the constraints beside it."""
    from webchat import gemini_schema

    bucket = gemini_schema.to_openapi_subset(
        schemas["rating_distribution"]
    )["properties"]["bucket_size"]
    assert bucket["maximum"] == 1.0 and bucket["type"] == "number"
    assert "exclusiveMinimum" not in bucket
    assert bucket["description"], "the description is what is left to say 0.05 to 1.0"


def test_an_optional_parameter_becomes_nullable_rather_than_an_anyOf_with_null(schemas):
    """
    Every optional parameter here is pydantic's `X | None`, which serialises as
    `anyOf: [{type: X}, {type: null}]`. Gemini's function-calling path honours
    `nullable`, so the fallback rewrites rather than passes through.
    """
    from webchat import gemini_schema

    props = gemini_schema.to_openapi_subset(schemas["page_count_stats"])["properties"]
    language = props["language"]
    assert language["type"] == "string" and language["nullable"] is True
    assert "anyOf" not in language
    assert props["year_from"]["type"] == "integer" and props["year_from"]["nullable"]


def test_the_dialect_keyword_set_matches_the_installed_sdk():
    """
    `OPENAPI_SUBSET` is read off `types.Schema`, not off prose documentation. An
    SDK bump that widens or narrows the dialect should fail here rather than in
    a deployment.
    """
    from google.genai import types

    from webchat import gemini_schema

    def camel(name: str) -> str:
        head, *rest = name.split("_")
        return head + "".join(w.title() for w in rest)

    sdk = set()
    for field in types.Schema.model_fields:
        sdk.add({"defs": "$defs", "ref": "$ref"}.get(field, camel(field)))
    assert sdk == set(gemini_schema.OPENAPI_SUBSET), (
        f"only in sdk: {sorted(sdk - set(gemini_schema.OPENAPI_SUBSET))}, "
        f"only in ours: {sorted(set(gemini_schema.OPENAPI_SUBSET) - sdk)}"
    )
    # The one that matters, stated so the reason survives a refactor.
    assert "exclusiveMinimum" not in sdk


# --- a transcript belongs to the provider that wrote it --------------------


def test_a_session_changing_provider_drops_its_transcript_and_says_so():
    """
    A Gemini `Content` has no `tool_use` block and an Anthropic message has no
    `functionResponse` part, so handing one to the other fails inside the SDK
    with a message about a field name. Dropped deliberately instead.
    """
    session = Session(id="s")
    session.provider = "anthropic"
    session.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": []}]
    session.sourced_numbers = {"1850115"}
    session.turns = 4

    dropped = session.adopt("gemini")

    assert dropped == 2 and session.messages == []
    assert session.provider == "gemini"
    assert session.sourced_numbers == set(), \
        "numerals from a discarded transcript are no longer sourced"
    assert session.turns == 4, \
        "the spend ceiling must survive, or switching provider would refill it"


def test_adopting_the_same_provider_is_a_no_op():
    session = Session(id="s")
    session.adopt("gemini")
    session.messages = [{"role": "user", "content": "hi"}]
    assert session.adopt("gemini") == 0
    assert len(session.messages) == 1


def test_the_switch_is_announced_to_the_person_not_only_logged():
    """The loop emits a `notice` frame, and the client renders it."""
    import inspect

    from webchat import agent

    body = _strip_comments(inspect.getsource(agent)).split("async def run_turn", 1)[1]
    assert 'session.adopt(' in body
    assert '"type": "notice"' in body
    assert '"kind": "provider_switch"' in body

    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "case 'notice':" in app_js
    assert "notice(text)" in app_js


# --- what the masthead says ------------------------------------------------


def test_health_names_the_provider_and_the_model_not_merely_that_chat_is_on(monkeypatch):
    _provider_env(monkeypatch, gemini="g-key")
    settings = config.public_settings()
    assert settings["chat_enabled"] is True
    assert settings["provider"] == "gemini"
    assert settings["model"] == config.GEMINI_MODEL

    _provider_env(monkeypatch, anthropic="a-key")
    settings = config.public_settings()
    assert settings["provider"] == "anthropic"
    assert settings["model"] == config.MODEL

    _provider_env(monkeypatch)
    settings = config.public_settings()
    assert settings["chat_enabled"] is False
    assert settings["provider"] is None and settings["model"] is None


def test_the_masthead_renders_both_the_provider_and_the_model():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "health.provider" in app_js and "health.model" in app_js


# --- a provider that streams no reasoning ----------------------------------


def test_the_client_opens_the_reasoning_disclosure_only_when_thinking_arrives():
    """
    Gemini may or may not return thoughts. The absence must be nothing on
    screen, not an empty disclosure or a wait -- so the element is created on
    the first delta rather than up front.
    """
    app_js = _strip_comments((STATIC / "app.js").read_text(encoding="utf-8"))
    thinking = app_js.split("thinking(text) {", 1)[1].split("\n  }", 1)[0]
    assert "if (!this.reasoning)" in thinking, \
        "the disclosure must be built lazily, on the first thinking delta"
    constructor = app_js.split("constructor(text) {", 1)[1].split("\n  }", 1)[0]
    assert "this.reasoning = null" in constructor
    assert "details" not in constructor, "no reasoning element before a delta arrives"


# --- the frames do not depend on which model fetched them ------------------


class _StubProvider:
    """
    A provider with the model removed. Its transcript entries are deliberately
    a different shape per instance, which is the point: two providers store
    incompatible native content and the frames must come out the same anyway.
    """

    def __init__(self, name, tool_calls, shape="blocks"):
        self.name = name
        self.model = f"{name}-test-model"
        self.shape = shape
        self._script = list(tool_calls)
        self.recorded = []

    async def declare(self, bridge=None):
        return "CONTRACT with the figure 1,850,115 in it"

    def user_turn(self, transcript, text):
        transcript.append({self.shape: text})

    async def stream(self, transcript):
        from webchat.provider import Delta, Reply

        yield Delta("thinking", f"[{self.name} thinking]")
        calls = self._script.pop(0) if self._script else []
        if calls:
            yield Reply(tool_calls=calls, stop="tools",
                        usage={"input_tokens": 3, "output_tokens": 4}, raw={"c": calls})
            return
        yield Delta("text", "The top row leads by a wide margin.")
        yield Reply(stop="end", usage={"input_tokens": 1, "output_tokens": 2}, raw={"t": "done"})

    def record_reply(self, transcript, reply):
        transcript.append({self.shape: reply.raw})

    def record_results(self, transcript, pairs):
        self.recorded.append([(c.name, o.kind) for c, o in pairs])
        transcript.append({self.shape: [o.for_model() for _, o in pairs]})

    def record_refusal(self, transcript, call, message):
        transcript.append({self.shape: message})


def _run(agent, session, text):
    async def go():
        return [f async for f in agent.run_turn(session, text)]

    return asyncio.run(go())


def _one_turn_frames(name, shape, schemas):
    from webchat.agent import Agent
    from webchat.provider import ToolCall

    outcome = ToolOutcome(
        tool="stats_by_author",
        params={"unit": "works"},
        kind="ok",
        # The registry's own rendered text, because `attach.structure()` maps
        # back from the text -- an id string here would resolve to no caveat.
        envelope={"data": [], "n": {"n_books": 3865},
                  "caveats": caveats.collect("edition_duplication"),
                  "query_meta": {"queries": 1}},
        mcp_ms=12.0,
    )
    bridge = _StubBridge(schemas, outcome=outcome)
    provider = _StubProvider(
        name, [[ToolCall(id="c1", name="stats_by_author", params={"unit": "works"})], []],
        shape=shape,
    )
    return _run(Agent(bridge, provider=provider), Session(id=f"s-{name}"), "who is most read?")


def test_both_providers_produce_the_same_frames_for_the_same_tool_call(schemas):
    """
    The console's claim is that a card is rendered from the tool's own
    envelope, whatever fetched it. So two providers whose native transcripts
    are incompatible must still emit an identical frame stream -- identical
    types, in identical order, with identical card payloads.
    """
    left = _one_turn_frames("anthropic", "blocks", schemas)
    right = _one_turn_frames("gemini", "parts", schemas)

    assert [f["type"] for f in left] == [f["type"] for f in right]

    def cards(frames):
        return [f for f in frames if f["type"] in ("tool_call", "tool_result", "tool_refusal")]

    assert cards(left) == cards(right), "the rendered frames differ between providers"

    # And the only frames that may name a provider are the ones that are about
    # the provider: turn_end reports which model answered.
    for frame in cards(left):
        assert "anthropic" not in json.dumps(frame)

    ends = [f for f in left if f["type"] == "turn_end"]
    assert ends[0]["provider"] == "anthropic" and ends[0]["model"] == "anthropic-test-model"


def test_the_tool_result_frame_carries_structured_caveats_whichever_provider_ran(schemas):
    """The caveat registry is reached from the loop, so it cannot be bypassed."""
    for name, shape in (("anthropic", "blocks"), ("gemini", "parts")):
        results = [f for f in _one_turn_frames(name, shape, schemas)
                   if f["type"] == "tool_result"]
        assert len(results) == 1
        caveats = results[0]["envelope"]["caveats"]
        assert caveats and isinstance(caveats[0], dict), name
        assert caveats[0]["id"] == "edition_duplication"
        assert caveats[0]["fields"], "a caveat must still name the fields it qualifies"


def test_a_provider_switch_announces_itself_in_the_turn_it_happens(schemas):
    """
    End to end through the loop: a session another provider wrote is emptied
    and the person is told, before any of the answer arrives.
    """
    from webchat.agent import Agent
    from webchat.provider import ToolCall

    session = Session(id="s")
    session.provider = "anthropic"
    session.messages = [{"blocks": "an earlier question"}, {"blocks": "an earlier answer"}]

    provider = _StubProvider("gemini", [[]], shape="parts")
    frames = _run(Agent(_StubBridge(schemas), provider=provider), session, "and now?")

    notices = [f for f in frames if f["type"] == "notice"]
    assert len(notices) == 1, "the switch must be stated exactly once"
    assert notices[0]["kind"] == "provider_switch"
    assert "gemini" in notices[0]["message"]
    assert "2 earlier messages" in notices[0]["message"]
    # Before the answer, so it is read as a precondition and not an afterthought.
    assert frames.index(notices[0]) < next(
        i for i, f in enumerate(frames) if f["type"] in ("text_delta", "thinking_delta")
    )
    # And the transcript that replaced it is this provider's own.
    assert all("parts" in entry for entry in session.messages), session.messages


def test_no_switch_notice_when_the_provider_is_unchanged(schemas):
    from webchat.agent import Agent

    session = Session(id="s")
    session.provider = "gemini"
    frames = _run(
        Agent(_StubBridge(schemas), provider=_StubProvider("gemini", [[]], shape="parts")),
        session, "hello",
    )
    assert not [f for f in frames if f["type"] == "notice"]


def test_the_budget_refusal_goes_back_to_the_model_not_only_to_the_screen(schemas, monkeypatch):
    """
    A refusal the console invents still has to reach the transcript, or the
    model waits for a result that never comes. Both providers get told through
    the same call.
    """
    from webchat.agent import Agent
    from webchat.provider import ToolCall

    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_TURN", 1)
    outcome = ToolOutcome(tool="stats_by_author", params={}, kind="ok",
                          envelope={"data": [], "n": {}, "caveats": []}, mcp_ms=1.0)
    provider = _StubProvider("gemini", [[
        ToolCall(id="c1", name="stats_by_author", params={}),
        ToolCall(id="c2", name="stats_by_publisher", params={}),
    ], []], shape="parts")
    session = Session(id="s")
    frames = _run(
        Agent(_StubBridge(schemas, outcome=outcome), provider=provider),
        session, "two things please",
    )

    refusals = [f for f in frames if f["type"] == "tool_refusal"]
    assert len(refusals) == 1 and refusals[0]["kind"] == "budget"
    assert refusals[0]["tool"] == "stats_by_publisher"
    # And it reached the transcript, not only the screen: a model left waiting
    # on a result that never arrives is the failure this guards against.
    assert any(
        isinstance(entry.get("parts"), str) and "budget" in entry["parts"]
        for entry in session.messages
    ), session.messages


def test_a_failed_call_is_named_by_its_provider_and_never_retried(monkeypatch, schemas):
    """
    On the free tier a spent quota is an ordinary outcome, and "ClientError"
    would send someone to read their own code instead of their quota page. It
    is reported, not retried: a retry loop would hide exactly the
    unreliability this console exists to make visible.
    """
    from webchat import provider

    class _ApiError(Exception):
        def __init__(self, code, message=""):
            super().__init__(message)
            self.code = code
            self.status_code = code
            self.message = message

    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "CHAT_PROVIDER", None)
    gem = provider.select(_StubBridge(schemas))

    spent = gem.explain_error(_ApiError(429, "Quota exceeded. Please retry in 12.3s"))
    assert "free tier" in spent and "quota" in spent
    assert "12s" in spent, "the retry delay the API supplied should reach the person"
    assert gem.model in spent

    assert "overloaded" in gem.explain_error(_ApiError(503, "high demand"))
    assert "GEMINI_MODEL" in gem.explain_error(_ApiError(404, "not available"))
    # Anything unrecognised still says something rather than nothing.
    assert gem.explain_error(ValueError("odd")) == "ValueError"

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    ant = provider.select(_StubBridge(schemas))
    assert "rate limited" in ant.explain_error(_ApiError(429))
    assert "temporary" in ant.explain_error(_ApiError(503))

    # No retry anywhere in the loop.
    import inspect

    from webchat import agent

    body = _strip_comments(inspect.getsource(agent)).split("async def run_turn", 1)[1]
    assert "explain_error" in body
    for word in ("retry", "sleep", "backoff", "attempt"):
        assert word not in body, f"the loop {word}s around a failed call; it must not"
