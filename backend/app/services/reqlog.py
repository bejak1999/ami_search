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

#: Which of a purpose's several jobs this is. The catalogue is four slices
#: sharing one purpose, and a debug view that mixed them told you what some
#: slice was doing rather than the one you opened.
_tag: ContextVar[str] = ContextVar("request_tag", default="")

#: Long enough for an hourly rate to be real rather than extrapolated, and
#: small enough to stay cheap: at 40 requests a minute this holds under three
#: thousand entries, a few hundred kilobytes.
WINDOW_SECONDS = 3600

#: Hosts, as they are counted. Photos come from a static image server with
#: its own allowance; the API is the one jobs queue for.
HOST_LABELS = {
    "amiami": "AmiAmi API",
    "amiami-images": "AmiAmi photos",
    "mfc": "MyFigureCollection",
}

PURPOSE_LABELS = {
    "catalogue": "Catalogue sweep",
    "shelf": "Shelf-life sampler",
    "watch": "Watch polling",
    "mfc": "MyFigureCollection linking",
    "images": "Photo downloads",
    "manual": "Opened by hand",
    "other": "Other",
}

#: (when, host, purpose, ok, url, status, milliseconds)
_events: deque[tuple[float, str, str, bool, str, int | None, float | None]] = deque()
_lock = threading.Lock()

#: The last few requests in full, per purpose, for the debug view. Short: this
#: is for looking over a job's shoulder, not for keeping history.
RECENT_PER_PURPOSE = 40
_recent: dict[str, deque] = {}

#: What each job says it is doing at this moment. Set by the job itself,
#: because only it knows - the request log can say a page was fetched but not
#: that it was page 14 of the pre-owned slice on its third pass.
_doing: dict[str, dict] = {}


class purpose:
    """Tag every request made inside this block.

    Used as a context manager around a job's work rather than at each call
    site, because the interesting boundary is "this is the crawler running",
    not "this is one HTTP request".
    """

    def __init__(self, name: str, tag: str = "") -> None:
        self.name = name
        self.tag = tag
        self._token = None
        self._tag_token = None

    def __enter__(self) -> "purpose":
        self._token = _purpose.set(self.name)
        self._tag_token = _tag.set(self.tag)
        return self

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _purpose.reset(self._token)
        if self._tag_token is not None:
            _tag.reset(self._tag_token)
        return None


def current() -> str:
    return _purpose.get()


def record(
    host: str,
    ok: bool = True,
    name: str | None = None,
    url: str | None = None,
    status: int | None = None,
    ms: float | None = None,
) -> None:
    """Note one outbound request against whatever work is running."""
    now = time.time()
    why = name or _purpose.get()
    with _lock:
        _events.append((now, host, why, ok, url or "", status, ms))
        cutoff = now - WINDOW_SECONDS
        while _events and _events[0][0] < cutoff:
            _events.popleft()

        trail = _recent.get(why)
        if trail is None:
            trail = _recent[why] = deque(maxlen=RECENT_PER_PURPOSE)
        trail.appendleft(
            {
                "at": now,
                "host": host,
                "ok": ok,
                "tag": _tag.get(),
                "url": url or "",
                "status": status,
                "ms": round(ms, 1) if ms is not None else None,
            }
        )


def doing(purpose: str, what: str, tag: str = "", **detail) -> None:
    """Say what this job is doing at the moment, for the debug view.

    Called by the job because only the job knows. The request log can say a
    page was fetched; it cannot say that it was page 14 of 213 of the
    pre-owned slice, read newest-updated first, on the third pass of the day.
    """
    key = f"{purpose}:{tag}" if tag else purpose
    with _lock:
        _doing[key] = {"what": what, "since": time.time(), **detail}


def done(purpose: str, tag: str = "") -> None:
    """This job has stopped; it is doing nothing until it says otherwise."""
    key = f"{purpose}:{tag}" if tag else purpose
    with _lock:
        _doing.pop(key, None)


def debug(purpose: str, tag: str = "") -> dict:
    """Everything worth showing about one job: what now, and what just went.

    A tag narrows it to one of a purpose's several jobs. The catalogue is four
    slices sharing one purpose and one allowance; without this, opening the
    debug view on the pre-owned slice showed whatever slice had run last.
    """
    key = f"{purpose}:{tag}" if tag else purpose
    with _lock:
        current = dict(_doing.get(key) or {})
        trail = [
            entry
            for entry in (_recent.get(purpose) or [])
            if not tag or entry.get("tag") == tag
        ]
    now = time.time()
    if current:
        current["for_seconds"] = round(now - current.get("since", now), 1)
    for entry in trail:
        entry = entry  # already a copy per append
    return {
        "purpose": purpose,
        "tag": tag or None,
        "label": PURPOSE_LABELS.get(purpose, purpose),
        "doing": current or None,
        "recent": [dict(e, ago_seconds=round(now - e["at"], 1)) for e in trail],
    }


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
    for event in sample:
        _, host, why, ok = event[0], event[1], event[2], event[3]
        entry = hosts.setdefault(host, {"total": 0, "errors": 0, "purposes": {}})
        entry["total"] += 1
        if not ok:
            entry["errors"] += 1
        # Counted per purpose as well as in total. A failure count on its own
        # says something is wrong somewhere; the one that says which job is
        # failing is the one worth reading.
        counts = entry["purposes"].setdefault(why, {"requests": 0, "errors": 0})
        counts["requests"] += 1
        if not ok:
            counts["errors"] += 1

    for entry in hosts.values():
        entry["per_minute"] = round(entry["total"] / seconds * 60, 1)
        entry["per_hour"] = round(entry["total"] / seconds * 3600)
        entry["purposes"] = [
            {
                "key": why,
                "label": PURPOSE_LABELS.get(why, why),
                "requests": counts["requests"],
                "errors": counts["errors"],
                "per_minute": round(counts["requests"] / seconds * 60, 1),
                "share": (
                    round(counts["requests"] / entry["total"] * 100, 1)
                    if entry["total"]
                    else 0.0
                ),
            }
            for why, counts in sorted(
                entry["purposes"].items(), key=lambda kv: -kv[1]["requests"]
            )
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
