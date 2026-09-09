"""Is the build that is running still the newest one?

The version number said 1.0.0 from the first commit onwards, so nothing on
any screen could answer "am I running what I just pushed?" - and a fix that
had not been deployed was indistinguishable from a fix that had not worked.
That is a bad way to spend an evening, and it happened.

The image now carries the commit it was built from. This asks GitHub what the
newest commit on the branch is and reports the difference. It is deliberately
forgiving: no network, a private repository, a rate limit, a local build with
no commit recorded - all of them mean "cannot say", never an error on screen.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from ..config import settings

log = logging.getLogger(__name__)

#: How long an answer is kept. GitHub allows sixty unauthenticated calls an
#: hour per address, and the question cannot change faster than someone
#: pushes, so asking more often would only spend the allowance.
CACHE_SECONDS = 3600.0

#: Anything slower than this is not worth making a page wait for.
TIMEOUT_SECONDS = 6.0

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


def version_label() -> str:
    """The version to show, which is the day the build was made.

    A number somebody has to remember to raise is a number that stops being
    raised, and this one read 1.0.0 from the first commit to the hundredth.
    The build date cannot go stale by neglect and answers the question the
    number was there for: how old is what I am running.

    A build made outside the workflow has no date and says so instead.
    """
    stamp = (settings.build_time or "").strip()
    if stamp:
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return when.strftime("%Y.%m.%d")
    return settings.app_version


def running() -> dict:
    """What this instance is, as far as it was told at build time."""
    commit = (settings.build_commit or "").strip()
    return {
        "version": version_label(),
        "commit": commit,
        "short_commit": commit[:7] if commit else "",
        "built_at": (settings.build_time or "").strip() or None,
        "ref": (settings.build_ref or "").strip() or None,
        # A build made outside the workflow records no commit, so it cannot be
        # compared with anything and says so rather than guessing.
        "identified": bool(commit),
    }


def _ask_github(repo: str, branch: str) -> dict | None:
    """The newest commit on that branch, or None when it cannot be had."""
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AmiSearch update check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.debug("Update check could not reach GitHub: %s", exc)
        return None

    sha = str(payload.get("sha") or "")
    if not sha:
        return None
    committed = (
        ((payload.get("commit") or {}).get("committer") or {}).get("date")
        or ((payload.get("commit") or {}).get("author") or {}).get("date")
    )
    message = str((payload.get("commit") or {}).get("message") or "").strip()
    return {
        "commit": sha,
        "short_commit": sha[:7],
        "committed_at": committed,
        # The first line only. A commit body here is several paragraphs and
        # the panel wants a label, not an essay.
        "title": message.splitlines()[0] if message else "",
    }


def _behind_by(repo: str, base: str, head: str) -> int | None:
    """How many commits separate the two, when GitHub will say."""
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/compare/{base}...{head}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AmiSearch update check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.debug("Update check could not compare commits: %s", exc)
        return None
    ahead = payload.get("ahead_by")
    return int(ahead) if isinstance(ahead, int) else None


def check(force: bool = False) -> dict:
    """What is running, what is newest, and whether they differ.

    Cached, and never raises. Every way of not knowing - no network, a private
    repository, a rate limit, a build with no commit recorded - comes back as
    ``status: "unknown"`` with a reason, because an update check that breaks
    the page it sits on is worse than no update check.
    """
    global _cached

    now = time.monotonic()
    if not force:
        with _lock:
            if _cached and now - _cached[0] < CACHE_SECONDS:
                return _cached[1]

    here = running()
    repo = (settings.update_check_repo or "").strip()
    branch = here["ref"] or "main"

    if not repo:
        result = {**here, "status": "disabled", "reason": "Update checking is switched off."}
    elif not here["identified"]:
        result = {
            **here,
            "status": "unknown",
            "reason": (
                "This build records no commit, so it cannot be compared. "
                "Images from the publish workflow carry one."
            ),
        }
    else:
        # Guarded as a whole. The two calls below catch what they expect, and
        # the promise this function makes is stronger than that: whatever
        # happens out there, the page that shows this must still render.
        try:
            newest = _ask_github(repo, branch)
            if newest is None:
                result = {
                    **here,
                    "status": "unknown",
                    "reason": "Could not reach GitHub just now.",
                }
            elif newest["commit"].lower() == here["commit"].lower():
                result = {**here, "status": "current", "latest": newest}
            else:
                result = {
                    **here,
                    "status": "behind",
                    "latest": newest,
                    # How far behind is a nicety; not knowing it must not
                    # cost the answer that there is something newer.
                    "behind_by": _behind_by(repo, here["commit"], newest["commit"]),
                }
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.debug("Update check failed: %s", exc)
            result = {
                **here,
                "status": "unknown",
                "reason": "The check did not complete.",
            }

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cached = (now, result)
    return result
