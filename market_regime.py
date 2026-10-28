"""Snapshot-based, fail-closed market regime gate."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class Regime:
    label: str; mode: str; score: int; breadth: float | None; foreign_net: float | None
    avg_change: float | None; sample: int; source: str; reasons: tuple[str, ...]

def _num(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try: return float(value)
            except (TypeError, ValueError): pass
    return None

def detect_regime(rows: list[dict], source: str = "shared market snapshot") -> Regime:
    usable = []
    for row in rows:
        close = _num(row, "Close", "close", "Last", "last")
        opening = _num(row, "OpenPrice", "Open", "open")
        net = _num(row, "netForeignValue", "NetForeignValue")
        if net is None:
            buy = _num(row, "foreignBuyValue", "ForeignBuyValue")
            sell = _num(row, "foreignSellValue", "ForeignSellValue")
            if buy is not None and sell is not None: net = buy - sell
        change = ((close / opening) - 1) * 100 if close and opening and opening > 0 else None
        if change is not None and net is not None: usable.append((change, net))
    sample = len(usable)
    if sample < 100:
        return Regime("UNKNOWN", "CASH", 0, None, None, None, sample, source,
                      ("Data breadth/foreign belum cukup (minimal 100 saham).",))
    advances = sum(c > 0 for c, _ in usable); declines = sum(c < 0 for c, _ in usable)
    breadth = (advances - declines) / sample
    foreign_net = sum(n for _, n in usable); avg_change = sum(c for c, _ in usable) / sample
    score = 0; reasons = []
    if breadth >= .15: score += 2; reasons.append("breadth positif")
    elif breadth <= -.15: score -= 2; reasons.append("breadth negatif")
    else: reasons.append("breadth campuran")
    if foreign_net > 0: score += 1; reasons.append("foreign net buy")
    else: score -= 1; reasons.append("foreign net sell")
    if avg_change > .15: score += 1
    elif avg_change < -.15: score -= 1
    if score >= 3: label, mode = "BULLISH", "SWING"
    elif score >= 1: label, mode = "NEUTRAL", "INTRADAY"
    else: label, mode = "BEARISH", "CASH"
    return Regime(label, mode, score, breadth, foreign_net, avg_change, sample, source, tuple(reasons))

def format_regime(regime: Regime, as_of: datetime | None = None) -> str:
    as_of = as_of or datetime.now(ZoneInfo("Asia/Jakarta"))
    if regime.label == "UNKNOWN":
        return ("🛡 MARKET REGIME GATE\n\nREGIME: UNKNOWN\nMODE: CASH / NO TRADE\n"
                f"Sample: {regime.sample}\nSource: {regime.source}\nReason: {regime.reasons[0]}\n"
                "Action: refresh market snapshot lalu ulangi command.")
    return ("🛡 MARKET REGIME GATE\n\n"
            f"REGIME: {regime.label}\nMODE: {regime.mode}\nScore: {regime.score:+d}\n"
            f"Breadth: {regime.breadth*100:+.1f}%\nAvg open→close: {regime.avg_change:+.2f}%\n"
            f"Foreign net: Rp {regime.foreign_net:+,.0f}\nSample: {regime.sample}\n"
            f"As of: {as_of:%d-%m-%Y %H:%M} WIB\nReasons: {', '.join(regime.reasons)}\n\n"
            f"ACTION: {'selective momentum' if regime.mode == 'INTRADAY' else 'follow regime gate'}.")
