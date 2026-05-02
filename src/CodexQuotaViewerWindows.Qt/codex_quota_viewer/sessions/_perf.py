"""Tiny perf instrumentation helper for the sessions hot paths.

Disabled unless the ``CQV_PERF`` env var is set. When enabled, every
``with _perf_timer("label", k=v): ...`` block prints a single line to
stdout with elapsed milliseconds and the kwargs as ``key=value`` pairs.

Kept off the stdlib logging tree so dev runs without ``CQV_PERF`` pay
nothing — the contextmanager just yields and returns.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator


_PERF_ENABLED = bool(os.environ.get("CQV_PERF"))


@contextmanager
def _perf_timer(label: str, **extra: object) -> Iterator[None]:
    if not _PERF_ENABLED:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        kv = " ".join(
            f"{k}={v}" for k, v in extra.items() if v is not None
        )
        print(f"[cqv-perf] {label} {elapsed_ms:.1f}ms {kv}".rstrip(), flush=True)


def _perf_log(label: str, **extra: object) -> None:
    """Fire-and-forget perf marker — emits a single line WITHOUT timing.
    Used to mark progress through a loop where a hang would prevent the
    enclosing _perf_timer's exit log from ever printing. The last line
    we see before a freeze is the smoking gun."""
    if not _PERF_ENABLED:
        return
    kv = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
    print(f"[cqv-perf] {label} {kv}".rstrip(), flush=True)
