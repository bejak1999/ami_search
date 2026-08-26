"""Browser Web Push (VAPID).

The service worker shows the notification even when the tab is closed, which
makes this the best desktop channel. Subscriptions that come back 404 or 410
are dead and the caller is told to delete them.
"""
from __future__ import annotations

import json
import logging

from pywebpush import WebPushException, webpush

from ..config import settings
from .base import Notification, Notifier, NotifierError

log = logging.getLogger(__name__)


class SubscriptionGone(NotifierError):
    """The browser dropped this subscription; remove it from the database."""


class WebPushNotifier(Notifier):
    type = "webpush"
    label = "Browser push"
    fields = (
        {
            "name": "subscription",
            "label": "Push subscription",
            "type": "hidden",
            "required": True,
            "help": "Filled in automatically when you allow notifications.",
        },
        {"name": "device", "label": "Device name", "type": "text", "required": False},
    )

    def send(self, config: dict, notification: Notification) -> None:
        if not settings.vapid_private_key:
            raise NotifierError(
                "No VAPID keys configured. Generate them once and set "
                "VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY."
            )
        subscription = config.get("subscription")
        if isinstance(subscription, str):
            try:
                subscription = json.loads(subscription)
            except ValueError as exc:
                raise NotifierError("Stored push subscription is not valid JSON") from exc
        if not subscription or not subscription.get("endpoint"):
            raise NotifierError("This browser is not subscribed to push notifications")

        payload = {
            "title": notification.title,
            "body": notification.item_name or notification.body,
            "url": notification.url,
            "image": notification.image_url,
            "icon": "/icons/icon-192.png",
            "badge": "/icons/badge-72.png",
            "tag": notification.trigger,
            "requireInteraction": notification.urgent,
            "price": notification.price_text,
            "landed": notification.landed_text,
        }
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
                ttl=600,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                raise SubscriptionGone("Browser subscription expired") from exc
            raise NotifierError(str(exc)) from exc


def generate_vapid_keys() -> dict[str, str]:
    """Create a fresh VAPID keypair for first-run setup."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    return {
        "public_key": b64(public_raw),
        "private_key": private_pem,
        "private_key_der": b64(private_der),
    }
