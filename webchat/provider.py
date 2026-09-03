"""
The model behind chat mode, behind one interface.

Two providers reach this console: Anthropic, and Gemini for the sake of Google
AI Studio's free tier, which makes chat mode work without paid credits. What
they have in common is almost everything. A turn is: declare the tools, stream
an assistant reply, run whatever tools it asked for, put the results back, go
again. Only four things are actually provider-shaped -- how tools are
declared, what a stream event looks like, how an assistant reply is stored,
and how a tool result is handed back -- so those four are the whole of this
interface and `agent.py` holds the loop once.

That matters beyond tidiness. The claim this console makes is that a figure is
rendered from the tool's own envelope; if the two providers had two loops, they
could drift into two renderings. Everything downstream of `Reply` is shared by
construction: the same `frames._result_frame`, the same caveat attachment, the
same numeral checker, the same cards. A provider cannot reach any of it.

NOTHING HERE IMPORTS A MODEL SDK. `select()` imports one inside the branch it
picks, so a console with no key -- or in tool mode, which has no model at all
-- pulls in neither. Two tests assert exactly that, one per SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, NamedTuple, Protocol, runtime_checkable

from . import config
from .mcp_client import MCPBridge, ToolOutcome


class ProviderError(RuntimeError):
    """No usable provider, or one that cannot be built."""


@dataclass(frozen=True)
class ToolCall:
    """
    One tool the model asked for, normalised.

    `id` is what ties a `tool_call` frame to the `tool_result` frame that
    replaces it in the DOM. Anthropic supplies one; Gemini's `functionCall` may
    omit it, so the provider synthesises a stable one rather than letting the
    client match on tool name -- two calls to the same tool in one turn would
    collide.
    """

    id: str
    name: str
    params: dict[str, Any]


class Delta(NamedTuple):
    """A streamed fragment. `kind` is "text" or "thinking"."""

    kind: str
    text: str


@dataclass
class Reply:
    """
    One assistant turn, normalised.

    `raw` is provider-native and opaque: it is what `record_reply()` puts back
    into the transcript, and nothing outside the provider reads it.
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"                       # "end" | "tools" | "refusal"
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


@runtime_checkable
class Provider(Protocol):
    """
    What `agent.py` needs from a model. Five methods and two attributes.

    `name` and `model` are shown in the masthead, so the person reading knows
    which model wrote the prose beside the card -- "chat on" would not be
    enough once there is more than one answer to that.
    """

    name: str
    model: str

    async def declare(self, bridge: MCPBridge) -> str:
        """
        Translate `tools/list` into this provider's dialect, once.

        Returns the static prompt text -- the contract plus the server's own
        instructions -- which seeds the numeral checker: anything the server
        told the model counts as a sourced figure.
        """
        ...

    def user_turn(self, transcript: list, text: str) -> None:
        """Append the person's own words."""
        ...

    def stream(self, transcript: list) -> AsyncIterator[Delta | Reply]:
        """
        Deltas as they arrive, then exactly one `Reply`, last.

        Implementations must not raise inside the iterator for an API failure;
        `agent.py` catches around the whole loop, but a provider that can name
        the failure better should let the exception carry that name.
        """
        ...

    def record_reply(self, transcript: list, reply: Reply) -> None:
        """Put the assistant turn back, so the next request has it."""
        ...

    def record_results(
        self, transcript: list, pairs: list[tuple[ToolCall, ToolOutcome]]
    ) -> None:
        """
        Put the tool outcomes back, in the order they were called.

        Both providers hand the model the same content -- the full envelope,
        caveats included -- but not the same encoding: Anthropic takes a JSON
        string in a `tool_result` block, Gemini an object in a
        `functionResponse` part. That difference stops here.
        """
        ...

    def explain_error(self, exc: Exception) -> str:
        """
        One line a person can act on, for a call that failed.

        `type(exc).__name__` is what the loop would say on its own, and
        "ClientError" tells nobody anything -- least of all that they hit a
        free-tier quota and can retry in twelve seconds. The provider knows its
        own error shapes, so it does the naming. This is a message, never a
        retry: a loop that quietly tried again would hide exactly the
        unreliability worth seeing.
        """
        ...

    def record_refusal(
        self, transcript: list, call: ToolCall, message: str
    ) -> None:
        """
        Tell the model a call was refused without running it.

        Only one thing refuses before the server does: this console's per-turn
        tool-call budget. It has to go back in the transcript, or the model
        waits for a result that is never coming.
        """
        ...


# --- choosing one ----------------------------------------------------------


def available() -> list[str]:
    """Which providers have a key, in preference order."""
    return [n for n, key in (
        ("anthropic", config.ANTHROPIC_API_KEY),
        ("gemini", config.GEMINI_API_KEY),
    ) if key]


def chosen() -> str | None:
    """
    Which provider this process will use, or None for tool mode only.

    A key is the enabling signal, so the common cases need no configuration:
    one key means that provider, no key means chat mode is not offered.

    `CHAT_PROVIDER` is read whenever it is set, not only when both keys are --
    naming a provider you have no key for is a mistake worth a startup error,
    and letting it fall through to the other one would start a console under a
    model nobody asked for. With both keys and nothing set the answer is
    Anthropic, fixed in `available()`'s order rather than left to whichever key
    happened to be read first.
    """
    have = available()
    if not have:
        # No key at all is tool mode, whatever CHAT_PROVIDER says: there is
        # nothing to be wrong about yet.
        return None
    want = (config.CHAT_PROVIDER or "").strip().lower()
    if not want:
        return have[0]
    if want not in have:
        raise ProviderError(
            f"CHAT_PROVIDER={want!r} has no key. Keys present: {', '.join(have)}."
        )
    return want


def select(bridge: MCPBridge) -> Provider:
    """
    Build the chosen provider. The SDK import happens inside the branch.

    Raises rather than returning None: the caller has already checked
    `config.chat_enabled()`, so arriving here with no key is a bug, not a mode.
    """
    name = chosen()
    if name == "anthropic":
        from .provider_anthropic import AnthropicProvider

        return AnthropicProvider(bridge)
    if name == "gemini":
        from .provider_gemini import GeminiProvider

        return GeminiProvider(bridge)
    raise ProviderError(
        "no model provider is configured; set ANTHROPIC_API_KEY or GEMINI_API_KEY"
    )
