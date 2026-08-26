"""SMTP delivery.

Slower than push by design, so it is the digest channel. The HTML body is
deliberately table-based and inline-styled because mail clients are hostile to
anything else.
"""
from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage

from ..config import settings
from .base import Notification, Notifier, NotifierError


class EmailNotifier(Notifier):
    type = "email"
    label = "E-mail"
    fields = (
        {
            "name": "to",
            "label": "Recipient",
            "type": "text",
            "required": True,
            "help": "Server settings come from the instance SMTP configuration.",
        },
        {
            "name": "digest_only",
            "label": "Only send digests",
            "type": "boolean",
            "required": False,
            "help": "Skip instant alerts, keep the daily or weekly summary.",
        },
    )

    def _server_config(self, config: dict) -> dict:
        """Per-channel SMTP overrides fall back to the instance settings."""
        return {
            "host": config.get("smtp_host") or settings.smtp_host,
            "port": int(config.get("smtp_port") or settings.smtp_port),
            "user": config.get("smtp_user") or settings.smtp_user,
            "password": config.get("smtp_password") or settings.smtp_password,
            "sender": config.get("smtp_from") or settings.smtp_from or settings.smtp_user,
            "starttls": bool(config.get("smtp_starttls", settings.smtp_starttls)),
            "ssl": bool(config.get("smtp_ssl", settings.smtp_ssl)),
        }

    def send(self, config: dict, notification: Notification) -> None:
        recipient = str(config.get("to", "")).strip()
        if not recipient:
            raise NotifierError("E-mail channel needs a recipient address")

        server = self._server_config(config)
        if not server["host"]:
            raise NotifierError(
                "No SMTP server configured. Set SMTP_HOST on the instance or in this channel."
            )
        if not server["sender"]:
            raise NotifierError("No sender address configured (SMTP_FROM)")

        message = EmailMessage()
        message["Subject"] = notification.title
        message["From"] = server["sender"]
        message["To"] = recipient
        message.set_content(notification.plain_text())
        message.add_alternative(self._render_html(notification), subtype="html")

        try:
            if server["ssl"]:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(server["host"], server["port"], context=context, timeout=20) as smtp:
                    self._deliver(smtp, server, message)
            else:
                with smtplib.SMTP(server["host"], server["port"], timeout=20) as smtp:
                    if server["starttls"]:
                        smtp.starttls(context=ssl.create_default_context())
                    self._deliver(smtp, server, message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifierError(str(exc)) from exc

    @staticmethod
    def _deliver(smtp, server: dict, message: EmailMessage) -> None:
        if server["user"]:
            smtp.login(server["user"], server["password"])
        smtp.send_message(message)

    @staticmethod
    def _render_html(notification: Notification) -> str:
        esc = html.escape
        rows = []
        if notification.price_text:
            rows.append(("Price", notification.price_text))
        if notification.landed_text:
            rows.append(("Estimated total", notification.landed_text))
        rows.extend(notification.fields.items())

        row_html = "".join(
            f'<tr><td style="padding:6px 14px 6px 0;color:#6b7280;font-size:13px">{esc(k)}</td>'
            f'<td style="padding:6px 0;font-weight:600;font-size:14px">{esc(str(v))}</td></tr>'
            for k, v in rows
        )
        image_html = (
            f'<img src="{esc(notification.image_url)}" alt="" width="180" '
            'style="border-radius:12px;display:block;margin-bottom:16px">'
            if notification.image_url
            else ""
        )
        button_html = (
            f'<a href="{esc(notification.url)}" style="display:inline-block;margin-top:20px;'
            "background:#f97316;color:#fff;text-decoration:none;padding:11px 20px;"
            'border-radius:10px;font-weight:600;font-size:14px">Open on shop</a>'
            if notification.url
            else ""
        )
        return f"""<!doctype html>
<html><body style="margin:0;background:#f4f4f5;font-family:-apple-system,Segoe UI,Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:28px 12px">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;padding:28px">
<tr><td>
{image_html}
<h1 style="margin:0 0 6px;font-size:20px;color:#111827">{esc(notification.title)}</h1>
<p style="margin:0 0 18px;color:#4b5563;font-size:15px">{esc(notification.item_name or "")}</p>
<table cellpadding="0" cellspacing="0">{row_html}</table>
{button_html}
<p style="margin:26px 0 0;color:#9ca3af;font-size:12px">Sent by AmiSearch</p>
</td></tr></table></td></tr></table></body></html>"""
