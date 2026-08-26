"""Human-like request pacing.

A crawler that fires exactly every six seconds is trivially recognisable, and
the regularity is what gets noticed long before the volume does. This module
turns a target rate into a sequence of irregular delays that looks like
somebody browsing:

* delays are drawn from a log-normal distribution, so most are near the
  target and a few are much longer, which is how human gaps actually fall
* every so often the pacer takes a long break, the way a person wanders off
* overnight it slows down, because a catalogue being read at a steady clip at
  four in the morning is its own signal

None of this is a licence to hammer anything. It shapes a rate that is
already deliberately low.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HumanPacer:
    """Turns a requests-per-minute target into irregular, human-ish delays."""

    requests_per_minute: float = 10.0
    #: Spread of the log-normal draw. Higher means more varied gaps.
    sigma: float = 0.55
    #: Chance that any given gap becomes a long break instead.
    break_probability: float = 0.07
    #: How much longer a break is than a normal gap.
    break_multiplier: tuple[float, float] = (6.0, 18.0)
    #: Local hours during which the pace is deliberately slower.
    quiet_hours: tuple[int, int] = (1, 7)
    quiet_slowdown: float = 2.5
    #: Never go below this, whatever the maths says.
    minimum_delay: float = 0.8

    _rng: random.Random = field(default_factory=random.Random, repr=False)
    #: Purely for the admin view.
    delays_generated: int = 0
    breaks_taken: int = 0
    total_sleep: float = 0.0

    @property
    def mean_delay(self) -> float:
        """Average gap needed to actually achieve the configured rate.

        The long breaks land on top of the ordinary gaps, so the ordinary gaps
        have to be correspondingly shorter or the real rate comes out well
        below what was asked for. Getting this wrong would also make every
        completion estimate in the admin view optimistic.
        """
        target = 60.0 / max(0.1, self.requests_per_minute)
        low, high = self.break_multiplier
        expected_break = (low + high) / 2.0
        inflation = (1.0 - self.break_probability) + self.break_probability * expected_break
        return target / inflation

    def _diurnal_factor(self, now: datetime | None = None) -> float:
        hour = (now or datetime.now()).hour
        start, end = self.quiet_hours
        inside = start <= hour < end if start <= end else (hour >= start or hour < end)
        return self.quiet_slowdown if inside else 1.0

    def next_delay(self, now: datetime | None = None) -> float:
        """Draw the next gap, in seconds."""
        mean = self.mean_delay * self._diurnal_factor(now)

        # Pick mu so the log-normal's mean lands on the target rather than its
        # median, otherwise the effective rate drifts below what was asked for.
        mu = math.log(mean) - (self.sigma**2) / 2.0
        delay = self._rng.lognormvariate(mu, self.sigma)

        if self._rng.random() < self.break_probability:
            delay *= self._rng.uniform(*self.break_multiplier)
            self.breaks_taken += 1

        self.delays_generated += 1
        return max(self.minimum_delay, delay)

    def sleep(self, now: datetime | None = None) -> float:
        delay = self.next_delay(now)
        self.total_sleep += delay
        time.sleep(delay)
        return delay

    def stats(self) -> dict:
        return {
            "requests_per_minute": self.requests_per_minute,
            "mean_delay_seconds": round(self.mean_delay, 2),
            "delays_generated": self.delays_generated,
            "breaks_taken": self.breaks_taken,
            "total_sleep_seconds": round(self.total_sleep, 1),
            "currently_slowed": self._diurnal_factor() > 1.0,
        }
