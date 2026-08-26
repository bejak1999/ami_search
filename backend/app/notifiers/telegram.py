"""Telegram bot delivery.

Fastest channel by a wide margin, which is the whole point of this app, so it
sends a photo message with the product image when one is available.
"""
from __future__ import annotations

import html

from .base import Notification, Notifier, NotifierError

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier(Notifier):
    type = "telegram"
    label = "Telegram"
    docs_url = "https://core.telegram.org/bots#how-do-i-create-a-bot"
    fields = (
        {
            "name": "bot_token",
            "label": "Bot token",
            "type": "password",
            "required": True,
            "help": "Create a bot with @BotFather and paste the token here.",
        },
        {
            "name": "chat_id",
            "label": "Chat ID",
            "type": "text",
            "required": True,
            "help": "Message your bot once, then use @userinfobot to get your ID.",
        },
        {
            "name": "silent",
            "label": "Send silently",
            "type": "boolean",
            "required": False,
            "help": "Delivers without a notification sound.",
        },
    )

    def _format(self, notification: Notification) -> str:
        parts = [f"<b>{html.escape(notification.title)}</b>"]
        if notification.item_name:
            parts.append(html.escape(notification.item_name))
        if notification.price_text:
            parts.append(f"\n\U0001f4b4 <b>{html.escape(notification.price_text)}</b>")
        if notification.landed_text:
            parts.append(
                f"\U0001f4e6 Est. total: {html.escape(notification.landed_text)}"
            )
        for key, value in notification.fields.items():
            parts.append(f"{html.escape(key)}: {html.escape(str(value))}")
        if notification.body:
            parts.append("\n" + html.escape(notification.body))
        if notification.url:
            parts.append(f'\n<a href="{html.escape(notification.url)}">Open on shop</a>')
        return "\n".join(parts)

    def send(self, config: dict, notification: Notification) -> None:
        token = str(config.get("bot_token", "")).strip()
        chat_id = str(config.get("chat_id", "")).strip()
        if not token or not chat_id:
            raise NotifierError("Telegram needs both a bot token and a chat ID")

        text = self._format(notification)
        silent = bool(config.get("silent")) and not notification.urgent

        if notification.image_url:
            payload = {
                "chat_id": chat_id,
                "photo": notification.image_url,
                "caption": text[:1024],
                "parse_mode": "HTML",
                "disable_notification": silent,
            }
            try:
                self._post(API.format(token=token, method="sendPhoto"), json=payload)
                return
            except NotifierError:
                # Telegram refuses images it cannot fetch; fall back to text
                # rather than losing the alert entirely.
                pass

        payload = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "disable_notification": silent,
        }
        self._post(API.format(token=token, method="sendMessage"), json=payload)

    def resolve_chat_id(self, token: str) -> list[dict]:
        """Read recent updates so the UI can offer a 'detect my chat' button."""
        import httpx

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(API.format(token=token, method="getUpdates"))
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise NotifierError(str(exc)) from exc
        if not payload.get("ok"):
            raise NotifierError(str(payload.get("description") or "Telegram rejected the token"))

        seen: dict[str, dict] = {}
        for update in payload.get("result") or []:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is None:
                continue
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            )
            seen[str(chat["id"])] = {
                "chat_id": str(chat["id"]),
                "name": name or chat.get("username") or "Chat",
                "type": chat.get("type"),
            }
        return list(seen.values())
