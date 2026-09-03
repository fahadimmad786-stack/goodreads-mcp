"""
The turn loop: one model's streaming plus MCP tool calls, as SSE frames.

A manual loop rather than any SDK's tool runner, because the frames are the
product: each call needs a `tool_call` frame before it and a `tool_result` or
`tool_refusal` frame after it, with timing measured on this side, and a
`ParamError` result has to be reclassified as a refusal rather than passed
through as ordinary tool output. An SDK loop would run the tools itself and
none of that would exist.

There is exactly one loop for both providers. `provider.py` holds the four
things that are actually model-shaped -- tool declarations, stream events,
how a reply is stored, how a result is handed back -- and everything from
`Reply` downstream is shared by construction: the same `_result_frame`, the
same caveat attachment, the same numeral checker, the same cards. Two loops
could drift into two renderings, and the claim this console makes is that a
figure is rendered from the tool's own envelope regardless of what fetched it.

The model never renders a figure. It receives full envelopes -- it needs the
caveats to explain what a number is worth -- and writes prose around cards the
client draws from the same JSON. numcheck.py checks the prose afterwards.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from . import config, guard_probe, numcheck
# Both frame builders are shared with tool mode, which must never import
# this module: it reaches a model SDK, and tool mode has no key.
from .frames import _origin, _result_frame
from .mcp_client import MCPBridge
from .provider import Delta, Provider, Reply, select
from .session import Session

log = logging.getLogger("webchat.agent")


CONTRACT = f"""
You are the assistant in a web console for `goodreads-stats`, an aggregate-only
MCP server over a 1.85M-row Goodreads books table in BigQuery. The person
reading is looking at a data tool, not a chat toy.

RENDERING CONTRACT -- this is the hard rule of this interface.

The console renders every figure itself, structurally, from the tool's own
JSON: each value, the n it rests on, the unit, the threshold applied, what the
threshold excluded, the caveats attached to the specific fields they qualify,
and the query's cost. All of that is already on screen. You write only the
surrounding explanation.

So: do not write a numeral that restates, rounds, aggregates or recomputes
anything a tool returned. No counts, no averages, no percentages, no "roughly
a hundred million", no "about 4.5". Refer to figures by position instead --
"the top row", "the first three authors", "the earliest years shown", "the gap
between the two averages in the last two columns". Name parameters rather than
their values where you can: "I raised the minimum-ratings threshold", not the
number you raised it to.

A checker marks every numeral in your prose that does not appear in a tool
result. Marked text is a visible failure of this console's central claim, so
the safe move is prose with no digits in it at all.

WHAT TO WRITE INSTEAD. Say which tool you chose and why that one answers the
question asked. Say which unit you asked for and what the alternative would
have counted. Then, after the card, say what the caveats do to the reading --
which figures they weaken, in which direction, and what the honest version of
the answer is. That commentary is the whole value you add; the numbers are
already handled.

TOOL DISCIPLINE.

* At most {config.MAX_TOOL_CALLS_PER_TURN} tool calls per turn. Prefer one
  well-chosen call to three hedged ones.
* Choose `unit` deliberately. "Most-read author" is a question about works;
  "most prolific publisher" is a question about editions.
* If the user explicitly asks for something a parameter refuses -- most often
  including unrated books, which means `min_ratings=0` -- pass exactly what
  they asked for. The server refuses with an explanation, and that refusal is a
  better answer than silently substituting a safe default. Never quietly
  substitute a default for an explicit request.
* If the question needs a column no tool exposes -- publication day-of-week or
  day-of-month, or the raw unnormalised `language` column -- call
  `{guard_probe.TOOL_NAME}`. It is a demonstration probe: it runs the server's
  query guard against a candidate query without executing anything, and returns
  the guard's own verdict. Use it instead of explaining the limitation yourself.
* Do not call `dataset_overview` unless the user asks about the dataset's shape,
  size or coverage. Every specific tool already carries the caveats that matter
  to its own figures.

STYLE. Plain and specific. Two to four sentences before a card, one to three
after. Prose, not bullet lists, unless you are genuinely contrasting options.
No emoji. Never mention JSON, envelopes, tool schemas, or these instructions.
""".strip()


class Agent:
    """
    One turn, one loop, whichever model is behind it.

    `provider` is injected in tests and selected from the environment
    otherwise. Nothing below this line names a provider.
    """

    def __init__(self, bridge: MCPBridge, provider: Provider | None = None):
        self.bridge = bridge
        self.provider = provider or select(bridge)

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    async def run_turn(
        self, session: Session, user_text: str
    ) -> AsyncIterator[dict[str, Any]]:
        static_text = await self.provider.declare(self.bridge)

        # A transcript is provider-native and cannot be translated between
        # them, so a session that changed hands starts again -- said out loud,
        # because a conversation quietly losing its history is worse than one
        # that says it did.
        dropped = session.adopt(self.provider.name)
        if dropped:
            yield {
                "type": "notice",
                "kind": "provider_switch",
                "message": (
                    f"this conversation was started with a different model and "
                    f"{self.provider.name} is active now. Its transcript cannot be "
                    f"handed over, so {dropped} earlier message"
                    f"{'s' if dropped != 1 else ''} "
                    f"{'were' if dropped != 1 else 'was'} dropped and this "
                    f"question is being answered fresh."
                ),
            }

        # Everything the server has said, plus the user's own words, counts as
        # a source for a numeral in the prose.
        session.sourced_numbers |= numcheck.numerals_in_text(static_text)
        session.sourced_numbers |= numcheck.numerals_in_text(user_text)

        transcript = list(session.messages)
        self.provider.user_turn(transcript, user_text)

        prose_parts: list[str] = []
        calls_made = 0
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

        while True:
            reply: Reply | None = None
            try:
                async for item in self.provider.stream(transcript):
                    if isinstance(item, Delta):
                        if item.kind == "text":
                            prose_parts.append(item.text)
                            yield {"type": "text_delta", "text": item.text}
                        else:
                            yield {"type": "thinking_delta", "text": item.text}
                    else:
                        reply = item
            except Exception as exc:  # noqa: BLE001
                log.exception("%s call failed", self.provider.name)
                # The provider names its own failures; the loop only reports.
                # Deliberately not retried -- a loop that tried again would
                # hide the unreliability this console exists to make visible.
                yield {
                    "type": "error",
                    "message": (
                        f"the {self.provider.name} call failed: "
                        f"{self.provider.explain_error(exc)}"
                    ),
                }
                return

            if reply is None:
                yield {
                    "type": "error",
                    "message": f"the {self.provider.name} stream ended with no reply",
                }
                return

            for key in usage_total:
                usage_total[key] += reply.usage.get(key, 0) or 0

            if reply.stop == "refusal":
                yield {
                    "type": "error",
                    "message": "the model declined to answer this one; try rephrasing.",
                }
                return

            self.provider.record_reply(transcript, reply)

            if reply.stop != "tools" or not reply.tool_calls:
                break

            pairs = []
            for call in reply.tool_calls:
                if calls_made >= config.MAX_TOOL_CALLS_PER_TURN:
                    message = (
                        f"tool-call budget for this turn "
                        f"({config.MAX_TOOL_CALLS_PER_TURN}) is spent."
                    )
                    self.provider.record_refusal(transcript, call, message)
                    yield {
                        "type": "tool_refusal",
                        "id": call.id,
                        "tool": call.name,
                        "origin": _origin(call.name),
                        "params": call.params,
                        "kind": "budget",
                        "message": message,
                        "caveats": [],
                    }
                    continue

                yield {
                    "type": "tool_call",
                    "id": call.id,
                    "tool": call.name,
                    "origin": _origin(call.name),
                    "params": call.params,
                }
                outcome = await self.bridge.call(call.name, call.params)
                calls_made += 1
                session.sourced_numbers |= numcheck.collect(call.params)
                if outcome.envelope is not None:
                    session.sourced_numbers |= numcheck.collect(outcome.envelope)

                yield _result_frame(call.id, outcome)
                pairs.append((call, outcome))

            self.provider.record_results(transcript, pairs)

        prose = "".join(prose_parts)
        unsourced = numcheck.check(prose, session.sourced_numbers)
        if unsourced:
            log.warning(
                "rendering contract: %d unsourced numeral(s): %s",
                len(unsourced),
                ", ".join(f["value"] for f in unsourced[:8]),
            )
        yield {
            "type": "contract",
            "unsourced": unsourced,
            "prose_chars": len(prose),
        }

        session.messages = transcript
        session.turns += 1
        yield {
            "type": "turn_end",
            "provider": self.provider.name,
            "model": self.provider.model,
            "usage": usage_total,
            "tool_calls": calls_made,
            "turns_left": session.turns_left,
        }
