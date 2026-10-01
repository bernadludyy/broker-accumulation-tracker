"""Bounded read retries and plain-text Telegram delivery helpers."""
from __future__ import annotations

import time
import requests


def get_json(url: str, *, headers: dict, params: dict, timeout: int = 30):
    # Only GET requests are retried; never blindly replay message sends.
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            delay = 2 ** attempt
            try:
                delay = min(30, max(delay, float(response.headers.get("Retry-After", delay))))
            except (TypeError, ValueError):
                pass
            response.close()
            time.sleep(delay)
            continue
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and (result.get("success") is False or result.get("ok") is False):
            raise ValueError("Upstream API returned an unsuccessful response.")
        return result.get("data", result) if isinstance(result, dict) else result


def split_message(text: str, max_chars: int = 3500) -> list[str]:
    """Limit UTF-16 units, including emoji and lines longer than the limit."""
    if max_chars < 2:
        raise ValueError("max_chars must be at least 2")
    chunks, current, units = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > max_chars:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


def send_message(token: str, chat_id: str, text: str) -> dict:
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum tersedia.")
    chunks = split_message(text)
    if not chunks:
        raise ValueError("Pesan Telegram kosong.")
    result = {}
    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        # Do not expose the token-bearing request URL through HTTPError.
        if not response.ok:
            raise RuntimeError(f"Telegram delivery failed (HTTP {response.status_code}).")
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram did not confirm message delivery.")
    return result
