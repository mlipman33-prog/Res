"""Optional push notifications via ntfy.sh (free phone app, no account needed)."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        requests.post(
            f"{server}/{topic}",
            data=message.encode(),
            headers={"Title": title, "Priority": "high"},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("Notification failed: %s", e)
