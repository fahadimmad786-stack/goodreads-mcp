"""
The Anthropic side of `Provider`. Nothing here is new behaviour.

This is the path the console has always taken, lifted out of `agent.py` so a
second provider could exist without a second loop. The things worth keeping in
mind while reading it are the two that are easy to lose in a refactor:

* `cache_control` sits on the LAST system block, so the cached prefix covers
  the tool array and both system blocks -- the whole static head of every
  request. Tool order is sorted by `bridge.describe()` for the same reason.
* tool inputs are the SDK's parsed `input`, never string-matched. Models vary
  their JSON string escaping and the parsed object is the only safe source.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from . import config
from .mcp_client import MCPBridge, ToolOutcome
from .provider import Delta, Reply, ToolCall


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, bridge: MCPBridge, client: AsyncAnthropic | None = None):
        self.bridge = bridge
        self.model = config.MODEL
        self.client = client or AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tools: list[dict] = []
        self._system: list[dict] = []

    async def declare(self, bridge: MCPBridge | None = None) -> str:
        from .agent import CONTRACT

        tools, instructions = await (bridge or self.bridge).describe()
        blocks: list[dict] = [{"type": "text", "text": CONTRACT}]
        if instructions:
            blocks.append({"type": "text", "text": instructions})
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        self._tools = tools
        self._system = blocks
        return CONTRACT + "\n" + instructions

    def user_turn(self, transcript: list, text: str) -> None:
        transcript.append({"role": "user", "content": text})

    async def stream(self, transcript: list) -> AsyncIterator[Delta | Reply]:
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=config.MAX_TOKENS,
            system=self._system,
            tools=self._tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": config.EFFORT},
            messages=transcript,
        ) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type == "text_delta":
                    yield Delta("text", event.delta.text)
                elif event.delta.type == "thinking_delta":
                    yield Delta("thinking", event.delta.thinking)
            final = await stream.get_final_message()

        usage = {
            key: getattr(final.usage, key, 0) or 0
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens")
        }
        calls = [
            ToolCall(id=b.id, name=b.name, params=_as_dict(b.input))
            for b in final.content
            if b.type == "tool_use"
        ]
        if final.stop_reason == "refusal":
            stop = "refusal"
        elif final.stop_reason == "tool_use" and calls:
            stop = "tools"
        else:
            stop = "end"
        yield Reply(tool_calls=calls, stop=stop, usage=usage, raw=final.content)

    def record_reply(self, transcript: list, reply: Reply) -> None:
        transcript.append({"role": "assistant", "content": reply.raw})

    def record_results(
        self, transcript: list, pairs: list[tuple[ToolCall, ToolOutcome]]
    ) -> None:
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": outcome.for_model(),
                **({"is_error": True} if outcome.kind == "transport" else {}),
            }
            for call, outcome in pairs
        ]
        if blocks:
            transcript.append({"role": "user", "content": blocks})

    def explain_error(self, exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        if status == 429:
            return "rate limited by the Anthropic API; wait a moment and ask again"
        if status is not None and 500 <= int(status) < 600:
            return f"the Anthropic API returned {status}; this is usually temporary"
        return type(exc).__name__

    def record_refusal(self, transcript: list, call: ToolCall, message: str) -> None:
        transcript.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps({"error": message}),
                "is_error": True,
            }],
        })


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
