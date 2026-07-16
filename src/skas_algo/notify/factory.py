"""Build the active notifier from settings."""

from __future__ import annotations

from skas_algo.config import get_settings

from .base import FanOutNotifier, LogNotifier, Notifier
from .in_app import InAppNotifier
from .telegram import TelegramNotifier


def build_notifier() -> Notifier:
    """Return a notifier with the log + in-app sinks always on, plus Telegram if configured.
    The in-app sink persists to the ``alert`` table (the mobile app's Alerts screen)."""
    settings = get_settings()
    channels: list[Notifier] = [LogNotifier(), InAppNotifier()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    return FanOutNotifier(channels)
