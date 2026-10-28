"""Snapshot-based, fail-closed market regime gate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Regime:
    label: str
    mode: str
    score: int
    breadth: float | None
    foreign_net: float | None
    avg_change: float | None
    sample: int
    advances: int
    declines: int
    unchanged: int
    foreign_estimated: bool
    source: str
    reasons: tuple[str, ...]


def _num(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _foreign_value(row: dict, close: float) -> tuple[float | None, bool]:
    """Return net foreign IDR and whether it was estimated from shares."""
    net = _num(row, "netForeignValue", "NetForeignValue", "net_foreign_value")
    if net is not None:
        return net, False

    buy_value = _num(
        row, "foreignBuyValue", "ForeignBuyValue", "foreign_buy_value"
    )
    sell_value = _num(
        row, "foreignSellValue", "ForeignSellValue", "foreign_sell_value"
    )
    if buy_value is not None and sell_value is not None:
        return buy_value - sell_value, False

    # Production stock-summary currently exposes foreign volume in shares.
    # Estimate IDR using the row close, and label it explicitly in output.
    buy_shares = _num(row, "ForeignBuy", "foreignBuy", "foreign_buy")
    sell_shares = _num(row, "ForeignSell", "foreignSell", "foreign_sell")
    if buy_shares is not None and sell_shares is not None and close > 0:
        return (buy_shares - sell_shares) * close, True
    return None, False


def detect_regime(
    rows: list[dict],
    source: str = "shared market snapshot",
) -> Regime:
    usable: list[tuple[float, float, bool]] = []
    for row in rows:
        close = _num(row, "Close", "close", "Last", "last")
        opening = _num(row, "OpenPrice", "Open", "open")
        if close is None or close <= 0 or opening is None or opening <= 0:
            continue
        net, estimated = _foreign_value(row, close)
        if net is None:
            continue
        change = ((close / opening) - 1) * 100
        usable.append((change, net, estimated))

    sample = len(usable)
    if sample < 100:
        return Regime(
            "UNKNOWN", "CASH", 0, None, None, None, sample,
            0, 0, 0, False, source,
            ("Data breadth/foreign belum cukup (minimal 100 saham).",),
        )

    advances = sum(change > 0 for change, _, _ in usable)
    declines = sum(change < 0 for change, _, _ in usable)
    unchanged = sample - advances - declines
    breadth = (advances - declines) / sample
    foreign_net = sum(net for _, net, _ in usable)
    avg_change = sum(change for change, _, _ in usable) / sample
    foreign_estimated = any(estimated for _, _, estimated in usable)

    score = 0
    reasons: list[str] = []
    if breadth >= 0.15:
        score += 2
        reasons.append("breadth positif")
    elif breadth <= -0.15:
        score -= 2
        reasons.append("breadth negatif")
    else:
        reasons.append("breadth campuran")

    if foreign_net > 0:
        score += 1
        reasons.append("foreign net buy")
    else:
        score -= 1
        reasons.append("foreign net sell")

    if avg_change > 0.15:
        score += 1
    elif avg_change < -0.15:
        score -= 1

    if score >= 3:
        label, mode = "BULLISH", "SWING"
    elif score >= 1:
        label, mode = "NEUTRAL", "INTRADAY"
    else:
        label, mode = "BEARISH", "CASH"

    return Regime(
        label, mode, score, breadth, foreign_net, avg_change, sample,
        advances, declines, unchanged, foreign_estimated, source, tuple(reasons),
    )


def format_regime(regime: Regime, as_of: datetime | None = None) -> str:
    as_of = as_of or datetime.now(ZoneInfo("Asia/Jakarta"))
    if regime.label == "UNKNOWN":
        return (
            "🛡 MARKET REGIME GATE\n\n"
            "REGIME: UNKNOWN\nMODE: CASH / NO TRADE\n"
            f"Sample: {regime.sample}\nSource: {regime.source}\n"
            f"Reason: {regime.reasons[0]}\n"
            "Action: refresh market snapshot lalu ulangi command."
        )

    foreign_note = " (estimasi shares × close)" if regime.foreign_estimated else ""
    action = {
        "CASH": "CASH / NO TRADE; tunggu breadth membaik.",
        "INTRADAY": "Selective momentum; maksimal 1–2 posisi.",
        "SWING": "Swing diperbolehkan setelah konfirmasi saham individual.",
    }[regime.mode]
    return (
        "🛡 MARKET REGIME GATE\n\n"
        f"REGIME: {regime.label}\nMODE: {regime.mode}\nScore: {regime.score:+d}\n"
        f"Breadth: {regime.breadth * 100:+.1f}% "
        f"(naik {regime.advances} / turun {regime.declines} / tetap {regime.unchanged})\n"
        f"Avg open→close: {regime.avg_change:+.2f}%\n"
        f"Foreign net: Rp {regime.foreign_net:+,.0f}{foreign_note}\n"
        f"Sample valid: {regime.sample}\nSource: {regime.source}\n"
        f"As of: {as_of:%d-%m-%Y %H:%M} WIB\n"
        f"Reasons: {', '.join(regime.reasons)}\n\nACTION: {action}"
    )
