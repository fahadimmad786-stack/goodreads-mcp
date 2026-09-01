"""
The turn loop: Anthropic streaming plus MCP tool calls, emitted as SSE frames.

A manual loop rather than the SDK tool runner, because the frames are the
product: each call needs a `tool_call` frame before it and a `tool_result` or
`tool_refusal` frame after it, with timing measured on this side, and a
`ParamError` result has to be reclassified as a refusal rather than passed
through as ordinary tool output.

The model never renders a figure. It receives full envelopes -- it needs the
caveats to explain what a number is worth -- and writes prose around cards the
client draws from the same JSON. numcheck.py checks the prose afterwards.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from . import config, guard_probe, numcheck
# Both frame builders are shared with tool mode, which must never import
# this module: it constructs an Anthropic client, and tool mode has no key.
from .frames import _origin, _result_frame
from .mcp_client import MCPBridge
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
    def __init__(self, bridge: MCPBridge, client: AsyncAnthropic | None = None):
        self.bridge = bridge
        self.client = client or AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    async def system_blocks(self) -> tuple[list[dict], list[dict], str]:
        """
        (tools, system blocks, all static prompt text).

        `cache_control` sits on the last system block so the cached prefix
        covers the tool array and both system blocks -- the whole static head
        of every request. The third return value seeds the numeral checker:
        anything the server told the model in its own instructions counts as
        sourced.
        """
        tools, instructions = await self.bridge.describe()
        blocks: list[dict] = [{"type": "text", "text": CONTRACT}]
        if instructions:
            blocks.append({"type": "text", "text": instructions})
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        static_text = CONTRACT + "\n" + instructions
        return tools, blocks, static_text

    async def run_turn(
        self, session: Session, user_text: str
    ) -> AsyncIterator[dict[str, Any]]:
        tools, system, static_text = await self.system_blocks()

        # Everything the server has said, plus the user's own words, counts as
        # a source for a numeral in the prose.
        session.sourced_numbers |= numcheck.numerals_in_text(static_text)
        session.sourced_numbers |= numcheck.numerals_in_text(user_text)

        messages = session.messages + [{"role": "user", "content": user_text}]
        prose_parts: list[str] = []
        calls_made = 0
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

        while True:
            try:
                async with self.client.messages.stream(
                    model=config.MODEL,
                    max_tokens=config.MAX_TOKENS,
                    system=system,
                    tools=tools,
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": config.EFFORT},
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        if event.type != "content_block_delta":
                            continue
                        if event.delta.type == "text_delta":
                            prose_parts.append(event.delta.text)
                            yield {"type": "text_delta", "text": event.delta.text}
                        elif event.delta.type == "thinking_delta":
                            yield {"type": "thinking_delta", "text": event.delta.thinking}
                    final = await stream.get_final_message()
            except Exception as exc:  # noqa: BLE001
                log.exception("anthropic call failed")
                yield {
                    "type": "error",
                    "message": f"the model call failed: {type(exc).__name__}",
                }
                return

            for key in usage_total:
                usage_total[key] += getattr(final.usage, key, 0) or 0

            if final.stop_reason == "refusal":
                yield {
                    "type": "error",
                    "message": "the model declined to answer this one; try rephrasing.",
                }
                return

            messages.append({"role": "assistant", "content": final.content})

            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if final.stop_reason != "tool_use" or not tool_uses:
                break

            results: list[dict] = []
            for block in tool_uses:
                if calls_made >= config.MAX_TOOL_CALLS_PER_TURN:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(
                                {
                                    "error": (
                                        "tool-call budget for this turn is spent; "
                                        "answer from what you already have"
                                    )
                                }
                            ),
                            "is_error": True,
                        }
                    )
                    yield {
                        "type": "tool_refusal",
                        "id": block.id,
                        "tool": block.name,
                        "origin": _origin(block.name),
                        "params": _as_dict(block.input),
                        "kind": "budget",
                        "message": (
                            f"tool-call budget for this turn "
                            f"({config.MAX_TOOL_CALLS_PER_TURN}) is spent."
                        ),
                        "caveats": [],
                    }
                    continue

                params = _as_dict(block.input)
                yield {
                    "type": "tool_call",
                    "id": block.id,
                    "tool": block.name,
                    "origin": _origin(block.name),
                    "params": params,
                }
                outcome = await self.bridge.call(block.name, params)
                calls_made += 1
                session.sourced_numbers |= numcheck.collect(params)
                if outcome.envelope is not None:
                    session.sourced_numbers |= numcheck.collect(outcome.envelope)

                yield _result_frame(block.id, outcome)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.for_model(),
                        **({"is_error": True} if outcome.kind == "transport" else {}),
                    }
                )

            messages.append({"role": "user", "content": results})

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

        session.messages = messages
        session.turns += 1
        yield {
            "type": "turn_end",
            "usage": usage_total,
            "tool_calls": calls_made,
            "turns_left": session.turns_left,
        }


def _as_dict(value: Any) -> dict:
    """
    Tool inputs are parsed JSON, never string-matched.

    Current models vary their JSON string escaping, so the SDK's parsed `input`
    is the only safe source.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"value": value}
    return {}
