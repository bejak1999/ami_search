"""One request budget for AmiAmi's API, shared out by what matters most.

Every job used to carry its own fixed rate: the catalogue crawler eight a
minute, the shelf-life sampler ten, watches none at all. Fixed rates cannot
lend. The ceiling is forty a minute and at most eighteen of it was ever asked
for, so the sampler crawled along at four a minute averaged over the hour -
four minutes of work in every ten, at ten a minute - while nothing else was
using the other thirty-six.

Here the total is one pool and the jobs draw from it by weight. A job that is
not running takes nothing, and its share flows to whoever is. So the sampler
runs at the full pool rate when it has the shop to itself and steps back when
a watch or a sweep wants it, without anybody choosing numbers in advance.

The order is the order things matter in:

    watch       an alert is the point of the application
    catalogue   the sweeps that keep the catalogue honest
    shelf       following individual copies, which can wait

Photos are not in here. They come from img.amiami.com, a static image host,
and throttling them against the API would slow them down for nothing.
MyFigureCollection is a different site again and keeps its own allowance.

What this does *not* change is how the requests are spaced. The pacer still
draws irregular gaps and takes the occasional long break; this only moves the
rate it is drawing around. A steadier stream would be easier to write and
would look exactly like a robot.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from ..config import settings

#: How much of the pool each job pulls when several are running at once.
#: Ratios, not rates - what they mean in requests depends on who else is up.
WEIGHTS: dict[str, int] = {
    "watch": 5,
    "catalogue": 3,
    "shelf": 2,
}

#: A job that is running gets at least this, however crowded it is. Without a
#: floor a low-weight job on a busy instance would be paced into never
#: finishing anything, which is worse than being slow.
MINIMUM_PER_MINUTE = 1.0

_lock = threading.Lock()
#: Purpose -> how many runs of it are currently in flight. A count rather than
#: a flag because two threads can be inside the same job, and the second one
#: leaving must not mark the first one idle.
_active: dict[str, int] = {}


def total_per_minute() -> float:
    return float(settings.amiami_budget_per_minute)


def rate_for(purpose: str) -> float:
    """The share this job may use right now, in requests per minute.

    With nothing else running it is the whole pool. The point of the exercise
    is that a quiet instance runs at the ceiling rather than at whatever fixed
    number somebody typed into the settings years ago.
    """
    weight = WEIGHTS.get(purpose)
    if weight is None:
        # Something that has not declared itself. It still has to be paced, so
        # it gets the smallest honest share rather than the run of the place.
        return max(MINIMUM_PER_MINUTE, total_per_minute() / len(WEIGHTS))

    with _lock:
        running = {name: WEIGHTS[name] for name in _active if name in WEIGHTS}
        if purpose not in running:
            # Asked before claiming - about to start. Answer as though it had.
            running[purpose] = weight
        share = weight / sum(running.values())

    return max(MINIMUM_PER_MINUTE, total_per_minute() * share)


@contextmanager
def claim(purpose: str) -> Iterator[None]:
    """Mark this job as running for as long as the block lasts."""
    with _lock:
        _active[purpose] = _active.get(purpose, 0) + 1
    try:
        yield
    finally:
        with _lock:
            remaining = _active.get(purpose, 1) - 1
            if remaining > 0:
                _active[purpose] = remaining
            else:
                _active.pop(purpose, None)


def snapshot() -> dict:
    """What the pool is doing, for the admin view."""
    with _lock:
        running = sorted(name for name in _active if name in WEIGHTS)
    total = total_per_minute()
    return {
        "total_per_minute": total,
        "running": running,
        "shares": [
            {
                "purpose": name,
                "weight": weight,
                "per_minute": round(rate_for(name), 1),
                "running": name in running,
            }
            for name, weight in sorted(WEIGHTS.items(), key=lambda kv: -kv[1])
        ],
    }
