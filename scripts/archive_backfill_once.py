"""One-off, bounded archival bootstrap; no market-wide ZAPI scan."""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tracker
from project_fabric.market_archive import MarketArchive

WIB = ZoneInfo("Asia/Jakarta")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="ADRO")
    args = parser.parse_args()
    code = args.ticker.upper().strip()
    archive = MarketArchive(service="archive-bootstrap")
    if not archive.ready:
        print("Missing shared Neon credentials; archive bootstrap stopped.")
        return 2
    archive.migrate()
    pre_existing = archive.latest_history_date(code)
    age = ((datetime.now(WIB).date() - pre_existing).days
           if pre_existing else None)
    # Only a SINGLE ticker fetch, and only when persistent archive is missing
    # or too old. tracker.fetch_stock_history is cache-first via Project Fabric.
    fetched = False
    if age is None or age > 8:
        print(f"[WAREHOUSE] filling history for {code}; existing_age={age}")
        payload = tracker.fetch_stock_history(code, length=120)
        if not isinstance(payload, dict) or not payload.get("items"):
            raise RuntimeError(f"stock-history {code} missing/invalid")
        fetched = True

    adro = archive.compute_score(code)
    result = archive.rebuild_available(maximum_tickers=250)
    latest = archive.latest_score(code)
    print(json.dumps({
        "ticker": code,
        "one_ticker_fetch": fetched,
        "target_history_last_date": latest.get("history_last_date") if latest else None,
        "target_factor_count": sum(
            1 for val in (latest.get("scores") or {}).values()
            if isinstance(val, dict) and val.get("score") is not None
        ) if latest else 0,
        "scored_history_tickers": result["saved_scores"],
        "failed_tickers": result["failed_tickers"][:10],
    }, sort_keys=True))
    if not latest or not adro or result["saved_scores"] < 1:
        raise RuntimeError(f"Warehouse did not produce Universe scores for {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
