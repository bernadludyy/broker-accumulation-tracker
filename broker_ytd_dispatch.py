"""Telegram Bot 1 YTD matrix command shim.

Wraps the existing CLI / repository_dispatch handler, preserving its
authorization, update-id dedupe, SQLite checkpoint and legacy commands.
"""
from __future__ import annotations

import telegram_query_bot as legacy
from broker_ytd_matrix import format_broker_top, format_ticker_top
from broker_persistence import format_persistence
from broker_stalker import ALLOWED_LOOKBACKS, format_broker_stalker
from market_combos import format_market_combo


_HELP = """
🏆 BROKER YTD MATRIX (hanya cache SQLite, 0 extra ZAPI)
BROKER STALKER
BROKER STALKER 10D 6
→ Saham yang net buy oleh banyak broker; default 5 sesi, maksimal 10 hasil.
MARKET COMBO 2 LIVE
→ Top Value ∩ Top Frequency, lalu konfirmasi bid/offer saat bursa berjalan.
MARKET COMBO 2 EOD
→ Watchlist Value + Frequency dari cache EOD; tampilkan penutupan dan harga rata-rata.
MARKET COMBO 1 LIVE
→ High ATS satu hari + bid/offer live; market cap tidak difilter.
MARKET COMBO 1 EOD
→ Watchlist High ATS dari cache EOD; 0 ZAPI/orderbook.
→ Opsional limit 1–10. Tanpa LIVE/EOD tetap memakai LIVE.
BROKER PERSIST BBRI
→ Persistensi broker 5/10/20 sesi cache, net buy terkini vs YTD, intensitas.
BROKER TOP BBRI YTD
→ Top 3 broker Net Buy + Net Sell untuk satu saham.
CC NI TOP YTD
→ Top 3 saham Net Buy oleh broker NI.
CC NI TOP SELL YTD
→ Top 3 saham Net Sell oleh broker NI.
→ Ranking seluruh ticker yang memiliki histori broker tersimpan, bukan seluruh IDX.
→ Setiap peringkat: Weighted Avg Buy (net buy) / Avg Sell (net sell).
→ Perhitungan avg = total value sisi ÷ (total lot sisi × 100), bukan avg posisi net.
→ PARTIAL/UNVERIFIED berarti coverage YTD belum dapat dipastikan.
"""

_original_command_result = legacy.command_result
legacy.HELP_TEXT += "\n" + _HELP


def command_result(text: str, chat_id: str) -> str:
    parts = text.strip().split()
    if not parts:
        return _original_command_result(text, chat_id)

    first = parts[0].split("@")[0].lower()
    if first == "market" and len(parts) >= 2 and parts[1].upper() == "COMBO":
        format_hint = "Format: MARKET COMBO [1|2] [LIVE|EOD] [1-10]"
        if len(parts) not in {3, 4, 5} or parts[2] not in {"1", "2"}:
            return format_hint
        limit = 5
        mode = "LIVE"
        mode_seen = False
        limit_seen = False
        for option in parts[3:]:
            upper = option.upper()
            if upper in {"LIVE", "EOD"}:
                if mode_seen:
                    return format_hint
                mode = upper
                mode_seen = True
            elif option.isdigit() and 1 <= int(option) <= 10:
                if limit_seen:
                    return format_hint
                limit = int(option)
                limit_seen = True
            else:
                return format_hint
        return format_market_combo(int(parts[2]), limit=limit, mode=mode)

    is_stalker = (
        (first == "broker" and len(parts) >= 2 and parts[1].upper() == "STALKER")
        or first in {"stalker", "/stalker"}
    )
    if is_stalker:
        options = parts[2:] if first == "broker" else parts[1:]
        lookback = 5
        limit = 6
        seen_period = False
        seen_limit = False
        for option in options:
            upper = option.upper()
            if upper.endswith("D") and upper[:-1].isdigit():
                value = int(upper[:-1])
                if seen_period or value not in ALLOWED_LOOKBACKS:
                    return "Format: BROKER STALKER [5D|10D|20D] [1-10]"
                lookback = value
                seen_period = True
            elif option.isdigit():
                value = int(option)
                if seen_limit or not 1 <= value <= 10:
                    return "Format: BROKER STALKER [5D|10D|20D] [1-10]"
                limit = value
                seen_limit = True
            else:
                return "Format: BROKER STALKER [5D|10D|20D] [1-10]"
        return format_broker_stalker(
            str(legacy.DB_PATH),
            legacy.safe_end_date(),
            lookback=lookback,
            limit=limit,
        )

    if first == "broker" and len(parts) >= 2 and parts[1].upper() == "PERSIST":
        if len(parts) != 3:
            return "Format: BROKER PERSIST BBRI"
        return format_persistence(parts[2], str(legacy.DB_PATH), legacy.safe_end_date())

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
