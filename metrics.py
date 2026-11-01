"""Append-only JSONL metrics.

The service already writes human-readable logs; those are great for watching and
terrible for answering "what is the success rate this week". One JSON object per
line fixes that without pulling in a metrics stack.
"""
from __future__ import annotations

import json
import os
import time


class Metrics:
    def __init__(self, path, enabled=True):
        self.path = path
        self.enabled = bool(enabled and path)
        if self.enabled:
            directory = os.path.dirname(os.path.abspath(path))
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                self.enabled = False

    def event(self, name, **fields):
        if not self.enabled:
            return False
        record = {"ts": round(time.time(), 3), "event": name}
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        try:
            with open(self.path, "a", encoding="utf-8") as target:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except OSError:
            self.enabled = False
            return False


def _percentile(values, ratio):
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio)))
    return ordered[index]


def summarize(path, days=7, now=None):
    """Aggregate the last ``days`` of events into counts and latency."""
    since = (now if now is not None else time.time()) - days * 86400
    counts = {}
    errors = {}
    latencies = []
    total = 0
    cached_total = 0
    input_total = 0
    try:
        with open(path, encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                stamp = record.get("ts")
                if not isinstance(stamp, (int, float)) or stamp < since:
                    continue
                name = record.get("event") or "unknown"
                counts[name] = counts.get(name, 0) + 1
                total += 1
                reason = record.get("reason")
                if reason:
                    errors[reason] = errors.get(reason, 0) + 1
                if name == "model_usage":
                    input_total += record.get("tokens_in") or 0
                    cached_total += record.get("cached") or 0
                latency = record.get("latency_ms")
                if name == "reply_sent" and isinstance(latency, (int, float)):
                    latencies.append(float(latency))
    except OSError:
        return {"days": days, "total": 0, "counts": {}, "reasons": {}, "reply_p95_ms": 0}
    return {
        "days": days,
        "total": total,
        "counts": counts,
        "reasons": errors,
        "reply_p95_ms": _percentile(latencies, 0.95),
        "reply_avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "input_tokens": input_total,
        "cached_tokens": cached_total,
        "cache_hit_ratio": round(cached_total / input_total, 3) if input_total else 0,
    }
