"""Notifier interface + a log sink and a fan-out notifier."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("skas_algo.alerts")


class AlertLevel(str, enum.Enum):
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class Alert:
    title: str
    message: str = ""
    level: AlertLevel = AlertLevel.INFO

    def as_text(self) -> str:
        emoji = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.SUCCESS: "✅",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.ERROR: "🛑",
        }[self.level]
        body = f"\n{self.message}" if self.message else ""
        return f"{emoji} {self.title}{body}"


@runtime_checkable
class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogNotifier:
    """Always-available sink that writes alerts to the log."""

    def send(self, alert: Alert) -> None:
        level = {
            AlertLevel.INFO: logging.INFO,
            AlertLevel.SUCCESS: logging.INFO,
            AlertLevel.WARNING: logging.WARNING,
            AlertLevel.ERROR: logging.ERROR,
        }[alert.level]
        logger.log(level, "%s | %s", alert.title, alert.message)


class FanOutNotifier:
    """Sends each alert to several channels; one failing channel never blocks others."""

    def __init__(self, channels: list[Notifier]):
        self.channels = channels

    def send(self, alert: Alert) -> None:
        for ch in self.channels:
            try:
                ch.send(alert)
            except Exception:  # pragma: no cover - defensive
                logger.exception("notifier channel failed: %s", type(ch).__name__)
