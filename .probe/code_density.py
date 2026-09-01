"""How many figures has AmiAmi ever listed?

The product code carries a number - FIGURE-184067-R is the 184,067th
something - and the ones we hold run from 6 to 611,765 with almost everything
under 210,000. If that range is densely allocated, the highest number is
roughly the count of figures ever registered, and the gap between it and what
we hold is what a complete catalogue would still be missing.

Density is the part the stored data cannot answer: our sample is only what is
currently on sale second-hand, so a number we have never seen might be a
figure we simply do not hold, or might never have been allocated at all.

So this asks the shop directly about numbers picked at random across the
range. A number that answers was allocated; one that does not, was not.

    python .probe/code_density.py
"""
from __future__ import annotations

import os
import pathlib
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "code-density-not-a-real-secret")

from app.providers import ItemNotFound  # noqa: E402
from app.providers.amiami import AmiAmiProvider  # noqa: E402

provider = AmiAmiProvider()

#: Bands to sample, and how many numbers from each. Weighted towards the
#: recent end, where the catalogue we hold is densest and the answer matters
#: most for "how much is missing".
BANDS = [
    (1_000, 50_000, 5),
    (50_000, 100_000, 4),
    (100_000, 150_000, 4),
    (150_000, 190_000, 5),
    (190_000, 210_000, 5),
    (210_000, 260_000, 4),
    (600_000, 615_000, 3),
]


def exists(number: int) -> str | None:
    """Which form of this number the shop knows, if any."""
    for suffix in ("", "-R"):
        code = f"FIGURE-{number:06d}{suffix}"
        try:
            provider.get_item(code)
            return code
        except ItemNotFound:
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"    {code}: {exc}")
            return None
        finally:
            time.sleep(random.uniform(4, 7))
    return None


def main() -> None:
    total_tried = total_found = 0
    print(f"  {'band':>20}{'tried':>8}{'allocated':>11}{'density':>10}")
    print("  " + "-" * 50)
    for low, high, count in BANDS:
        picks = random.sample(range(low, high), count)
        found = sum(1 for n in picks if exists(n))
        total_tried += count
        total_found += found
        print(f"  {low:>8,}-{high:<9,}{count:>8}{found:>11}{found / count * 100:>9.0f}%")

    density = total_found / total_tried if total_tried else 0
    print("  " + "-" * 50)
    print(f"  {'overall':>20}{total_tried:>8}{total_found:>11}{density * 100:>9.0f}%")
    print()
    print("Reading it")
    print("  Near 100% means the numbers are handed out one after another and the")
    print("  highest one is close to the count of figures ever listed. Much lower")
    print("  means the range is shared with other things, and the highest number")
    print("  says nothing much about how many figures there are.")
    print()
    print("  Either way this is a small sample, so treat it as an order of")
    print("  magnitude rather than a figure to quote.")


if __name__ == "__main__":
    main()
