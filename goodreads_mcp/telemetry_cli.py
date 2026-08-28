"""
Summarise the telemetry log.

    goodreads-telemetry [--path FILE] [--tool NAME] [--since ISO8601] [--json]

Unlike the server, this is an ordinary CLI in its own process -- writing the
report to stdout is correct here. It never imports the server, so running it
cannot touch a live stdio session.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .telemetry import log_path

OUTCOMES = ("ok", "guard_rejected", "bq_error", "other_error")

# Parameters worth knowing the distribution of: they decide what a result
# means, so it matters which values callers actually pass.
TRACKED_PARAMS = ("min_ratings", "unit")


def _records(text: str) -> tuple[list[dict], int]:
    """
    Accept both shapes the log arrives in.

    JSONL is what the file sink writes. A JSON array is what
    `gcloud logging read --format=json` emits for the Cloud Run sink, where
    each entry wraps our line in `jsonPayload`.
    """
    stripped = text.lstrip()
    if stripped.startswith("["):
        entries = json.loads(stripped)
        out = [e.get("jsonPayload", e) for e in entries if isinstance(e, dict)]
        return [r for r in out if isinstance(r, dict)], 0

    rows: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return rows, malformed


def load(path: Path, tool: str | None, since: str | None) -> tuple[list[dict], int]:
    """`path` of "-" reads stdin, so Cloud Logging output can be piped in."""
    if str(path) == "-":
        text = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()

    rows, malformed = _records(text)
    kept = [
        r
        for r in rows
        if (not tool or r.get("tool") == tool)
        and (not since or str(r.get("ts", "")) >= since)
    ]
    return kept, malformed


def pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "-"


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


def quantile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Exact for the sizes a log reaches; no deps."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarise(rows: list[dict]) -> dict[str, Any]:
    per_tool: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "bytes_billed": 0, "durations": []}
    )
    outcomes: Counter[str] = Counter()
    guard_rules: Counter[str] = Counter()
    guard_columns: Counter[str] = Counter()
    params: dict[str, Counter] = {p: Counter() for p in TRACKED_PARAMS}
    durations: list[float] = []
    total_billed = 0
    total_processed = 0
    cache_hits = 0
    cache_known = 0

    for r in rows:
        tool = r.get("tool", "?")
        outcome = r.get("outcome", "?")
        dur = float(r.get("duration_ms") or 0.0)
        billed = int(r.get("bytes_billed") or 0)

        outcomes[outcome] += 1
        t = per_tool[tool]
        t["calls"] += 1
        t["bytes_billed"] += billed
        t["durations"].append(dur)
        if outcome != "ok":
            t["errors"] += 1

        durations.append(dur)
        total_billed += billed
        total_processed += int(r.get("bytes_processed") or 0)

        if r.get("cache_hit") is not None:
            cache_known += 1
            if r["cache_hit"]:
                cache_hits += 1

        if outcome == "guard_rejected":
            guard_rules[r.get("guard_rule") or "?"] += 1
            guard_columns[r.get("guard_column") or "(none)"] += 1

        for key, counter in params.items():
            if key in (r.get("params") or {}):
                counter[repr(r["params"][key])] += 1

    return {
        "calls": len(rows),
        "outcomes": dict(outcomes),
        "error_rate": (len(rows) - outcomes.get("ok", 0)) / len(rows) if rows else 0.0,
        "p50_ms": quantile(durations, 0.50),
        "p95_ms": quantile(durations, 0.95),
        "bytes_billed": total_billed,
        "bytes_processed": total_processed,
        "cache_hit_rate": (cache_hits / cache_known) if cache_known else None,
        "per_tool": {
            name: {
                "calls": v["calls"],
                "errors": v["errors"],
                "bytes_billed": v["bytes_billed"],
                "p50_ms": quantile(v["durations"], 0.50),
                "p95_ms": quantile(v["durations"], 0.95),
            }
            for name, v in sorted(per_tool.items(), key=lambda kv: -kv[1]["calls"])
        },
        "guard_rejections_by_rule": dict(guard_rules),
        "guard_rejections_by_column": dict(guard_columns),
        "params": {k: dict(v) for k, v in params.items()},
    }


def render(s: dict[str, Any], path: Path, malformed: int) -> str:
    out: list[str] = []
    w = out.append

    w(f"{path}  --  {s['calls']} calls")
    if malformed:
        w(f"  !! {malformed} malformed line(s) skipped")
    if not s["calls"]:
        return "\n".join(out)

    w("")
    w(f"  error rate   {s['error_rate'] * 100:.1f}%"
      f"   ({', '.join(f'{k}={v}' for k, v in sorted(s['outcomes'].items()))})")
    w(f"  duration     p50 {s['p50_ms']:.0f} ms   p95 {s['p95_ms']:.0f} ms")
    w(f"  billed       {human_bytes(s['bytes_billed'])}"
      f"   (processed {human_bytes(s['bytes_processed'])})")
    if s["cache_hit_rate"] is not None:
        w(f"  cache hits   {s['cache_hit_rate'] * 100:.0f}%")

    w("")
    w(f"  {'tool':<32}{'calls':>7}{'err':>6}{'p50 ms':>10}{'p95 ms':>10}{'billed':>13}")
    w(f"  {'-' * 78}")
    for name, t in s["per_tool"].items():
        w(f"  {name:<32}{t['calls']:>7}{t['errors']:>6}"
          f"{t['p50_ms']:>10.0f}{t['p95_ms']:>10.0f}"
          f"{human_bytes(t['bytes_billed']):>13}")

    if s["guard_rejections_by_rule"]:
        w("")
        w("  guard rejections by rule")
        for rule, n in sorted(s["guard_rejections_by_rule"].items(), key=lambda kv: -kv[1]):
            w(f"    {rule:<28}{n:>5}")
        w("  guard rejections by column")
        for col, n in sorted(s["guard_rejections_by_column"].items(), key=lambda kv: -kv[1]):
            w(f"    {col:<28}{n:>5}")

    for key in TRACKED_PARAMS:
        values = s["params"].get(key) or {}
        w("")
        if values:
            w(f"  {key} values passed")
            for val, n in sorted(values.items(), key=lambda kv: -kv[1]):
                w(f"    {val:<28}{n:>5}   {pct(n, s['calls'])} of calls")
        else:
            w(f"  {key} values passed: none (always left at its default)")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="goodreads-telemetry", description="Summarise the goodreads-mcp telemetry log."
    )
    ap.add_argument(
        "--path",
        type=Path,
        default=None,
        help="log file, or - for stdin. Default: the server's local log. "
             "For Cloud Run: gcloud logging read ... --format=json | %(prog)s --path -",
    )
    ap.add_argument("--tool", default=None, help="only this tool")
    ap.add_argument("--since", default=None, help="only lines with ts >= this ISO8601 prefix")
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = ap.parse_args(argv)

    path = args.path or log_path()
    if str(path) != "-" and not path.exists():
        print(f"no telemetry log at {path}", file=sys.stderr)
        return 1

    rows, malformed = load(path, args.tool, args.since)
    summary = summarise(rows)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render(summary, path, malformed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
