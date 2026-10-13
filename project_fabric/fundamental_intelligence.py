"""Read-only, sector-relative fundamental intelligence for Bot 1.

The module consumes the durable fundamental warehouse in Neon.  It never
calls ZAPI and never writes/migrates database state.  Scores are decision
support only: existing broker, foreign-flow, technical and risk/reward gates
remain authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from statistics import mean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


JAKARTA = ZoneInfo("Asia/Jakarta")
MIN_COVERAGE = 0.70

_METRICS = (
    "pe", "pb", "ps", "roe_pct", "roa_pct", "net_margin_pct",
    "debt_equity_pct", "current_ratio",
)


@dataclass(frozen=True)
class Benchmark:
    label: str
    mode: str
    count: int
    pools: tuple[tuple[dict[str, Any], ...], ...]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_metric(metric: str, value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    if metric in {"pe", "pb", "ps", "current_ratio"} and number <= 0:
        return None
    if metric == "debt_equity_pct" and number < 0:
        return None
    return number


def _quantile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _winsorized_percentile(value: float, values: Iterable[float]) -> float | None:
    clean = [float(item) for item in values if math.isfinite(float(item))]
    if not clean:
        return None
    low = _quantile(clean, 0.05)
    high = _quantile(clean, 0.95)
    assert low is not None and high is not None
    clipped = [min(high, max(low, item)) for item in clean]
    target = min(high, max(low, value))
    less = sum(item < target for item in clipped)
    equal = sum(item == target for item in clipped)
    return 100.0 * (less + 0.5 * equal) / len(clipped)


def macro_sector(sector: str | None) -> str:
    text = (sector or "").strip().lower()
    groups = (
        ("FINANCIAL", ("bank", "insurance", "financing", "financial", "securities", "investment service")),
        ("TECHNOLOGY", ("online", "software", "internet", "computer", "technology", "it service", "digital")),
        ("TELECOM_INFRA", ("telecom", "wireless", "tower", "infrastructure operator")),
        ("COMMODITY", ("coal", "mining", "metal", "oil", "gas", "plantation", "forest", "paper", "chemical")),
        ("PROPERTY", ("property", "real estate", "building construction", "residential")),
        ("HEALTHCARE", ("healthcare", "hospital", "pharma", "medical")),
        ("CONSUMER", ("food", "beverage", "consumer", "retail", "household", "tobacco", "apparel")),
        ("INDUSTRIAL", ("construction", "logistics", "transport", "machinery", "equipment", "industrial")),
    )
    for label, keywords in groups:
        if any(keyword in text for keyword in keywords):
            return label
    return "OTHER"


def sector_policy(sector: str | None) -> dict[str, float]:
    macro = macro_sector(sector)
    if macro == "FINANCIAL":
        # Current ratio and conventional D/E are not comparable for banks.
        return {"valuation": 25, "quality": 40, "growth": 25, "safety": 0, "consistency": 10}
    if macro == "TECHNOLOGY":
        return {"valuation": 15, "quality": 25, "growth": 35, "safety": 15, "consistency": 10}
    if macro == "COMMODITY":
        return {"valuation": 25, "quality": 20, "growth": 20, "safety": 20, "consistency": 15}
    if macro == "PROPERTY":
        return {"valuation": 25, "quality": 20, "growth": 15, "safety": 25, "consistency": 15}
    return {"valuation": 20, "quality": 30, "growth": 25, "safety": 15, "consistency": 10}


def choose_benchmark(target: dict[str, Any], universe: list[dict[str, Any]]) -> Benchmark:
    sector = str(target.get("sector") or "UNKNOWN")
    exact = tuple(row for row in universe if str(row.get("sector") or "") == sector)
    macro = macro_sector(sector)
    broad = tuple(row for row in universe if macro_sector(row.get("sector")) == macro)
    global_pool = tuple(universe)
    if len(exact) >= 8:
        return Benchmark(sector, "SEKTOR", len(exact), (exact,))
    if len(exact) >= 4 and len(broad) >= 8:
        return Benchmark(f"{sector} + {macro}", "BLEND 50/50", len(exact), (exact, broad))
    if len(broad) >= 8:
        return Benchmark(macro, "MAKRO SEKTOR", len(broad), (broad,))
    return Benchmark("IDX GLOBAL", "GLOBAL", len(global_pool), (global_pool,))


def benchmark_stats(benchmark: Benchmark, metric: str) -> dict[str, float | None]:
    pool_stats: list[tuple[float, float, float]] = []
    for pool in benchmark.pools:
        values = [_valid_metric(metric, row.get(metric)) for row in pool]
        clean = [value for value in values if value is not None]
        if clean:
            p25, median, p75 = _quantile(clean, 0.25), _quantile(clean, 0.50), _quantile(clean, 0.75)
            assert p25 is not None and median is not None and p75 is not None
            pool_stats.append((p25, median, p75))
    if not pool_stats:
        return {"p25": None, "median": None, "p75": None}
    return {
        "p25": mean(row[0] for row in pool_stats),
        "median": mean(row[1] for row in pool_stats),
        "p75": mean(row[2] for row in pool_stats),
    }


def relative_score(benchmark: Benchmark, metric: str, value: Any, *, lower_is_better: bool) -> float | None:
    target = _valid_metric(metric, value)
    if target is None:
        return None
    scores: list[float] = []
    for pool in benchmark.pools:
        values = [_valid_metric(metric, row.get(metric)) for row in pool]
        percentile = _winsorized_percentile(target, [item for item in values if item is not None])
        if percentile is not None:
            scores.append(100.0 - percentile if lower_is_better else percentile)
    return mean(scores) if scores else None


_QUARTER = re.compile(r"Q([1-4])\s*[’']?\s*(\d{2,4})", re.IGNORECASE)


def _period_key(row: dict[str, Any]) -> tuple[int, int] | None:
    match = _QUARTER.search(str(row.get("timeframe") or ""))
    if not match:
        return None
    year = int(match.group(2))
    if year < 100:
        year += 2000
    return year, int(match.group(1))


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def _growth_score(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if previous < 0 <= current:
        return 100.0
    change = _growth(current, previous)
    if change is None:
        return 60.0 if current > 0 else 20.0
    if previous < 0 and current < 0:
        return 70.0 if current > previous else 15.0
    if change >= 20:
        return 100.0
    if change >= 5:
        return 80.0
    if change >= 0:
        return 60.0
    if change >= -10:
        return 40.0
    return 10.0


def _ocf_score(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if previous <= 0 < current:
        return 100.0
    if current <= 0:
        return 10.0 if current < previous else 35.0
    change = _growth(current, previous)
    if change is None:
        return 70.0
    if change >= 10:
        return 90.0
    if change >= 0:
        return 75.0
    return 55.0


def growth_snapshot(statements: list[dict[str, Any]]) -> dict[str, Any]:
    quarters = [(key, row) for row in statements if row.get("period_kind") == "quarterly" and (key := _period_key(row))]
    quarters.sort(key=lambda item: item[0])
    if not quarters:
        return {"current": None, "previous": None, "scores": {}, "values": {}}
    key, current = quarters[-1]
    previous = next((row for item_key, row in quarters if item_key == (key[0] - 1, key[1])), None)
    if previous is None:
        return {"current": current, "previous": None, "scores": {}, "values": {}}
    fields = ("revenue", "net_profit_loss", "operating_cash_flow")
    values = {field: _growth(_number(current.get(field)), _number(previous.get(field))) for field in fields}
    scores = {
        "revenue_yoy": _growth_score(_number(current.get("revenue")), _number(previous.get("revenue"))),
        "profit_yoy": _growth_score(_number(current.get("net_profit_loss")), _number(previous.get("net_profit_loss"))),
        "ocf_yoy": _ocf_score(_number(current.get("operating_cash_flow")), _number(previous.get("operating_cash_flow"))),
    }
    return {"current": current, "previous": previous, "scores": scores, "values": values}


def consistency_snapshot(statements: list[dict[str, Any]]) -> dict[str, Any]:
    annual = [row for row in statements if row.get("period_kind") == "annually" and str(row.get("timeframe") or "").isdigit()]
    annual.sort(key=lambda row: int(row["timeframe"]))
    recent = annual[-3:]
    scores: dict[str, float] = {}
    for field, name in (("net_profit_loss", "profit_positive"), ("operating_cash_flow", "ocf_positive")):
        values = [_number(row.get(field)) for row in recent]
        usable = [value for value in values if value is not None]
        if usable:
            scores[name] = 100.0 * sum(value > 0 for value in usable) / len(usable)
    return {"years": [str(row.get("timeframe")) for row in recent], "scores": scores}


def _category(parts: Iterable[float | None]) -> tuple[float | None, float]:
    values = list(parts)
    available = [value for value in values if value is not None]
    return (mean(available), len(available) / len(values)) if values and available else (None, 0.0)


def _structural_red_flag(statements: list[dict[str, Any]]) -> bool:
    def bad(row: dict[str, Any]) -> bool:
        profit = _number(row.get("net_profit_loss"))
        ocf = _number(row.get("operating_cash_flow"))
        return profit is not None and ocf is not None and profit < 0 and ocf < 0

    quarters = [(key, row) for row in statements if row.get("period_kind") == "quarterly" and (key := _period_key(row))]
    quarters.sort(key=lambda item: item[0])
    annual = [row for row in statements if row.get("period_kind") == "annually" and str(row.get("timeframe") or "").isdigit()]
    annual.sort(key=lambda row: int(row["timeframe"]))
    return (len(quarters) >= 2 and all(bad(row) for _, row in quarters[-2:])) or (len(annual) >= 2 and all(bad(row) for row in annual[-2:]))


def score_fundamental(target: dict[str, Any], universe: list[dict[str, Any]], statements: list[dict[str, Any]]) -> dict[str, Any]:
    benchmark = choose_benchmark(target, universe)
    policy = sector_policy(target.get("sector"))
    valuation = _category(relative_score(benchmark, metric, target.get(metric), lower_is_better=True) for metric in ("pe", "pb", "ps"))
    quality = _category(relative_score(benchmark, metric, target.get(metric), lower_is_better=False) for metric in ("roe_pct", "roa_pct", "net_margin_pct"))
    growth = growth_snapshot(statements)
    growth_category = _category(growth["scores"].get(metric) for metric in ("revenue_yoy", "profit_yoy", "ocf_yoy"))

    current = growth.get("current") or {}
    if macro_sector(target.get("sector")) == "FINANCIAL":
        safety = (None, 0.0)
    else:
        safety = _category((
            relative_score(benchmark, "debt_equity_pct", target.get("debt_equity_pct"), lower_is_better=True),
            relative_score(benchmark, "current_ratio", target.get("current_ratio"), lower_is_better=False),
            100.0 if (_number(current.get("operating_cash_flow")) or 0) > 0 else (10.0 if current.get("operating_cash_flow") is not None else None),
        ))
    consistency = consistency_snapshot(statements)
    consistency_category = _category(consistency["scores"].get(metric) for metric in ("profit_positive", "ocf_positive"))
    categories = {
        "valuation": valuation,
        "quality": quality,
        "growth": growth_category,
        "safety": safety,
        "consistency": consistency_category,
    }
    total_weight = sum(policy.values())
    effective_weight = sum(policy[name] * availability for name, (_, availability) in categories.items())
    coverage = effective_weight / total_weight if total_weight else 0.0
    weighted = sum(policy[name] * availability * score for name, (score, availability) in categories.items() if score is not None)
    score = weighted / effective_weight if coverage >= MIN_COVERAGE and effective_weight else None
    red_flag = _structural_red_flag(statements)
    if score is None:
        tier, overlay = "DATA TERBATAS", 0
    elif red_flag:
        tier, overlay = "BLOCK SWING — RED FLAG", -12
    elif score >= 75:
        tier, overlay = "PRIORITAS", 5
    elif score >= 60:
        tier, overlay = "LAYAK / TRADABLE", 2
    elif score >= 45:
        tier, overlay = "TAKTIKAL", 0
    else:
        tier, overlay = "HINDARI SWING", -8
    return {
        "score": round(score, 1) if score is not None else None,
        "tier": tier,
        "overlay": overlay,
        "coverage": round(coverage * 100.0, 1),
        "benchmark": benchmark,
        "benchmark_stats": {metric: benchmark_stats(benchmark, metric) for metric in _METRICS},
        "policy": policy,
        "categories": {name: (round(value, 1) if value is not None else None) for name, (value, _) in categories.items()},
        "growth": growth,
        "consistency": consistency,
        "red_flag": red_flag,
    }


def _read_warehouse(ticker: str, database_url: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    url = (database_url if database_url is not None else configured_database_url()).strip()
    if not url or psycopg is None:
        raise RuntimeError("Neon fundamental belum terkonfigurasi")
    with psycopg.connect(url, connect_timeout=8, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ticker, company_name, sector, pe, pb, ps, roe_pct, roa_pct,
                      net_margin_pct, debt_equity_pct, current_ratio,
                      endpoint_status, updated_at
               FROM project_fabric.fundamental_latest
               ORDER BY ticker"""
        )
        columns = [item.name for item in cursor.description]
        universe = [dict(zip(columns, row)) for row in cursor.fetchall()]
        target = next((row for row in universe if str(row["ticker"]).upper() == ticker), None)
        if target is None:
            raise LookupError(f"Fundamental {ticker} belum tersedia")
        cursor.execute(
            """SELECT period_kind, timeframe, revenue, net_profit_loss,
                      operating_cash_flow, captured_at
               FROM project_fabric.financial_statement_periods
               WHERE ticker = %s""",
            (ticker,),
        )
        columns = [item.name for item in cursor.description]
        statements = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return target, universe, statements


def evaluate_fundamental(ticker: str, database_url: str | None = None) -> dict[str, Any]:
    code = ticker.strip().upper().replace(".JK", "")
    try:
        target, universe, statements = _read_warehouse(code, database_url)
        result = score_fundamental(target, universe, statements)
        return {"ticker": code, "available": result["score"] is not None, "target": target, **result}
    except Exception as exc:
        return {
            "ticker": code,
            "available": False,
            "score": None,
            "tier": "DATA TERBATAS",
            "overlay": 0,
            "coverage": 0.0,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def _fmt(value: Any, suffix: str = "") -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:,.2f}{suffix}"


def _comparison(value: Any, stats: dict[str, Any], *, higher_is_better: bool, suffix: str) -> str:
    number = _number(value)
    median = _number(stats.get("median"))
    if number is None or median is None:
        return "N/A"
    passed = number >= median if higher_is_better else number <= median
    return f"{_fmt(number, suffix)} vs median {_fmt(median, suffix)} {'✅' if passed else '⚠️'}"


def _pct(value: Any) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:+.1f}%"


def _updated_at(value: Any) -> str:
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=JAKARTA)
        return current.astimezone(JAKARTA).strftime("%d-%m-%Y %H:%M WIB")
    return str(value or "N/A")


def format_fundamental_report(ticker: str, result: dict[str, Any] | None = None) -> str:
    result = result or evaluate_fundamental(ticker)
    code = result["ticker"]
    if result.get("score") is None:
        return "\n".join((
            f"📊 FUNDAMENTAL SEKTOR — {code}",
            "Status: DATA TERBATAS (fail-closed)",
            f"Detail: {result.get('error', 'coverage fundamental di bawah 70%')}",
            "Tidak ada ZAPI tambahan; cek konfigurasi/data Neon.",
        ))
    target = result["target"]
    stats = result["benchmark_stats"]
    growth = result["growth"]
    benchmark: Benchmark = result["benchmark"]
    categories = result["categories"]
    current = growth.get("current") or {}
    lines = [
        f"📊 FUNDAMENTAL SEKTOR — {code}",
        f"{target.get('company_name') or '-'} | {target.get('sector') or 'UNKNOWN'}",
        f"Score: {result['score']:.1f}/100 | {result['tier']} | Coverage {result['coverage']:.0f}%",
        f"Benchmark: {benchmark.label} | {benchmark.mode} | n={benchmark.count}",
        "",
        "VALUASI (menarik jika ≤ median sektor)",
        f"PE  {_comparison(target.get('pe'), stats['pe'], higher_is_better=False, suffix='x')}",
        f"PB  {_comparison(target.get('pb'), stats['pb'], higher_is_better=False, suffix='x')}",
        f"PS  {_comparison(target.get('ps'), stats['ps'], higher_is_better=False, suffix='x')}",
        "",
        "KUALITAS (kuat jika ≥ P75; ✅ di bawah ini = minimal median)",
        f"ROE {_comparison(target.get('roe_pct'), stats['roe_pct'], higher_is_better=True, suffix='%')}",
        f"ROA {_comparison(target.get('roa_pct'), stats['roa_pct'], higher_is_better=True, suffix='%')}",
        f"NPM {_comparison(target.get('net_margin_pct'), stats['net_margin_pct'], higher_is_better=True, suffix='%')}",
        "",
        f"GROWTH {current.get('timeframe') or 'N/A'} YoY",
        f"Revenue {_pct(growth.get('values', {}).get('revenue'))}",
        f"Laba    {_pct(growth.get('values', {}).get('net_profit_loss'))}",
        f"OCF     {_pct(growth.get('values', {}).get('operating_cash_flow'))}",
    ]
    if macro_sector(target.get("sector")) == "FINANCIAL":
        lines += ["", "SAFETY: CR dan D/E normal dikecualikan untuk sektor finansial."]
    else:
        lines += [
            "",
            "SAFETY (D/E lebih rendah; CR lebih tinggi dari median)",
            f"D/E {_comparison(target.get('debt_equity_pct'), stats['debt_equity_pct'], higher_is_better=False, suffix='x')}",
            f"CR  {_comparison(target.get('current_ratio'), stats['current_ratio'], higher_is_better=True, suffix='%')}",
            f"OCF terbaru {'POSITIF ✅' if (_number(current.get('operating_cash_flow')) or 0) > 0 else 'NEGATIF ⚠️' if current.get('operating_cash_flow') is not None else 'N/A'}",
        ]
    category_text = " | ".join(f"{name[:3].upper()} {value:.0f}" for name, value in categories.items() if value is not None)
    lines += [
        "",
        f"Komponen: {category_text or 'N/A'}",
        f"Overlay ranking Bot1: {result['overlay']:+d} poin (bukan pengganti hard gate)",
        f"Data Neon: {_updated_at(target.get('updated_at'))}",
        "Threshold: skor ≥75 prioritas; 60–74 layak; 45–59 taktikal; <45 hindari swing.",
    ]
    if result["red_flag"]:
        lines.append("⛔ RED FLAG: laba dan OCF negatif berulang; swing diblokir oleh overlay fundamental.")
    return "\n".join(lines)


def fundamental_summary(result: dict[str, Any]) -> str:
    if result.get("score") is None:
        return "Fundamental: DATA TERBATAS | overlay +0"
    return f"Fundamental: {result['score']:.1f}/100 {result['tier']} | overlay {result['overlay']:+d}"


def combine_with_technical(fundamental: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """Combine the soft fundamental overlay with existing technical results.

    The function deliberately does not recompute or relax a hard gate.  The
    existing Foreign Entry result remains the source of truth for eligibility.
    """
    entry = evaluation.get("entry") if isinstance(evaluation, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    base_priority = _number(evaluation.get("priority_score")) if entry else None
    overlay = int(fundamental.get("overlay") or 0)
    final_priority = (
        round(max(0.0, min(100.0, base_priority + overlay)), 1)
        if base_priority is not None else None
    )
    hard_gate = bool(evaluation.get("passed"))
    fundamental_score = _number(fundamental.get("score"))
    red_flag = bool(fundamental.get("red_flag"))

    if red_flag:
        decision = "⛔ BLOCK SWING — RED FLAG FUNDAMENTAL"
        strategy = "Tunggu pemulihan laba dan operating cash flow."
    elif not hard_gate:
        decision = "⏳ WAIT — HARD GATE TEKNIKAL/FOREIGN BELUM PASS"
        strategy = "Jangan entry; selesaikan alasan penolakan pada Foreign Entry."
    elif fundamental_score is None:
        decision = "✅ HARD GATE PASS — FUNDAMENTAL BELUM TERUKUR"
        strategy = "Entry mengikuti plan teknikal, dengan prioritas tanpa bonus fundamental."
    elif fundamental_score >= 75:
        decision = "✅ ENTRY PRIORITAS — QUALITY + MOMENTUM SELARAS"
        strategy = "Gunakan plan entry existing; tetap patuhi invalidasi dan ukuran risiko."
    elif fundamental_score >= 60:
        decision = "✅ ENTRY SELEKTIF — KONFLUENSI CUKUP"
        strategy = "Swing selektif sesuai zona/trigger teknikal existing."
    elif fundamental_score >= 45:
        decision = "🟠 ENTRY TAKTIKAL — BUKAN PRIORITAS SWING"
        strategy = "Ukuran posisi lebih kecil dan disiplin pada stop."
    else:
        decision = "⚠️ HARD GATE PASS, TETAPI HINDARI SWING"
        strategy = "Batasi ke observasi/daytrade; fundamental belum mendukung holding."

    component_keys = (
        "broker_score", "technical_score", "momentum_score",
        "volume_score", "rr_score",
    )
    return {
        "decision": decision,
        "strategy": strategy,
        "hard_gate_passed": hard_gate,
        "entry_score": _number(entry.get("score")),
        "base_priority": base_priority,
        "fundamental_overlay": overlay,
        "final_priority": final_priority,
        "filter_status": entry.get("filter_status") or "N/A",
        "selected_plan": entry.get("selected_plan") or "N/A",
        "selected_rr": _number(entry.get("selected_rr")),
        "components": {key: _number(entry.get(key)) for key in component_keys},
        "reasons": list(evaluation.get("reasons") or []),
    }

