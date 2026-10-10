"""Read-only Bot 1/2 decision context from Universe Intelligence V1.

No extra ZAPI calls. A missing/old score never becomes a false zero or broker
confirmation. Existing Bot 1 foreign/entry gates and Bot 2 market gates remain
authoritative; the new score ranks valid candidates and surfaces risk warnings.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import tracker
from project_fabric.universe_intelligence import institutional_flow

WIB = ZoneInfo("Asia/Jakarta")
WEIGHTS = {
    "accumulation_persistence": 0.25,
    "breakout_readiness": 0.25,
    "institutional_flow": 0.20,
    "distribution_risk": 0.15,
    "entry_quality": 0.15,
}
HISTORY_KEYS = (
    "accumulation_persistence", "breakout_readiness",
    "distribution_risk", "entry_quality",
)
LABELS = {
    "accumulation_persistence": "Accumulation (proxy)",
    "breakout_readiness": "Breakout Readiness",
    "institutional_flow": "Foreign Flow",
    "distribution_risk": "Distribution Risk",
    "entry_quality": "Entry Quality",
}


def _valid_score(item):
    if not isinstance(item, dict):
        return None
    try:
        value = float(item.get("score"))
        if not isfinite(value) or not 0 <= value <= 100:
            return None
        return value
    except (TypeError, ValueError):
        return None


def _date_is_recent(value: str | None, as_of: str | None, max_age_days: int = 8) -> bool:
    try:
        reference = datetime.fromisoformat(str(as_of)).date() if as_of else datetime.now(WIB).date()
        observed = datetime.fromisoformat(str(value)).date()
    except (ValueError, TypeError):
        return False
    return timedelta(0) <= reference - observed <= timedelta(days=max_age_days)


def _safe_payload(ticker):
    try:
        payload = tracker.peek_universe_intelligence(ticker)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        # Intel is an optional confirmation layer; preserve existing commands.
        print(f"[UNIVERSE-INTEL-WARN] {type(exc).__name__}: optional cache unavailable")
        return {}


def evaluate_universe(ticker: str, *, market_row: dict | None = None,
                      as_of: str | None = None) -> dict:
    """Compute transparent five-factor ranking from already-cached observations."""
    code = str(ticker).strip().upper()
    payload = _safe_payload(code)
    original = payload.get("scores") or {}
    source = original if isinstance(original, dict) else {}
    items = {key: dict(source[key]) if isinstance(source.get(key), dict) else
             {"score": None, "status": "UNAVAILABLE"} for key in WEIGHTS}
    history_date = payload.get("history_last_date")
    if not _date_is_recent(history_date, as_of):
        for key in HISTORY_KEYS:
            items[key] = {"score": None, "status": "STALE_OR_MISSING"}
    if market_row is not None:
        # Historical Bot 2 snapshots often outlive the 15-minute live Neon index.
        # Label source as dated market snapshot, never as live broker activity.
        flow = institutional_flow(market_row)
        items["institutional_flow"] = flow
    elif not payload.get("summary_observed_at"):
        items["institutional_flow"] = {"score": None, "status": "UNAVAILABLE"}
    elif not _date_is_recent(str(payload.get("summary_observed_at"))[:10], as_of, 8 if payload.get("archived") else 1):
        items["institutional_flow"] = {"score": None, "status": "STALE_OR_MISSING"}

    numbers = {}
    for key, value in items.items():
        score = _valid_score(value)
        # A foreign estimate is never promoted to independent confirmation.
        if key == "institutional_flow" and value.get("source") != "reported_foreign_value":
            score = None
        if score is not None:
            numbers[key] = 100 - score if key == "distribution_risk" else score
    coverage = len(numbers)
    raw_score = (
        round(sum(WEIGHTS[k] * v for k, v in numbers.items()) /
              sum(WEIGHTS[k] for k in numbers), 1)
        if coverage >= 3 else None
    )
    distribution = _valid_score(items["distribution_risk"])
    risk_high = distribution is not None and distribution >= 70
    if raw_score is None:
        decision = "DATA TERBATAS"
    elif risk_high:
        decision = "WASPADA DISTRIBUSI"
    elif raw_score >= 75:
        decision = "PRIORITAS TINGGI"
    elif raw_score >= 60:
        decision = "PRIORITAS MENENGAH"
    else:
        decision = "KONFIRMASI LEMAH"
    return {
        "ticker": code,
        "score": raw_score,
        "decision": decision,
        "coverage": coverage,
        "history_date": history_date if _date_is_recent(history_date, as_of) else None,
        "data_source": ("cached-market-snapshot" if market_row is not None else
                        "neon-market-warehouse" if payload.get("archived") else "neon-cache"),
        "risk_high": risk_high,
        "distribution_score": distribution,
        "items": items,
    }


def priority_score(base: float | int | None, intelligence: dict) -> float:
    """Only a tie-breaker/ranking adjustment, never substitutes existing gates."""
    try:
        base = float(base)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(base):
        return 0.0
    score = intelligence.get("score")
    if score is None:
        return round(base, 1)
    ranked = 0.75 * base + 0.25 * float(score)
    # Prioritize candidates with less evidence of distribution, even when the
    # original entry and market eligibility requirements both passed.
    if intelligence.get("risk_high"):
        ranked -= 12.0
    return round(max(0.0, min(100.0, ranked)), 1)


def summary_line(intelligence: dict) -> str:
    score = intelligence.get("score")
    coverage = intelligence.get("coverage", 0)
    if score is None:
        return f"🧠 Universe: DATA TERBATAS ({coverage}/5 indikator)"
    return (f"🧠 Universe: {score:.1f}/100 ({coverage}/5) | "
            f"{intelligence['decision']}")


def format_universe_report(ticker: str) -> str:
    # Only explicit /intel query may use at most one cache-first upstream
    # stock-history request when the permanent archive lacks this ticker.
    try:
        tracker.ensure_universe_intelligence(ticker, allow_one_fetch=True)
    except Exception as exc:
        print(f"[UNIVERSE-REPORT-WARN] {type(exc).__name__}")
    info = evaluate_universe(ticker)
    out = [f"🧠 UNIVERSE INTELLIGENCE — {info['ticker']}",
           summary_line(info),
           f"History terakhir: {info['history_date'] or 'tidak tersedia/terlalu lama'}",
           f"Sumber: {info['data_source']} (historis, bukan harga live)",
           ""]
    for key, label in LABELS.items():
        item = info["items"].get(key, {})
        val = _valid_score(item)
        if val is None:
            txt = "N/A"
        else:
            txt = f"{val:.1f}/100"
        if key == "institutional_flow" and item.get("source") == "estimated_from_foreign_shares_x_close":
            txt += " (estimasi, bukan nilai transaksi aktual)"
        out.append(f"• {label}: {txt}")
    out.extend(["", "Skor akumulasi adalah PROXY harga-volume, bukan bukti broker.",
                "High Distribution Risk = risiko tinggi, bukan rekomendasi jual.",
                "Data observasi, bukan instruksi entry atau probabilitas win rate."])
    return "\n".join(out)
