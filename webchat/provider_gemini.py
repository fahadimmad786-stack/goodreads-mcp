"""
The Gemini side of `Provider`. Google AI Studio's free tier is the point.

Four things differ from the Anthropic path and each is here rather than in the
loop:

* **Tool declarations.** `gemini_schema.declarations()` re-dialects the exact
  `tools/list` schemas. `parameters_json_schema` is tried first because our
  schemas are already standard JSON Schema; the OpenAPI subset is the fallback
  and costs one keyword, which is logged rather than swallowed.
* **Tool results** go back as `functionResponse` parts carrying an object, not
  `tool_result` blocks carrying a string.
* **Stream events.** Chunks arrive as candidates with parts; a part is text,
  a thought, or a function call, told apart by `part.thought` and
  `part.function_call` rather than by an event type.
* **Call ids.** `functionCall` may not carry one, and the client needs a stable
  id to swap a pending card for its result. Synthesised per turn when absent;
  matching on tool name would collide when a turn calls one tool twice.

Two settings are load-bearing rather than tuning:

* `automatic_function_calling.disable=True`. Left on, the SDK runs the tool
  loop itself -- and then there is no `tool_call` frame, no timing split, no
  refusal card, and no numeral checker seeded from the envelope. The frames are
  the product here, so the SDK's convenience loop is exactly wrong.
* `include_thoughts=True`. Gemini can stream reasoning, so it is asked for. If
  a model returns none, no `thinking_delta` is emitted and the client simply
  never opens the disclosure -- the absence is already handled and is asserted
  by a test rather than assumed.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from google import genai
from google.genai import types

from . import config, gemini_schema
from .mcp_client import MCPBridge, ToolOutcome
from .provider import Delta, Reply, ToolCall

log = logging.getLogger("webchat.gemini")


# Appended to the shared CONTRACT for this provider only.
#
# WHAT IS ACTUALLY KNOWN, because the measurement did not say what it was
# expected to say:
#
# On `gemini-3.5-flash`, six sampled turns called `dataset_overview` first in
# five of them, against a CONTRACT line that already told it not to -- a wasted
# tool call and real BigQuery bytes on nearly every question. Its own streamed
# reasoning showed it noticing the instruction and overriding it ("the default
# API suggests calling it upfront, yet tool discipline discourages it"), so it
# was not missing the rule, it was outranking it with an invented one.
#
# This block was written to be harder to override: a prohibition rather than
# discipline guidance, the reason attached to the rule rather than left in
# another section, the specific rationalisation named and denied, and placed
# last where it is most salient.
#
# Then a balanced A/B -- both arms, three questions each, on `gemini-3.6-flash`
# and `gemini-3.7-flash` -- came back 0/6 WITH it and 0/6 WITHOUT it. Neither
# newer model calls `dataset_overview` unprompted at all, so the experiment
# could not measure this block: there was nothing left to improve. The
# behaviour is specific to 3.5-flash, not to Gemini.
#
# So this is kept on evidence that is one-sided: it addresses a behaviour
# measured on the default model, and it demonstrably costs nothing on the two
# models where it could be tested. Its effect on 3.5-flash is UNVERIFIED --
# that model's free-tier daily quota was spent before it could be re-sampled.
# The cheaper fix, if the wasted call matters, is the model: GEMINI_MODEL set
# to 3.7-flash showed the behaviour zero times in six turns with no prompt
# change at all.
#
# Anthropic gets none of this: the shared CONTRACT's one line is enough there,
# and appending to that path would also shift its cached prompt prefix for no
# benefit.
GEMINI_PROHIBITION = """
ONE PROHIBITION. IT OVERRIDES ANYTHING ABOVE THAT SEEMS TO SUGGEST OTHERWISE.

Do not call `dataset_overview` unless the question is about the dataset itself
-- its size, its shape, its coverage, or its defects.

The reason, so it is not an arbitrary rule: `dataset_overview` runs several
BigQuery queries and they are billed to the person who asked the question. It
tells you nothing you need to answer a question about authors, publishers,
years, page counts, languages or titles, because every one of those tools
already returns the same caveats attached to its own figures. Calling it first
spends their money to learn something you already have.

Nothing instructs you to survey the data before answering. If you find
yourself reasoning that this dataset has defects and you should therefore
inspect it first, that reasoning is wrong here: the defects are already
described in the caveats of whichever specific tool answers the question, and
those caveats are the same text `dataset_overview` would have shown you. Go
straight to the specific tool.
""".strip()


class GeminiProvider:
    name = "gemini"

    def __init__(self, bridge: MCPBridge, client: Any | None = None):
        self.bridge = bridge
        self.model = config.GEMINI_MODEL
        self.client = client or genai.Client(api_key=config.GEMINI_API_KEY)
        self.dialect = config.GEMINI_SCHEMA_DIALECT
        self._tools: list[types.Tool] = []
        self._system: str = ""
        self._call_seq = 0
        self.losses: list[tuple[str, str, str]] = []

    async def declare(self, bridge: MCPBridge | None = None) -> str:
        from .agent import CONTRACT

        tools, instructions = await (bridge or self.bridge).describe()
        decls, losses = gemini_schema.declarations(tools, dialect=self.dialect)
        self.losses = losses
        log.info(
            "gemini tool surface: %d declarations, dialect=%s — %s",
            len(decls), self.dialect, gemini_schema.describe_losses(losses),
        )
        self._tools = [types.Tool(function_declarations=decls)]
        # Last, after the server's own instructions: most salient position.
        self._system = "\n\n".join(
            part for part in (CONTRACT, instructions, GEMINI_PROHIBITION) if part
        )
        return self._system

    def user_turn(self, transcript: list, text: str) -> None:
        transcript.append(
            types.Content(role="user", parts=[types.Part(text=text)])
        )

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=self._system,
            tools=self._tools,
            max_output_tokens=config.MAX_TOKENS,
            # The loop is ours. See the module docstring.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            thinking_config=types.ThinkingConfig(include_thoughts=True),
        )

    async def stream(self, transcript: list) -> AsyncIterator[Delta | Reply]:
        parts: list[types.Part] = []
        calls: list[ToolCall] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        finish = ""

        chunks = await self.client.aio.models.generate_content_stream(
            model=self.model, contents=transcript, config=self._config(),
        )
        async for chunk in chunks:
            meta = getattr(chunk, "usage_metadata", None)
            if meta is not None:
                usage["input_tokens"] = getattr(meta, "prompt_token_count", 0) or 0
                usage["output_tokens"] = (
                    (getattr(meta, "candidates_token_count", 0) or 0)
                    + (getattr(meta, "thoughts_token_count", 0) or 0)
                )
                usage["cache_read_input_tokens"] = (
                    getattr(meta, "cached_content_token_count", 0) or 0
                )
            for candidate in getattr(chunk, "candidates", None) or []:
                if getattr(candidate, "finish_reason", None):
                    finish = str(candidate.finish_reason)
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    parts.append(part)
                    if getattr(part, "function_call", None) is not None:
                        calls.append(self._tool_call(part.function_call))
                    elif getattr(part, "text", None):
                        if getattr(part, "thought", False):
                            yield Delta("thinking", part.text)
                        else:
                            yield Delta("text", part.text)

        yield Reply(
            tool_calls=calls,
            stop="tools" if calls else _stop_for(finish),
            usage=usage,
            # Every part, thought signatures included: Gemini needs its own
            # thought_signature echoed back or a multi-step tool turn loses
            # the reasoning that led to the call.
            raw=types.Content(role="model", parts=parts),
        )

    def _tool_call(self, fc: Any) -> ToolCall:
        self._call_seq += 1
        return ToolCall(
            id=getattr(fc, "id", None) or f"gemini-call-{self._call_seq}",
            name=fc.name or "",
            params=dict(getattr(fc, "args", None) or {}),
        )

    def record_reply(self, transcript: list, reply: Reply) -> None:
        transcript.append(reply.raw)

    def record_results(
        self, transcript: list, pairs: list[tuple[ToolCall, ToolOutcome]]
    ) -> None:
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    # `id` only when the model supplied one; echoing a
                    # synthesised id back would be inventing protocol.
                    **({"id": call.id} if not call.id.startswith("gemini-call-") else {}),
                    name=call.name,
                    response=outcome.for_model_dict(),
                )
            )
            for call, outcome in pairs
        ]
        if parts:
            transcript.append(types.Content(role="user", parts=parts))

    def explain_error(self, exc: Exception) -> str:
        """
        Free-tier failures are ordinary here, so they get named.

        A turn costs one request per model round-trip, so a two-tool answer
        spends three against a per-model free-tier allowance in the low tens.
        Hitting it is not a bug and not something to retry around -- it is the
        tier working as sold -- but "ClientError" would send someone reading
        their own code instead of their quota page.
        """
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        detail = str(getattr(exc, "message", "") or exc)
        if code == 429:
            wait = _retry_seconds(detail)
            when = f"; retry in about {wait}" if wait else "; wait a minute and ask again"
            return (
                f"the Google AI Studio free tier's request quota for "
                f"{self.model} is spent{when}"
            )
        if code == 503:
            return (
                f"{self.model} is overloaded on the free tier "
                f"(503 'high demand'); try again, or set GEMINI_MODEL to another"
            )
        if code == 404:
            return (
                f"the model id {self.model!r} is not available to this key; "
                f"set GEMINI_MODEL to one that models.list() reports"
            )
        return type(exc).__name__

    def record_refusal(self, transcript: list, call: ToolCall, message: str) -> None:
        transcript.append(types.Content(role="user", parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    name=call.name, response={"error": message},
                )
            )
        ]))


def _retry_seconds(detail: str) -> str:
    """The retryDelay Google puts in a 429 body, if it is there."""
    import re

    match = re.search(r"[Rr]etry in (?:about )?([\d.]+)s", detail) or re.search(
        r"'retryDelay': '(\d+)s'", detail
    )
    return f"{round(float(match.group(1)))}s" if match else ""


def _stop_for(finish: str) -> str:
    """
    Gemini's finish reasons, mapped onto the loop's three.

    SAFETY and friends are the model declining, which the console reports as a
    refusal to the person rather than as a crash.
    """
    upper = finish.upper()
    for token in ("SAFETY", "BLOCKLIST", "PROHIBITED", "SPII", "RECITATION"):
        if token in upper:
            return "refusal"
    return "end"
