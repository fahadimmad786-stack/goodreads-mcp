"""
In-memory conversation state, keyed by a cookie.

The transcript is held server-side rather than posted back by the client on
each turn, and that is a correctness decision rather than a convenience one:
if the client supplied the history, a client could forge tool results into the
model's context. Fabricated figures in history is precisely the failure this
project exists to prevent, so the only thing a client may contribute is its
own text.

The cost is honest and stated in the README: an instance recycle loses the
transcript, and the client is told so rather than silently continuing against
an empty history.
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import config


@dataclass
class Session:
    id: str
    # The transcript is whatever the active provider puts there -- Anthropic
    # content blocks, Gemini Contents -- so it is only meaningful alongside the
    # name of the provider that wrote it. Handing one provider the other's
    # history is not a degraded conversation, it is a malformed request, so
    # `adopt()` below refuses instead of hoping.
    provider: str | None = None
    messages: list[Any] = field(default_factory=list)
    turns: int = 0
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Every numeral the server has put in front of this session, accumulated
    # across turns: tool output, the server's own instructions, and the user's
    # own questions. numcheck.py checks model prose against it.
    sourced_numbers: set[str] = field(default_factory=set)

    def touch(self) -> None:
        self.last_seen = time.time()

    def adopt(self, provider: str) -> int:
        """
        Claim this session for `provider`, discarding a transcript another one
        wrote. Returns how many messages were dropped -- 0 in every ordinary
        case.

        A switch happens when a deployment gains or loses a key, or when
        CHAT_PROVIDER changes under a browser holding a live cookie. The
        transcript is provider-native and cannot be translated: Gemini has no
        `tool_use` block and Anthropic has no `functionResponse` part, so
        passing one to the other fails inside the SDK with an error about a
        field, which tells the person nothing. Dropped loudly instead, and the
        caller states it in the turn.

        `turns` deliberately survives. It is the per-session spend ceiling, and
        a reset that refilled it would make provider-switching a way around it.
        """
        if self.provider == provider:
            return 0
        dropped = len(self.messages)
        self.provider = provider
        self.messages = []
        # Sourced numerals belonged to the discarded conversation; keeping them
        # would let the checker call a figure sourced on the strength of a
        # transcript the model can no longer see.
        self.sourced_numbers = set()
        return dropped

    @property
    def turns_left(self) -> int:
        return max(0, config.MAX_TURNS_PER_SESSION - self.turns)


class SessionStore:
    """Bounded LRU-ish store. Oldest-touched sessions are evicted first."""

    def __init__(self, max_sessions: int | None = None, ttl_s: float | None = None):
        self._sessions: dict[str, Session] = {}
        self._max = max_sessions if max_sessions is not None else config.MAX_SESSIONS
        self._ttl = ttl_s if ttl_s is not None else config.SESSION_TTL_S

    def new(self) -> Session:
        self._reap()
        sid = secrets.token_urlsafe(24)
        s = Session(id=sid)
        self._sessions[sid] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._sessions.get(sid)
        if s is None:
            return None
        if time.time() - s.last_seen > self._ttl:
            self._sessions.pop(sid, None)
            return None
        s.touch()
        return s

    def get_or_new(self, sid: str | None) -> tuple[Session, bool]:
        """Returns (session, is_new). is_new drives the client's 'restarted' notice."""
        s = self.get(sid)
        if s is not None:
            return s, False
        return self.new(), True

    def _reap(self) -> None:
        now = time.time()
        for sid, s in list(self._sessions.items()):
            if now - s.last_seen > self._ttl:
                del self._sessions[sid]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            del self._sessions[oldest.id]

    def __len__(self) -> int:
        return len(self._sessions)


class RateLimiter:
    """Per-key sliding window. Keys are client IPs."""

    def __init__(self, limit: int | None = None, window_s: float | None = None):
        self._limit = limit if limit is not None else config.RATE_LIMIT_TURNS
        self._window = window_s if window_s is not None else config.RATE_LIMIT_WINDOW_S
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, seconds_until_next_slot)."""
        now = time.time()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= self._limit:
            return False, round(self._window - (now - q[0]), 1)
        q.append(now)
        if len(self._hits) > 4096:  # unbounded dict is the only leak here
            for k in [k for k, v in self._hits.items() if not v][:2048]:
                self._hits.pop(k, None)
        return True, 0.0
