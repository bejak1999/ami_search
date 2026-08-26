"""Notification channel interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TIMEOUT = 15.0


@dataclass(slots=True)
class Notification:
    """One thing worth telling the user about."""

    title: str
    body: str = ""
    url: str | None = None
    image_url: str | None = None
    price_text: str | None = None
    landed_text: str | None = None
    trigger: str = "new_match"
    item_name: str | None = None
    shop: str | None = None
    urgent: bool = False
    fields: dict[str, str] = field(default_factory=dict)

    def plain_text(self) -> str:
        lines = [self.title]
        if self.body:
            lines.append(self.body)
        if self.price_text:
            lines.append("Price: " + self.price_text)
        if self.landed_text:
            lines.append("Est. total: " + self.landed_text)
        for key, value in self.fields.items():
            lines.append(f"{key}: {value}")
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)


class NotifierError(RuntimeError):
    """Delivery failed. The message is shown to the user in the UI."""


class Notifier(ABC):
    type: str = "base"
    label: str = "Base"
    #: Field descriptors the UI renders as a config form.
    fields: tuple[dict[str, Any], ...] = ()
    docs_url: str = ""

    @abstractmethod
    def send(self, config: dict, notification: Notification) -> None:
        """Deliver, or raise NotifierError."""

    def validate(self, config: dict) -> None:
        for spec in self.fields:
            if spec.get("required") and not str(config.get(spec["name"], "")).strip():
                raise NotifierError(f"{spec.get('label', spec['name'])} is required")

    @staticmethod
    def _post(url: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
                response = client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise NotifierError(str(exc)) from exc
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise NotifierError(f"HTTP {response.status_code}: {detail}")
        return response

    def describe(self) -> dict:
        return {
            "type": self.type,
            "label": self.label,
            "fields": list(self.fields),
            "docs_url": self.docs_url,
        }
