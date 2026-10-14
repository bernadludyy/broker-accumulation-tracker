"""Telegram Bot 1 YTD matrix command shim.

Wraps the existing CLI / repository_dispatch handler, preserving its
authorization, update-id dedupe, SQLite checkpoint and legacy commands.
"""
from __future__ import annotations

import telegram_query_bot as legacy
from broker_ytd_matrix import format_broker_top, format_ticker_top


_HELP = """
🏆 BROKER YTD MATRIX (hanya cache SQLite, 0 extra ZAPI)
BROKER TOP BBRI YTD
→ Top 3 broker Net Buy + Net Sell untuk satu saham.
CC NI TOP YTD
→ Top 3 saham Net Buy oleh broker NI.
CC NI TOP SELL YTD
→ Top 3 saham Net Sell oleh broker NI.
→ Ranking seluruh ticker yang memiliki histori broker tersimpan, bukan seluruh IDX.
→ PARTIAL/UNVERIFIED berarti coverage YTD belum dapat dipastikan.
"""

_original_command_result = legacy.command_result
legacy.HELP_TEXT += "\n" + _HELP


def command_result(text: str, chat_id: str) -> str:
    parts = text.strip().split()
    if not parts:
        return _original_command_result(text, chat_id)

    first = parts[0].split("@")[0].lower()
    if first == "broker" and len(parts) >= 2 and parts[1].upper() == "TOP":
        if len(parts) not in (3, 4) or (
            len(parts) == 4 and parts[3].upper() != "YTD"
        ):
            return "Format: BROKER TOP BBRI YTD"
        return format_ticker_top(parts[2], str(legacy.DB_PATH), legacy.safe_end_date())

    if first == "cc" and len(parts) >= 3 and parts[2].upper() == "TOP":
        options = [p.upper() for p in parts[3:]]
        if (len(options) != len(set(options))
                or any(p not in {"YTD", "BUY", "SELL"} for p in options)
                or ("BUY" in options and "SELL" in options)):
            return "Format: CC NI TOP YTD atau CC NI TOP SELL YTD"
        mode = "SELL" if "SELL" in options else "BUY"
        return format_broker_top(
            parts[1], str(legacy.DB_PATH), legacy.safe_end_date(), mode=mode
        )

    return _original_command_result(text, chat_id)


legacy.command_result = command_result


if __name__ == "__main__":
    raise SystemExit(legacy.main())
