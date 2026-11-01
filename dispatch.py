"""Run model calls off the UI thread.

AT-SPI is not safe to touch from two threads, so this dispatcher only ever runs
the *network* part. Detection and sending stay on the polling thread.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

DEFAULT_STALE_SECONDS = 120.0


class Dispatcher:
    def __init__(self, respond, workers=2, max_outbox=64, stale_after=DEFAULT_STALE_SECONDS):
        self._respond = respond
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(workers)))
        self._lock = threading.Lock()
        self._jobs = {}
        self._outbox = deque(maxlen=max(4, int(max_outbox)))
        self._stale_after = float(stale_after)
        self._closed = False

    def submit(self, key, payload, now=None):
        """Queue one job per conversation; returns False when one is in flight."""
        stamp = now if now is not None else time.monotonic()
        with self._lock:
            if self._closed or key in self._jobs:
                return False
            future = self._pool.submit(self._run, payload)
            self._jobs[key] = {"future": future, "payload": payload, "started": stamp}
            return True

    def _run(self, payload):
        try:
            return ("reply", self._respond(payload))
        except Exception as exc:  # noqa: BLE001 - reported, never raised to the loop
            return ("error", type(exc).__name__)

    def in_flight(self, key):
        with self._lock:
            return key in self._jobs

    def pending(self):
        with self._lock:
            return len(self._jobs)

    def drain(self, now=None):
        """Collect finished or stale jobs as (key, payload, status, value)."""
        stamp = now if now is not None else time.monotonic()
        with self._lock:
            ready = [
                (key, entry)
                for key, entry in self._jobs.items()
                if entry["future"].done() or stamp - entry["started"] >= self._stale_after
            ]
            for key, _ in ready:
                self._jobs.pop(key, None)
        results = []
        for key, entry in ready:
            if entry["future"].done():
                try:
                    status, value = entry["future"].result()
                except Exception as exc:  # noqa: BLE001
                    status, value = "error", type(exc).__name__
            else:
                status, value = "timeout", None
            results.append((key, entry["payload"], status, value))
        return results

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False)
