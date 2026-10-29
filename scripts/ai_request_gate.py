"""Authorize AI dependency installation before processing a Telegram query.

This script only prints a GitHub Actions step output: remote=true|false.
It never calls an API or prints secrets, chat IDs, or message text.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from ai_decision_engine.telegram import parse_ai_command, remote_enabled
from ai_decision_engine.__main__ import ticker_code


def should_install(*, text: str, chat_id: str, private_chat_id: str) -> bool:
    if not private_chat_id or chat_id != private_chat_id:
        return False
    if not remote_enabled() or not os.getenv("OPENAI_API_KEY"):
        return False
    request = parse_ai_command(text)
    if request is None or request.mode != "RUN":
        return False
    try:
        ticker_code(request.ticker or "")
    except ValueError:
        return False
    return True


def _extract() -> tuple[str, str]:
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return (
            os.getenv("MANUAL_TEXT", ""),
            os.getenv("MANUAL_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", ""),
        )
    path = os.getenv("GITHUB_EVENT_PATH", "")
    if not path:
        return "", ""
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    update = (event.get("client_payload") or {}).get("telegram_update") or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    return str(message.get("text") or ""), str(chat.get("id") or "")


def main() -> int:
    try:
        text, chat = _extract()
        active = should_install(
            text=text, chat_id=chat,
            private_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )
    except (OSError, ValueError, TypeError, AttributeError):
        active = False
    print("remote=" + ("true" if active else "false"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
