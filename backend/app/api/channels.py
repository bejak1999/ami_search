"""Notification channel CRUD, test sends and Web Push subscription."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..models import ChannelType, NotificationChannel, User
from ..notifiers import NotifierError, TelegramNotifier, describe_all, get_notifier
from ..schemas import ChannelCreate, ChannelOut, ChannelUpdate, MessageResponse
from ..services import digest, notify

router = APIRouter(prefix="/channels", tags=["notifications"])

#: Never echo these back to the browser once stored.
SECRET_KEYS = {"bot_token", "token", "webhook_url", "smtp_password", "subscription"}


def _preview(config: dict) -> dict:
    """Redact secrets but keep enough for the UI to show what is configured."""
    out: dict = {}
    for key, value in (config or {}).items():
        if key in SECRET_KEYS:
            text = str(value)
            out[key] = ("..." + text[-4:]) if len(text) > 8 else "set"
        else:
            out[key] = value
    return out


def _serialize(channel: NotificationChannel) -> ChannelOut:
    payload = ChannelOut.model_validate(channel)
    payload.config_preview = _preview(channel.config or {})
    return payload


def _get_or_404(db: Session, channel_id: int, user: User) -> NotificationChannel:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None or channel.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return channel


@router.get("/types", response_model=list[dict])
def channel_types() -> list[dict]:
    """Field descriptors so the UI can render a form per channel type."""
    types = describe_all()
    for entry in types:
        if entry["type"] == "webpush":
            entry["available"] = bool(settings.vapid_public_key)
            entry["unavailable_reason"] = (
                "" if settings.vapid_public_key else "VAPID keys are not configured"
            )
        elif entry["type"] == "email":
            entry["available"] = bool(settings.smtp_host)
            entry["unavailable_reason"] = (
                "" if settings.smtp_host else "No SMTP server configured on this instance"
            )
        else:
            entry["available"] = True
            entry["unavailable_reason"] = ""
    return types


@router.get("", response_model=list[ChannelOut])
def list_channels(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[ChannelOut]:
    rows = db.execute(
        select(NotificationChannel)
        .where(NotificationChannel.user_id == user.id)
        .order_by(NotificationChannel.created_at)
    ).scalars()
    return [_serialize(c) for c in rows]


@router.post("", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelCreate, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> ChannelOut:
    notifier = get_notifier(payload.type)
    try:
        notifier.validate(payload.config)
    except NotifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    channel = NotificationChannel(
        user_id=user.id,
        type=payload.type,
        name=payload.name.strip() or notifier.label,
        config=payload.config,
        enabled=payload.enabled,
        is_default=payload.is_default,
        send_digest=payload.send_digest,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _serialize(channel)


@router.patch("/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: int,
    payload: ChannelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ChannelOut:
    channel = _get_or_404(db, channel_id, user)
    data = payload.model_dump(exclude_unset=True)

    if "config" in data and data["config"] is not None:
        # A blank secret means "keep the stored one" rather than "erase it",
        # so the edit form never has to round-trip a token to the browser.
        merged = dict(channel.config or {})
        for key, value in data.pop("config").items():
            if key in SECRET_KEYS and value in ("", None):
                continue
            merged[key] = value
        channel.config = merged
        try:
            get_notifier(channel.type).validate(merged)
        except NotifierError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    for key, value in data.items():
        setattr(channel, key, value)
    if data.get("enabled"):
        channel.failure_count = 0
        channel.last_error = None
    db.commit()
    db.refresh(channel)
    return _serialize(channel)


@router.delete("/{channel_id}", response_model=MessageResponse)
def delete_channel(
    channel_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    channel = _get_or_404(db, channel_id, user)
    db.delete(channel)
    db.commit()
    return MessageResponse(message="Channel deleted")


@router.post("/{channel_id}/test", response_model=MessageResponse)
def test_channel(
    channel_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    channel = _get_or_404(db, channel_id, user)
    try:
        notify.send_test(db, channel)
    except NotifierError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return MessageResponse(message="Test notification sent")


@router.post("/telegram/detect", response_model=MessageResponse)
def detect_telegram_chat(
    payload: dict, _user: User = Depends(current_user)
) -> MessageResponse:
    """Resolve chat IDs from a bot token so nobody has to hunt for theirs."""
    token = str(payload.get("bot_token", "")).strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bot token required")
    try:
        chats = TelegramNotifier().resolve_chat_id(token)
    except NotifierError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not chats:
        return MessageResponse(
            ok=False,
            message="No chats found. Send your bot a message first, then try again.",
            detail=[],
        )
    return MessageResponse(message=f"Found {len(chats)} chat(s)", detail=chats)


@router.post("/webpush/subscribe", response_model=ChannelOut)
def subscribe_webpush(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> ChannelOut:
    """Store a browser push subscription, one channel per device."""
    if not settings.vapid_public_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Web Push is not configured on this instance",
        )
    subscription = payload.get("subscription")
    if not isinstance(subscription, dict) or not subscription.get("endpoint"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid push subscription"
        )

    endpoint = subscription["endpoint"]
    existing = None
    for channel in db.execute(
        select(NotificationChannel).where(
            NotificationChannel.user_id == user.id,
            NotificationChannel.type == ChannelType.webpush,
        )
    ).scalars():
        if (channel.config or {}).get("subscription", {}).get("endpoint") == endpoint:
            existing = channel
            break

    device = str(payload.get("device") or "This browser")[:120]
    if existing is not None:
        existing.config = {"subscription": subscription, "device": device}
        existing.enabled = True
        existing.last_error = None
        existing.failure_count = 0
        db.commit()
        db.refresh(existing)
        return _serialize(existing)

    channel = NotificationChannel(
        user_id=user.id,
        type=ChannelType.webpush,
        name=device,
        config={"subscription": subscription, "device": device},
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _serialize(channel)


@router.post("/digest/send", response_model=MessageResponse)
def send_digest_now(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    sent = digest.send_digest(db, user, force=True)
    if not sent:
        return MessageResponse(
            ok=False,
            message="No channel has digests enabled. Turn 'Send digests' on for a channel first.",
        )
    return MessageResponse(message="Digest sent")
