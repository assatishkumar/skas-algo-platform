"""Telegram notifier."""

from __future__ import annotations

import requests

from .base import Alert


class TelegramNotifier:
    """Sends alerts to a Telegram chat via the Bot API.

    Get a token from @BotFather and your chat id from @userinfobot.
    """

    def __init__(self, bot_token: str, chat_id: str, http: requests.Session | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._http = http or requests.Session()

    def send(self, alert: Alert) -> None:
        """Fire-and-forget: the POST runs on a daemon thread so a slow Telegram call can
        never stall a request or hold a DB transaction open (the 2026-07-07 lock storm —
        the broker-login route notified INSIDE its write transaction)."""
        import threading

        threading.Thread(target=self._post, args=(alert,), daemon=True).start()

    def _post(self, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            self._http.post(
                url,
                json={"chat_id": self.chat_id, "text": alert.as_text()},
                timeout=10,
            )
        except Exception:  # pragma: no cover - alerts are best-effort by design
            pass
