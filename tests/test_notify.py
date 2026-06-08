"""Notifier tests: Telegram payload + fan-out resilience."""

from __future__ import annotations

from skas_algo.notify import Alert, AlertLevel, TelegramNotifier
from skas_algo.notify.base import FanOutNotifier, LogNotifier


class _FakeHttp:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))


def test_telegram_sends_formatted_message():
    http = _FakeHttp()
    notifier = TelegramNotifier("TOKEN", "12345", http=http)
    notifier.send(Alert("Order filled", "RELIANCE x10", AlertLevel.SUCCESS))

    assert len(http.posts) == 1
    url, body = http.posts[0]
    assert "botTOKEN/sendMessage" in url
    assert body["chat_id"] == "12345"
    assert "Order filled" in body["text"]
    assert "✅" in body["text"]


def test_fanout_isolates_failing_channel():
    class Boom:
        def send(self, alert):
            raise RuntimeError("down")

    http = _FakeHttp()
    good = TelegramNotifier("T", "C", http=http)
    fan = FanOutNotifier([Boom(), good, LogNotifier()])
    fan.send(Alert("still delivered"))  # must not raise
    assert len(http.posts) == 1
