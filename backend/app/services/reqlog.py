"""Who is spending the request budget, and on what.

Four jobs share one allowance to each upstream host: the catalogue crawler,
the shelf-life sampler, the watch poller and the MyFigureCollection linker.
When the budget runs short it is never obvious which of them is eating it, and
the per-job settings only say what each was *allowed* rather than what it
actually used.

This keeps a short in-memory record of every outbound request, tagged with the
work it was for, so the admin view can answer that directly. In memory on
purpose: it is a live readout, not history, and writing a database row per
request would cost more than the thing it measures. A restart clears it, which
is fine - after a restart there is nothing interesting to look at anyway.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextvars import ContextVar

#: What the current thread is doing, so a request can be attributed without
#: every call site having to pass it along. Set by whichever job is running;
#: anything that does not set it shows up as "other", which in practice means
#: someone opening an item and it refreshing from the shop.
_purpose: ContextVar[str] = ContextVar("request_purpose", default="other")

#: Long enough for an hourly rate to be real rather than extrapolated, and
#: small enough to stay cheap: at 40 requests a minute this holds under three
#: thousand entries, a few hundred kilobytes.
WINDOW_SECONDS = 3600

PURPOSE_LABELS = {
    "catalogue": "Catalogue sweep",
    "shelf": "Shelf-life sampler",
    "watch": "Watch polling",
    "mfc": "MyFigureCollection linking",
    "images": "Photo downloads",
    "manual": "Opened by hand",
    "other": "Other",
}

_events: deque[tuple[float, str, str, bool]] = deque()
_lock = threading.Lock()


class purpose:
    """Tag every request made inside this block.

    Used as a context manager around a job's work rather than at each call
    site, because the interesting boundary is "this is the crawler running",
    not "this is one HTTP request".
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._token = None

    def __enter__(self) -> "purpose":
        self._token = _purpose.set(self.name)
        return self

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _purpose.reset(self._token)
        return None


def current() -> str:
    return _purpose.get()


def record(host: str, ok: bool = True, name: str | None = None) -> None:
    """Note one outbound request against whatever work is running."""
    now = time.time()
    with _lock:
        _events.append((now, host, name or _purpose.get(), ok))
        cutoff = now - WINDOW_SECONDS
        while _events and _events[0][0] < cutoff:
            _events.popleft()


def rates(seconds: int = 60) -> dict:
    """Requests in the last ``seconds``, split by host and by purpose.

    Rates are reported per minute and per hour from the same sample. The hourly
    figure over a one-minute window is an extrapolation and says so, because a
    minute of crawling scaled up by sixty is not a promise about the next hour.
    """
    seconds = max(1, min(seconds, WINDOW_SECONDS))
    cutoff = time.time() - seconds
    with _lock:
        sample = [event for event in _events if event[0] >= cutoff]

    hosts: dict[str, dict] = {}
    for _, host, why, ok in sample:
        entry = hosts.setdefault(host, {"total": 0, "errors": 0, "purposes": {}})
        entry["total"] += 1
        if not ok:
            entry["errors"] += 1
        entry["purposes"][why] = entry["purposes"].get(why, 0) + 1

    for entry in hosts.values():
        entry["per_minute"] = round(entry["total"] / seconds * 60, 1)
        entry["per_hour"] = round(entry["total"] / seconds * 3600)
        entry["purposes"] = [
            {
                "key": why,
                "label": PURPOSE_LABELS.get(why, why),
                "requests": count,
                "per_minute": round(count / seconds * 60, 1),
                "share": round(count / entry["total"] * 100, 1) if entry["total"] else 0.0,
            }
            for why, count in sorted(entry["purposes"].items(), key=lambda kv: -kv[1])
        ]

    with _lock:
        held = len(_events)
    return {
        "window_seconds": seconds,
        "sampled": len(sample),
        "held": held,
        # A window shorter than an hour makes the hourly column a projection.
        "hourly_is_projected": seconds < 3600,
        "hosts": hosts,
    }
