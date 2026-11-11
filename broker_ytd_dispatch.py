"""Telegram Bot 1 YTD matrix command shim.

Wraps the existing CLI / repository_dispatch handler, preserving its
authorization, update-id dedupe, SQLite checkpoint and legacy commands.
"""
from __future__ import annotations

import re
import telegram_query_bot as legacy
from broker_ytd_matrix import format_broker_top, format_ticker_top
from broker_persistence import format_persistence
from broker_stalker import ALLOWED_LOOKBACKS, format_broker_stalker
from bandar_momentum import format_bandar_momentum
from bull_divergence import format_bull_divergence
from market_combos import format_market_combo
from quality_trend import format_quality_trend
from silent_accum import format_silent_accum
from ai_decision_engine.telegram import format_ai_command, parse_ai_command
from project_fabric.insider_intelligence import format_insider_report
from orderflow_tape import ALIASES as TAPE_ALIASES, HELP as TAPE_HELP, TapeReport, build_report as build_tape_report, send_photo


_SCREENER_HELP = """📊 SCREENER TEKNIKAL & FLOW
SILENT ACCUM | SILENT ACCUM 10 | SILENT ACCUM BBRI
→ Area low 60D + value sepi + broker yang sama net buy ≥3/5 sesi; alias /silent_accum, 0 ZAPI.

BANDAR MOMENTUM | BANDAR MOMENTUM EOD 10
→ Broker + Value + EMA20 + RS + RSI + return; EOD default dan 0 ZAPI.
BANDAR MOMENTUM LIVE 10
→ Harga/value berjalan memakai 1 snapshot ZAPI; alias /bandar_momentum.

BULL DIV | BULL DIV 10 | BULL DIV BBRI
→ Bullish divergence harga vs RSI14 dari pivot EOD Neon; alias /bulldiv, 0 ZAPI.

RATIO TREND | RATIO TREND 10 | RATIO TREND BBRI
→ Quality-ratio + MA + RSI/MACD/volume/foreign/R:R; alias QUALITY TREND dan /ratiotrend.

MARKET COMBO 1 LIVE | MARKET COMBO 1 EOD
MARKET COMBO 2 LIVE | MARKET COMBO 2 EOD
→ Format umum: MARKET COMBO [1|2] [LIVE|EOD] [1-10].
→ COMBO 1: High ATS; COMBO 2: Top Value ∩ Top Frequency.
→ LIVE konfirmasi bid/offer; EOD memakai cache tanpa orderbook."""

_BROKER_HELP = """🏆 BROKER INTELLIGENCE & YTD MATRIX
BROKER STALKER | BROKER STALKER 10D 6
→ Saham yang net buy oleh banyak broker; default 5 sesi, maks. 10 hasil.

BROKER PERSIST BBRI
→ Persistensi broker 5/10/20 sesi, net buy terkini vs YTD, dan intensitas.

BROKER TOP BBRI YTD
→ Top 3 broker Net Buy + Net Sell untuk satu saham.
CC NI TOP YTD | CC NI TOP SELL YTD
→ Top 3 saham Net Buy/Net Sell oleh broker NI.
→ Ranking memakai histori cache; PARTIAL/UNVERIFIED berarti coverage belum pasti.
→ Avg = total value sisi ÷ (total lot sisi × 100), bukan average posisi net."""

_DATA_HELP = """🗂 INSIDER DISCLOSURE
INSIDER BBRI
→ Disclosure Direksi/Komisaris/Pengendali resmi dari cache Neon; 0 ZAPI.
INSIDER REFRESH BBRI
→ Owner-only, maks. 1 ZAPI lalu simpan ke Neon.
→ Data tertunda; tidak disamakan dengan broker/order flow live."""

_ANALYSIS_HELP = """🧠 BOT1 AI DECISION (Neon EOD; repair histori maks. 1 ZAPI)
AI STATUS | AI CHECK BBRI | AI RUN BBRI
→ CHECK: 0 AI; RUN: maks. 3 API berbayar dan dinonaktifkan secara default."""

_original_send_telegram = legacy.send_telegram


def send_telegram_with_tape(text: str, chat_id: str | None = None) -> None:
    if isinstance(text, TapeReport) and text.dashboard:
        try:
            send_photo(legacy.TELEGRAM_BOT_TOKEN, str(chat_id or legacy.TELEGRAM_CHAT_ID),
                       text.dashboard, text.metrics["ticker"])
        except Exception as exc:
            print(f"[TAPE-DELIVERY] photo unavailable ({type(exc).__name__}); sending text")
            text = str(text) + "\nFoto tidak terkirim; ringkasan angka tetap tersedia."
    _original_send_telegram(text, chat_id)


legacy.send_telegram = send_telegram_with_tape
legacy.extend_help_section("ANALISIS", """📊 TAPE / FOOTPRINT / HAKA–HAKI
/tape ELTY | /tape ELTY 60 | TAPE ELTY
→ Gambar bubble tape, footprint, CVD parsial, VWAP, POC nilai, cetakan besar.
→ Alias /footprint, /haka, /dna. On-demand, maks. 7 operasi ZAPI, 0 AI.
→ 60 = 60 menit terakhir pada tape tersedia, bukan jaminan live.
→ DNA/iceberg/identitas bandar tidak terverifikasi; tanggal/cakupan dilaporkan.""")

_original_command_result = legacy.command_result
legacy.extend_help_section("ANALISIS", _ANALYSIS_HELP)
legacy.extend_help_section("SCREENER", _SCREENER_HELP)
legacy.extend_help_section("BROKER", _BROKER_HELP)
legacy.extend_help_section("DATA", _DATA_HELP)


def command_result(text: str, chat_id: str) -> str:
    parts = text.strip().split()
    if not parts:
        return _original_command_result(text, chat_id)

    first_tape = parts[0].split("@")[0].lower()
    if first_tape in TAPE_ALIASES:
        if not legacy.authorized(chat_id):
            return "⛔ TAPE hanya tersedia untuk chat pribadi pemilik bot."
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "60"):
            return TAPE_HELP
        ticker = parts[1].upper().removesuffix(".JK")
        if not re.fullmatch(r"[A-Z]{4}", ticker):
            return TAPE_HELP
        try:
            return build_tape_report(ticker, window=60 if len(parts) == 3 else None)
        except ValueError as exc:
            return f"⚠️ TAPE {ticker}: {exc}"
        except Exception as exc:
            print(f"[TAPE-ERROR] {type(exc).__name__}")
            return f"⚠️ TAPE {ticker} belum tersedia ({type(exc).__name__}). Coba kembali; /flow {ticker} untuk konteks EOD."

    # Read-only check by default. Legacy owner authorization is checked BEFORE DB/LLM.
    ai_request = parse_ai_command(text)
    if ai_request is not None:
        if not legacy.authorized(chat_id):
            return "⛔ AI Decision hanya tersedia untuk chat pribadi pemilik bot."
        return format_ai_command(ai_request, broker_db=str(legacy.DB_PATH))

    first = parts[0].split("@")[0].lower()

    is_silent_accum = (
        (first == "silent" and len(parts) >= 2 and parts[1].upper() == "ACCUM")
        or first in {"/silent_accum", "silent_accum"}
    )
    if is_silent_accum:
        options = parts[2:] if first == "silent" else parts[1:]
        if len(options) > 1:
            return "Format: SILENT ACCUM [1-15|TICKER]"
        if not options:
            return format_silent_accum(
                str(legacy.DB_PATH), legacy.safe_end_date(), limit=10
            )
        option = options[0].strip().upper().replace(".JK", "")
        if option.isdigit():
            if 1 <= int(option) <= 15:
                return format_silent_accum(
                    str(legacy.DB_PATH), legacy.safe_end_date(), limit=int(option)
                )
            return "Format: SILENT ACCUM [1-15|TICKER]"
        if legacy.TICKER_RE.fullmatch(option):
            return format_silent_accum(
                str(legacy.DB_PATH), legacy.safe_end_date(), ticker=option, limit=1
            )
        return "Format: SILENT ACCUM [1-15|TICKER]"

    if first in {"insider", "/insider"}:
        format_hint = "Format: INSIDER BBRI [1-10] atau INSIDER REFRESH BBRI [1-10]"
        refresh = len(parts) >= 2 and parts[1].upper() == "REFRESH"
        ticker_index = 2 if refresh else 1
        if len(parts) <= ticker_index or len(parts) > ticker_index + 2:
            return format_hint
        ticker = parts[ticker_index].strip().upper().replace(".JK", "")
        if not legacy.TICKER_RE.fullmatch(ticker):
            return format_hint
        limit = 5
        if len(parts) == ticker_index + 2:
            if not parts[-1].isdigit() or not 1 <= int(parts[-1]) <= 10:
                return format_hint
            limit = int(parts[-1])
        if refresh and not legacy.authorized(chat_id):
            return "⛔ INSIDER REFRESH hanya tersedia untuk chat pribadi pemilik bot."
        return format_insider_report(ticker, refresh=refresh, limit=limit)

    is_bandar_momentum = (
        (first == "bandar" and len(parts) >= 2 and parts[1].upper() == "MOMENTUM")
        or first in {"/bandar_momentum", "bandar_momentum"}
    )
    if is_bandar_momentum:
        options = parts[2:] if first == "bandar" else parts[1:]
        format_hint = "Format: BANDAR MOMENTUM [EOD|LIVE] [1-15]"
        mode = "EOD"
        limit = 10
        mode_seen = False
        limit_seen = False
        for option in options:
            upper = option.upper()
            if upper in {"LIVE", "EOD"}:
                if mode_seen:
                    return format_hint
                mode = upper
                mode_seen = True
            elif option.isdigit() and 1 <= int(option) <= 15:
                if limit_seen:
                    return format_hint
                limit = int(option)
                limit_seen = True
            else:
                return format_hint
        return format_bandar_momentum(
            str(legacy.DB_PATH),
            legacy.safe_end_date(),
            mode=mode,
            limit=limit,
        )

    is_bull_div = (
        (first == "bull" and len(parts) >= 2 and parts[1].upper() in {"DIV", "DIVERGENCE"})
        or first in {"/bulldiv", "bulldiv", "/bull_div", "bull_div"}
    )
    if is_bull_div:
        options = parts[2:] if first == "bull" else parts[1:]
        if len(options) > 1:
            return "Format: BULL DIV [1-15|TICKER]"
        if not options:
            return format_bull_divergence(limit=10)
        option = options[0].strip().upper()
        if option.isdigit() and 1 <= int(option) <= 15:
            return format_bull_divergence(limit=int(option))
        if legacy.TICKER_RE.fullmatch(option):
            return format_bull_divergence(ticker=option, limit=1)
        return "Format: BULL DIV [1-15|TICKER]"

    is_ratio_trend = (
        (
            first in {"ratio", "quality"}
            and len(parts) >= 2
            and parts[1].upper() == "TREND"
        )
        or first in {
            "/ratiotrend", "ratiotrend", "/ratio_trend", "ratio_trend",
            "/qualitytrend", "qualitytrend", "/quality_trend", "quality_trend",
        }
    )
    if is_ratio_trend:
        options = parts[2:] if first in {"ratio", "quality"} else parts[1:]
        if len(options) > 1:
            return "Format: RATIO TREND [1-15|TICKER]"
        if not options:
            return format_quality_trend(limit=10)
        option = options[0].strip().upper()
        if option.isdigit() and 1 <= int(option) <= 15:
            return format_quality_trend(limit=int(option))
        if legacy.TICKER_RE.fullmatch(option):
            return format_quality_trend(ticker=option, limit=1)
        return "Format: RATIO TREND [1-15|TICKER]"

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
