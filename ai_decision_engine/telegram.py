"""Telegram command adapter for the isolated Bot 1 AI decision layer.

No Telegram sends occur here: the existing authorized dispatcher delivers the
text. AI CHECK is read-only/deterministic; AI RUN is strictly opt-in and paid.
"""
from __future__ import annotations

import os
from typing import NamedTuple

from .__main__ import (
    DataNotReady,
    decide,
    is_repairable_history_error,
    load_snapshot,
    ticker_code,
)


USAGE = (
    "🧠 BOT 1 — AI DECISION\n"
    "AI STATUS — status integrasi dan biaya\n"
    "AI CHECK BBRI — validasi EOD/cache, 0 AI; repair histori maks 1 ZAPI\n"
    "AI RUN BBRI — Bull/Bear/Risk, maks 3 request AI (berbayar, jika diaktifkan)\n"
    "Alias: /ai BBRI, /ai_run BBRI, BOT1 AI CHECK BBRI"
)

class AIRequest(NamedTuple):
    mode: str
    ticker: str | None


def parse_ai_command(text: str) -> AIRequest | None:
    tokens = text.strip().split()
    if not tokens:
        return None
    first = tokens[0].split("@")[0].lstrip("/").upper()
    rest = tokens[1:]
    if first == "BOT1" and rest and rest[0].upper() == "AI":
        first, rest = "AI", rest[1:]
    if first not in {"AI", "AI_CHECK", "AICHECK", "AI_RUN", "AIRUN", "AI_STATUS"}:
        return None
    if first == "AI_STATUS":
        return AIRequest("STATUS" if not rest else "HELP", None)
    if first in {"AI_CHECK", "AICHECK", "AI_RUN", "AIRUN"}:
        mode = "RUN" if first in {"AI_RUN", "AIRUN"} else "CHECK"
        return AIRequest(mode, rest[0]) if len(rest) == 1 else AIRequest("HELP", None)
    if not rest:
        return AIRequest("HELP", None)
    sub = rest[0].upper()
    if sub == "STATUS":
        return AIRequest("STATUS" if len(rest) == 1 else "HELP", None)
    if sub in {"CHECK", "RUN"}:
        return AIRequest(sub, rest[1]) if len(rest) == 2 else AIRequest("HELP", None)
    return AIRequest("CHECK", rest[0]) if len(rest) == 1 else AIRequest("HELP", None)


def remote_enabled() -> bool:
    return os.getenv("BOT1_AI_ENABLED") == "1" and os.getenv("BOT1_AI_REMOTE") == "1"


def _number(value: float | int | None) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.2f}"


def _broker_value(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"Rp {value / 1_000_000_000:+,.2f} M"


def _repair_history_and_reload(code: str, broker_db: str) -> tuple[dict, int]:
    """Repair one ticker through the shared cache, then re-read the strict snapshot.

    ``tracker.fetch_stock_history`` checks the six-hour Project Fabric cache first,
    archives completed sessions in Neon, and makes at most one logical upstream
    request when no fresh cache exists.  The second load still applies every
    point-in-time and continuity guard from ``load_snapshot``.
    """
    import tracker

    before = int(tracker._GATEWAY.usage()["totals"]["upstream_attempts"])
    tracker.fetch_stock_history(code, length=tracker.CANONICAL_STOCK_HISTORY_LENGTH)
    after = int(tracker._GATEWAY.usage()["totals"]["upstream_attempts"])
    return load_snapshot(code, broker_db=broker_db), max(0, after - before)


def format_ai_command(request: AIRequest, *, broker_db: str = "broker_tracker.db") -> str:
    if request.mode == "HELP":
        return USAGE
    if request.mode == "STATUS":
        status = "TERSEDIA (API berbayar)" if remote_enabled() and os.getenv("OPENAI_API_KEY") else "NONAKTIF"
        return (
            "🧠 BOT1 AI STATUS\n"
            "Mode CHECK: SIAP (cache Neon + SQLite, tanpa AI)\n"
            f"Mode RUN: {status}\n"
            "TradingAgents: client v0.7.1 (bukan graph lengkap)\n"
            "ZAPI tambahan: 0 | Auto-trading: TIDAK\n"
            "Perintah: AI CHECK BBRI / AI RUN BBRI"
        )
    if request.mode not in {"CHECK", "RUN"}:
        return USAGE
    try:
        code = ticker_code(request.ticker or "")
    except ValueError:
        return USAGE
    remote = request.mode == "RUN"
    # Reject paid work before opening DB or starting expensive deps when disabled.
    if remote and not remote_enabled():
        return (
            f"🔒 BOT1 AI RUN {code} BELUM DIAKTIFKAN\n"
            "Aktifkan BOT1_AI_ENABLED=1 dan BOT1_AI_REMOTE=1 serta API key "
            "sesudah uji valid. Untuk analisis cache gratis: AI CHECK " + code
        )
    if remote and not os.getenv("OPENAI_API_KEY"):
        return "🔒 OPENAI_API_KEY belum dikonfigurasi. AI CHECK " + code + " tetap tersedia."
    history_repaired = False
    zapi_requests = 0
    try:
        try:
            snapshot = load_snapshot(code, broker_db=broker_db)
        except DataNotReady as exc:
            if not is_repairable_history_error(exc):
                raise
            history_repaired = True
            snapshot, zapi_requests = _repair_history_and_reload(code, broker_db)
        result = decide(snapshot, remote=remote)
    except (DataNotReady, ValueError) as exc:
        return (f"⚠️ BOT1 AI {code}: WAIT / DATA NOT READY\n{str(exc)[:180]}\n"
                f"API AI: 0 | ZAPI: {zapi_requests}")
    except Exception as exc:
        # Never leak connection strings, credentials, or provider errors.
        return (f"⚠️ BOT1 AI {code}: WAIT / ERROR\n"
                f"Error type: {type(exc).__name__}\n"
                "Periksa log GitHub; tidak ada order otomatis.")
    lines = [
        "🧠 BOT 1 — AI DECISION (EOD)",
        f"Saham: {code} | Data: {snapshot['asof']}",
        f"Regime: {snapshot['market_regime']['label']} / {snapshot['market_regime']['mode']}",
        f"Close: {_number(snapshot['close'])} | MA20: {_number(snapshot['ma20'])}",
        f"RSI14: {_number(snapshot['rsi14'])} | Volume/MA20: {_number(snapshot['volume_ratio20'])}x",
        f"Broker 30D: {_broker_value(snapshot['broker']['net_value_idr'])} "
        f"({snapshot['broker']['coverage']}; {snapshot['broker']['sessions']} sesi)",
        f"Hard gate: {result['gate']}",
    ]
    if history_repaired:
        source = "cache bersama" if zapi_requests == 0 else f"{zapi_requests} request ZAPI"
        lines.append(f"History repair: BERHASIL ({source})")
    if result["gate_reasons"]:
        lines.append("Alasan gate: " + "; ".join(result["gate_reasons"][:5]))
    if remote:
        lines.extend([
            f"AI action: {result['action']} (bukan order)",
            f"Confidence: {result['confidence'] if result['confidence'] is not None else 'N/A'}",
            "Alasan: " + result["reason"],
            "Risiko: " + result["risk"],
        ])
    else:
        lines.append("AI action: NOT RUN (CHECK hanya validasi data & gate)")
    lines.extend([
        f"AI requests: {result['ai_calls']} | ZAPI requests: {zapi_requests}",
        "Catatan: broker net value adalah aktivitas transaksi, bukan kepemilikan.",
        "Tidak ada eksekusi order; bukan jaminan profit.",
    ])
    return "\n".join(lines)
