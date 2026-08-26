"""Notification registry and dispatcher."""
from __future__ import annotations

import logging

from ..models import ChannelType
from .base import Notification, Notifier, NotifierError
from .email import EmailNotifier
from .simple import DiscordNotifier, GotifyNotifier, NtfyNotifier, WebhookNotifier
from .telegram import TelegramNotifier
from .webpush import SubscriptionGone, WebPushNotifier, generate_vapid_keys

log = logging.getLogger(__name__)

_NOTIFIERS: dict[str, Notifier] = {
    n.type: n
    for n in (
        TelegramNotifier(),
        WebPushNotifier(),
        EmailNotifier(),
        DiscordNotifier(),
        NtfyNotifier(),
        GotifyNotifier(),
        WebhookNotifier(),
    )
}

#: Order the UI presents them in: fastest and most useful first.
CHANNEL_ORDER = ("telegram", "webpush", "ntfy", "discord", "gotify", "email", "webhook")


def get_notifier(channel_type: ChannelType | str) -> Notifier:
    key = channel_type.value if isinstance(channel_type, ChannelType) else str(channel_type)
    try:
        return _NOTIFIERS[key]
    except KeyError:
        raise NotifierError("Unknown notification channel: " + key) from None


def describe_all() -> list[dict]:
    return [_NOTIFIERS[key].describe() for key in CHANNEL_ORDER if key in _NOTIFIERS]


def send(channel_type: ChannelType | str, config: dict, notification: Notification) -> None:
    get_notifier(channel_type).send(config or {}, notification)


__all__ = [
    "CHANNEL_ORDER",
    "Notification",
    "Notifier",
    "NotifierError",
    "SubscriptionGone",
    "TelegramNotifier",
    "describe_all",
    "generate_vapid_keys",
    "get_notifier",
    "send",
]
