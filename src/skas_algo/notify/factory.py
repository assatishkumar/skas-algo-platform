"""Build the active notifier from settings."""

from __future__ import annotations

from skas_algo.config import get_settings

from .base import FanOutNotifier, LogNotifier, Notifier
from .telegram import TelegramNotifier


def build_notifier() -> Notifier:
    """Return a notifier with the log sink always on, plus Telegram if configured."""
    settings = get_settings()
    channels: list[Notifier] = [LogNotifier()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    return FanOutNotifier(channels)
