"""Webhook-style channels: Discord, ntfy, Gotify and a generic POST."""
from __future__ import annotations

import json
from urllib.parse import urljoin

from .base import Notification, Notifier, NotifierError

TRIGGER_COLORS = {
    "price_below": 0x22C55E,
    "price_drop": 0x22C55E,
    "back_in_stock": 0x3B82F6,
    "restock_preowned": 0x3B82F6,
    "new_match": 0xF59E0B,
    "deal_radar": 0xA855F7,
}


class DiscordNotifier(Notifier):
    type = "discord"
    label = "Discord"
    docs_url = "https://support.discord.com/hc/en-us/articles/228383668"
    fields = (
        {
            "name": "webhook_url",
            "label": "Webhook URL",
            "type": "password",
            "required": True,
            "help": "Server Settings, Integrations, Webhooks, Copy Webhook URL.",
        },
        {"name": "username", "label": "Override bot name", "type": "text", "required": False},
        {"name": "mention", "label": "Mention (e.g. @here)", "type": "text", "required": False},
    )

    def send(self, config: dict, notification: Notification) -> None:
        url = str(config.get("webhook_url", "")).strip()
        if not url:
            raise NotifierError("Discord needs a webhook URL")

        embed: dict = {
            "title": notification.title[:256],
            "color": TRIGGER_COLORS.get(notification.trigger, 0x6366F1),
        }
        if notification.url:
            embed["url"] = notification.url
        if notification.item_name:
            embed["description"] = notification.item_name[:2048]
        if notification.image_url:
            embed["thumbnail"] = {"url": notification.image_url}

        inline_fields = []
        if notification.price_text:
            inline_fields.append({"name": "Price", "value": notification.price_text, "inline": True})
        if notification.landed_text:
            inline_fields.append(
                {"name": "Est. total", "value": notification.landed_text, "inline": True}
            )
        for key, value in notification.fields.items():
            inline_fields.append({"name": key, "value": str(value)[:1024], "inline": True})
        if inline_fields:
            embed["fields"] = inline_fields[:25]
        if notification.shop:
            embed["footer"] = {"text": notification.shop}

        payload: dict = {"embeds": [embed]}
        if config.get("username"):
            payload["username"] = str(config["username"])[:80]
        if config.get("mention"):
            payload["content"] = str(config["mention"])
        self._post(url, json=payload)


class NtfyNotifier(Notifier):
    type = "ntfy"
    label = "ntfy"
    docs_url = "https://docs.ntfy.sh/"
    fields = (
        {
            "name": "server",
            "label": "Server URL",
            "type": "text",
            "required": False,
            "default": "https://ntfy.sh",
            "help": "Point this at your self-hosted ntfy if you run one.",
        },
        {"name": "topic", "label": "Topic", "type": "text", "required": True},
        {"name": "token", "label": "Access token", "type": "password", "required": False},
        {
            "name": "priority",
            "label": "Priority",
            "type": "select",
            "required": False,
            "options": ["min", "low", "default", "high", "urgent"],
            "default": "high",
        },
    )

    def send(self, config: dict, notification: Notification) -> None:
        server = str(config.get("server") or "https://ntfy.sh").rstrip("/")
        topic = str(config.get("topic", "")).strip()
        if not topic:
            raise NotifierError("ntfy needs a topic")

        priority = str(config.get("priority") or "high")
        if notification.urgent:
            priority = "urgent"

        headers = {
            "Title": notification.title.encode("ascii", "ignore").decode() or "AmiSearch",
            "Priority": priority,
            "Tags": notification.trigger,
        }
        if notification.url:
            headers["Click"] = notification.url
            headers["Actions"] = "view, Open shop, " + notification.url
        if notification.image_url:
            headers["Attach"] = notification.image_url
        if config.get("token"):
            headers["Authorization"] = "Bearer " + str(config["token"])

        body = notification.plain_text().encode("utf-8")
        self._post(f"{server}/{topic}", content=body, headers=headers)


class GotifyNotifier(Notifier):
    type = "gotify"
    label = "Gotify"
    docs_url = "https://gotify.net/docs/pushmsg"
    fields = (
        {"name": "server", "label": "Server URL", "type": "text", "required": True},
        {"name": "token", "label": "App token", "type": "password", "required": True},
        {
            "name": "priority",
            "label": "Priority (0-10)",
            "type": "number",
            "required": False,
            "default": 6,
        },
    )

    def send(self, config: dict, notification: Notification) -> None:
        server = str(config.get("server", "")).strip().rstrip("/")
        token = str(config.get("token", "")).strip()
        if not server or not token:
            raise NotifierError("Gotify needs a server URL and an app token")

        priority = int(config.get("priority") or 6)
        if notification.urgent:
            priority = max(priority, 8)

        markdown = notification.plain_text()
        payload = {
            "title": notification.title,
            "message": markdown,
            "priority": priority,
            "extras": {
                "client::display": {"contentType": "text/plain"},
                "client::notification": (
                    {"click": {"url": notification.url}} if notification.url else {}
                ),
            },
        }
        self._post(urljoin(server + "/", "message"), params={"token": token}, json=payload)


class WebhookNotifier(Notifier):
    type = "webhook"
    label = "Generic webhook"
    docs_url = ""
    fields = (
        {"name": "url", "label": "URL", "type": "text", "required": True},
        {
            "name": "headers",
            "label": "Extra headers (JSON)",
            "type": "textarea",
            "required": False,
            "help": 'For example {"Authorization": "Bearer ..."}',
        },
    )

    def send(self, config: dict, notification: Notification) -> None:
        url = str(config.get("url", "")).strip()
        if not url:
            raise NotifierError("Webhook needs a URL")

        headers = {"Content-Type": "application/json"}
        raw_headers = config.get("headers")
        if raw_headers:
            try:
                extra = raw_headers if isinstance(raw_headers, dict) else json.loads(raw_headers)
                headers.update({str(k): str(v) for k, v in extra.items()})
            except (ValueError, AttributeError) as exc:
                raise NotifierError("Extra headers must be valid JSON") from exc

        payload = {
            "title": notification.title,
            "body": notification.body,
            "item": notification.item_name,
            "shop": notification.shop,
            "trigger": notification.trigger,
            "price": notification.price_text,
            "landed_total": notification.landed_text,
            "url": notification.url,
            "image_url": notification.image_url,
            "fields": notification.fields,
        }
        self._post(url, json=payload, headers=headers)
