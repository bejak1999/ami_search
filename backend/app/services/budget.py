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
    manual      somebody is sitting there waiting for it
    catalogue   the sweeps that keep the catalogue honest
    shelf       following individual copies, which can wait

"manual" is anything a person set off by clicking: opening an item and
refreshing it, resolving a pasted code, asking the collection what has got
cheaper. It carries a watch's weight because a person waiting is at least as
urgent as an alert, and like everything else it takes nothing at all when
nobody is clicking.

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
from ..providers.ratelimit import TokenBucket

#: How much of the pool each job pulls when several are running at once.
#: Ratios, not rates - what they mean in requests depends on who else is up.
WEIGHTS: dict[str, int] = {
    "watch": 5,
    "manual": 5,
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


#: One gate per purpose, so the share is enforced across threads rather than
#: per thread. The pacers already space out the jobs that have one; this is
#: what holds the jobs that do not.
_gates: dict[str, TokenBucket] = {}

#: Enough to let a short burst through without waiting - a watch poll is a
#: search and sometimes a detail read, and making the second wait behind the
#: metronome would add seconds to every alert for nothing.
GATE_BURST = 5

#: How long a request may wait for its turn before giving up. Long, because
#: this is cooperative spacing and not a fault: twenty-five watches falling
#: due at once should be spread out, not failed. Past this something is
#: genuinely wrong and the caller's error handling is the right answer.
GATE_TIMEOUT_SECONDS = 180.0


def wait_for_turn(purpose: str, timeout: float = GATE_TIMEOUT_SECONDS) -> None:
    """Block until this job's share of the pool allows another request.

    The weights alone only ever slowed down the jobs that consult them. Watch
    polling never did: it runs up to sixteen at a time with no pacer, so the
    only thing holding it was the provider's own ceiling, well above the pool
    everything else was being shared out of. The panel said twenty-four a
    minute was being divided up while one job was free to ignore it.

    Higher priority still means higher priority - watches carry the largest
    weight and so the largest share, and take the whole pool when nothing
    else is running. What changes is that the share is now a limit as well as
    an allocation.
    """
    rate = max(MINIMUM_PER_MINUTE, rate_for(purpose))
    with _lock:
        gate = _gates.get(purpose)
        if gate is None:
            gate = _gates[purpose] = TokenBucket(
                rate_per_minute=int(rate), burst=GATE_BURST
            )
    # Reassigned outside the lock and read by the bucket's own: the pool
    # moves as jobs start and stop, so the gate follows it rather than
    # holding whatever the rate was when it was first built.
    gate.rate_per_minute = rate
    gate.acquire(timeout=timeout)


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
