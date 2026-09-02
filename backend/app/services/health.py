"""Telling you when the background machinery breaks.

A tracker that silently stops working is worse than one that never worked,
because you carry on believing you are covered. The failures that matter are
quiet by nature: a shop starts refusing requests, a MyFigureCollection session
expires, a notification channel stops accepting messages. None of them produce
a visible error unless somebody happens to open the admin page.

So the instance watches itself and tells its administrators through the very
channels it already has. Each condition alerts once when it starts and once
when it clears, never repeatedly, because an alarm that repeats is an alarm
people learn to ignore.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppSetting, CatalogCrawl, NotificationChannel, User, UserRole, Watch
from ..notifiers import Notification, NotifierError, send

log = logging.getLogger(__name__)

STATE_KEY = "health_state"

#: Don't re-announce the same problem for this long, even if it persists.
REMINDER_AFTER = timedelta(hours=12)


@dataclass(slots=True)
class Issue:
    key: str
    title: str
    detail: str
    hint: str = ""
    urgent: bool = False

    def as_notification(self, resolved: bool = False) -> Notification:
        if resolved:
            return Notification(
                title="\u2705 Resolved: " + self.title,
                body="This is working again.",
                trigger="health",
                shop="AmiSearch",
            )
        body = self.detail
        if self.hint:
            body += "\n\n" + self.hint
        return Notification(
            title="\u26a0\ufe0f " + self.title,
            body=body,
            trigger="health",
            shop="AmiSearch",
            url=settings.base_url.rstrip("/") + "/admin",
            urgent=self.urgent,
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def collect_issues(db: Session) -> list[Issue]:
    """Everything currently wrong that a person would want to know about."""
    from ..enrichment.mfc import client as mfc_client
    from ..providers import all_providers

    issues: list[Issue] = []

    # A shop refusing requests stops every watch, so this is the loud one.
    for provider in all_providers():
        circuit = provider.breaker.snapshot()
        if circuit.get("open"):
            issues.append(
                Issue(
                    key=f"provider:{provider.id}",
                    title=f"{provider.name} is refusing requests",
                    detail=(
                        f"{circuit.get('failures', 0)} consecutive failures, backing off for "
                        f"{circuit.get('backoff_seconds', 0):.0f}s. No watch can check while this lasts."
                    ),
                    hint="Usually a temporary block. If it persists, lower PROVIDER_REQUESTS_PER_MINUTE.",
                    urgent=True,
                )
            )

    # Watches that keep failing are individually quiet but collectively fatal.
    broken = list(
        db.execute(
            select(Watch).where(
                Watch.enabled.is_(True),
                Watch.consecutive_errors >= 5,
                # A watch that has worked before is not broken; it is watching
                # something that is not on sale today, which is the whole
                # point of it. Only one that has never once found anything is
                # worth a person's attention - that is a bad code or a search
                # that matches nothing, and it will not fix itself.
                Watch.last_success_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    if broken:
        names = ", ".join((w.label or w.query or str(w.id))[:40] for w in broken[:5])
        issues.append(
            Issue(
                key="watches:failing",
                title=f"{len(broken)} watch(es) keep failing",
                detail=f"Failing repeatedly: {names}"
                + (f" and {len(broken) - 5} more" if len(broken) > 5 else ""),
                hint="Open the watch to see the error. A search that no longer matches anything looks like this too.",
            )
        )

    # An expired MyFigureCollection session degrades quietly: entries simply
    # stop being tagged, and nothing else looks wrong.
    if mfc_client.authenticated:
        state = mfc_client.check_session()
        if not state.get("valid"):
            issues.append(
                Issue(
                    key="mfc:session",
                    title="The MyFigureCollection session stopped working",
                    detail=state.get("detail", "The stored cookie is no longer accepted."),
                    hint="Sign in again and paste a fresh PHPSESSID under Administration.",
                )
            )
        elif state.get("restricted_entries_visible") is False:
            issues.append(
                Issue(
                    key="mfc:adult",
                    title="Restricted MyFigureCollection entries are hidden",
                    detail="The session works, but the account cannot see restricted entries.",
                    hint="Enable adult content under Settings, Account, Content on MyFigureCollection.",
                )
            )

    # A crawl slice that has given up stops the catalogue growing.
    stalled = list(
        db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.enabled.is_(True), CatalogCrawl.consecutive_errors >= 5
            )
        )
        .scalars()
        .all()
    )
    if stalled:
        issues.append(
            Issue(
                key="crawler:stalled",
                title=f"{len(stalled)} catalogue slice(s) stalled",
                detail=", ".join(f"{c.label or c.scope}: {c.last_error or 'unknown'}"[:90] for c in stalled[:3]),
                hint="The catalogue stops growing until this clears. Rewind the slice in Administration to retry.",
            )
        )

    # A job that stops without failing says nothing at all, which is worse
    # than one that fails loudly. A slice whose last run is many intervals
    # behind has not errored - it would be above if it had - so nothing else
    # in here notices, and the panel goes on showing its last known progress
    # as though it were current. That is exactly how a crawler that died at
    # a quarter to seven one morning went unremarked until someone happened
    # to look at the request rate.
    from .crawler import resting_after_errors

    now = datetime.now(timezone.utc)
    quiet = []
    for crawl in db.execute(
        select(CatalogCrawl).where(CatalogCrawl.enabled.is_(True))
    ).scalars():
        if resting_after_errors(crawl) or crawl.consecutive_errors >= 5:
            continue  # already reported above, or deliberately backing off
        last = _aware(crawl.last_run_at)
        if last is None:
            continue  # never started; the first run has not come round yet
        # Generous, so a slice that merely waited its turn behind three
        # others is not reported: this is for silence, not for slowness.
        overdue = timedelta(minutes=max(30, crawl.recheck_interval_minutes or 30) * 6)
        if now - last > overdue:
            quiet.append((crawl, now - last))
    if quiet:
        worst = max(quiet, key=lambda pair: pair[1])
        issues.append(
            Issue(
                key="crawler:quiet",
                title=f"{len(quiet)} catalogue slice(s) have gone quiet",
                detail=(
                    f"{worst[0].label or worst[0].scope} last ran "
                    f"{int(worst[1].total_seconds() // 3600)}h ago, with no error recorded. "
                    "A job that stops without failing looks exactly like one that is idle."
                ),
                hint="Check the scheduler is running. Restarting the container starts every slice again.",
                urgent=True,
            )
        )

    # A channel that keeps rejecting messages is the worst failure of all: the
    # alerts are being raised and thrown away.
    dead = list(
        db.execute(
            select(NotificationChannel).where(
                NotificationChannel.enabled.is_(True), NotificationChannel.failure_count >= 3
            )
        )
        .scalars()
        .all()
    )
    for channel in dead:
        issues.append(
            Issue(
                key=f"channel:{channel.id}",
                title=f"Notifications to {channel.name or channel.type.value} are failing",
                detail=(channel.last_error or "Repeated delivery failures.")[:300],
                hint="Alerts are still being recorded, but they are not reaching you on this channel.",
                urgent=True,
            )
        )

    # Exchange rates going stale quietly skews every landed price.
    from . import fx

    age = fx.rates_age(db)
    if age is None:
        # Worse than stale, and the one case this used to pass over in
        # silence: with no rate at all every conversion returns nothing, so
        # the landed price simply is not there. Quietly missing prices look
        # like a display quirk rather than a broken instance.
        issues.append(
            Issue(
                key="fx:missing",
                title="No exchange rates have been fetched",
                detail=(
                    "Nothing can be converted out of yen, so no landed price can be "
                    "shown and no watch set in another currency can match."
                ),
                hint="Both rate sources were unreachable on start-up. Check the instance's outbound connectivity.",
                urgent=True,
            )
        )
    elif age > timedelta(hours=max(24, settings.fx_refresh_hours * 4)):
        issues.append(
            Issue(
                key="fx:stale",
                title="Exchange rates are stale",
                detail=f"Last refreshed {age.days} day(s) ago. Landed prices are drifting.",
                hint="Both rate sources are unreachable. Check the instance's outbound connectivity.",
            )
        )

    return issues


def _load_state(db: Session) -> dict:
    row = db.get(AppSetting, STATE_KEY)
    return dict(row.value or {}) if row else {}


def _save_state(db: Session, state: dict) -> None:
    row = db.get(AppSetting, STATE_KEY)
    if row is None:
        row = AppSetting(key=STATE_KEY, value={})
        db.add(row)
    row.value = state
    db.commit()


def _recipients(db: Session) -> list[tuple[User, list[NotificationChannel]]]:
    """Administrators and the channels they can actually be reached on.

    Health problems go to administrators rather than everyone: they are about
    the instance, not about anybody's figures.
    """
    admins = list(
        db.execute(
            select(User).where(User.role == UserRole.admin, User.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    out = []
    for admin in admins:
        channels = list(
            db.execute(
                select(NotificationChannel).where(
                    NotificationChannel.user_id == admin.id,
                    NotificationChannel.enabled.is_(True),
                    # A channel that is itself broken cannot carry the news
                    # that it is broken, so skip the ones already failing.
                    NotificationChannel.failure_count < 3,
                )
            )
            .scalars()
            .all()
        )
        if channels:
            out.append((admin, channels))
    return out


def _deliver(db: Session, issue: Issue, resolved: bool) -> int:
    notification = issue.as_notification(resolved=resolved)
    delivered = 0
    for _admin, channels in _recipients(db):
        for channel in channels:
            try:
                send(channel.type, channel.config or {}, notification)
                delivered += 1
            except NotifierError as exc:
                log.warning("Health alert to channel %s failed: %s", channel.id, exc)
            except Exception:  # noqa: BLE001
                log.exception("Health alert to channel %s crashed", channel.id)
    return delivered


def check(db: Session, notify_enabled: bool = True) -> dict:
    """Compare what is wrong now with what was wrong last time, and report."""
    now = datetime.now(timezone.utc)
    current = {issue.key: issue for issue in collect_issues(db)}
    state = _load_state(db)

    announced: list[str] = []
    resolved: list[str] = []

    for key, issue in current.items():
        previous = state.get(key)
        last_sent = _aware(datetime.fromisoformat(previous["notified_at"])) if previous else None
        due = last_sent is None or now - last_sent >= REMINDER_AFTER
        if due:
            if notify_enabled and _deliver(db, issue, resolved=False):
                announced.append(key)
            state[key] = {
                "notified_at": now.isoformat(),
                "title": issue.title,
                "detail": issue.detail,
            }
        elif previous:
            state[key] = {**previous, "title": issue.title, "detail": issue.detail}

    for key in list(state):
        if key in current:
            continue
        issue = Issue(key=key, title=state[key].get("title", key), detail="")
        if notify_enabled:
            _deliver(db, issue, resolved=True)
        resolved.append(key)
        state.pop(key, None)

    _save_state(db, state)

    if announced or resolved:
        log.info("Health: %s new issue(s), %s resolved", len(announced), len(resolved))

    return {
        "issues": [
            {"key": i.key, "title": i.title, "detail": i.detail, "hint": i.hint, "urgent": i.urgent}
            for i in current.values()
        ],
        "healthy": not current,
        "announced": announced,
        "resolved": resolved,
        "checked_at": now,
    }
