"""Permanent IDX history warehouse on Neon; independent of the ephemeral API cache.

Data saved per ticker/trading date, never conflating today's partial candle with
a completed daily bar. Existing API cache remains the low-latency path.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from .postgres_backend import configured_database_url
from .universe_intelligence import _items, _history, history_features, institutional_flow

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:
    psycopg = None
    Jsonb = None

WIB = timezone(timedelta(hours=7))
_CODE = re.compile(r"^[A-Z0-9]{1,15}$")


def _num(row: dict, *names: str) -> float | None:
    for key in names:
        val = row.get(key)
        if val is None or val == "" or isinstance(val, bool):
            continue
        try:
            n = float(str(val).replace(",", ""))
            if n == n and abs(n) < 1e25:
                return n
        except (ValueError, TypeError):
            continue
    return None


def _session_date(row: dict, *, history: bool) -> date | None:
    stamp = str(row.get("date") or row.get("Date") or row.get("TradingDate") or "")[:10]
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return None


class MarketArchive:
    """Narrow PostgreSQL read/write contract shared by Bot 4 and Bot 1/2."""

    def __init__(self, database_url: str | None = None, service: str = "market-archive"):
        self.url = database_url if database_url is not None else configured_database_url()
        self.service = service

    @property
    def ready(self) -> bool:
        return bool(self.url and psycopg is not None and Jsonb is not None)

    def _connect(self):
        if not self.ready:
            raise RuntimeError("Neon MarketArchive requires PROJECT_FABRIC_DATABASE_URL and psycopg")
        return psycopg.connect(self.url, connect_timeout=6, autocommit=True)

    def migrate(self) -> None:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS project_fabric.stock_summary_daily (
                    trade_date DATE NOT NULL, ticker TEXT NOT NULL,
                    open_price NUMERIC, high_price NUMERIC, low_price NUMERIC,
                    close_price NUMERIC, volume_shares NUMERIC, turnover_idr NUMERIC,
                    frequency BIGINT, foreign_buy_value_idr NUMERIC,
                    foreign_sell_value_idr NUMERIC, raw_json JSONB NOT NULL,
                    source_service TEXT NOT NULL,
                    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(trade_date,ticker))""")
                c.execute("""CREATE TABLE IF NOT EXISTS project_fabric.stock_history_daily (
                    trade_date DATE NOT NULL, ticker TEXT NOT NULL,
                    open_price NUMERIC NOT NULL, high_price NUMERIC NOT NULL,
                    low_price NUMERIC NOT NULL, close_price NUMERIC NOT NULL,
                    volume_shares NUMERIC NOT NULL, turnover_idr NUMERIC,
                    frequency BIGINT, foreign_buy_value_idr NUMERIC,
                    foreign_sell_value_idr NUMERIC, raw_json JSONB NOT NULL,
                    source_service TEXT NOT NULL,
                    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(trade_date,ticker))""")
                c.execute("""CREATE TABLE IF NOT EXISTS project_fabric.universe_scores (
                    trade_date DATE NOT NULL, ticker TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, scores_json JSONB NOT NULL,
                    available_factors INTEGER NOT NULL DEFAULT 0,
                    history_last_date DATE,
                    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    source_service TEXT NOT NULL,
                    PRIMARY KEY(trade_date,ticker,schema_version))""")

    def save_summary(self, payload: Any) -> int:
        """Only archive completed dates. During today's session keep live in cache."""
        today = datetime.now(WIB).date()
        prepared = []
        for row in _items(payload):
            ticker = str(row.get("StockCode") or row.get("stockCode") or row.get("ticker") or row.get("code") or "").strip().upper()
            stamp = _session_date(row, history=False)
            if not _CODE.fullmatch(ticker) or not stamp or stamp >= today:
                continue
            prepared.append((stamp, ticker,
                _num(row, "OpenPrice", "Open", "open"),
                _num(row, "High", "HighPrice", "high"),
                _num(row, "Low", "LowPrice", "low"),
                _num(row, "Close", "close"),
                _num(row, "Volume", "volume"),
                _num(row, "Value", "value"),
                _num(row, "Frequency", "frequency"),
                _num(row, "foreignBuyValue", "ForeignBuyValue"),
                _num(row, "foreignSellValue", "ForeignSellValue"),
                Jsonb(row), self.service))
        if not prepared:
            return 0
        # Cheap daily idempotency, avoiding 900 duplicate inserts every 5 min.
        with self._connect() as cn:
            with cn.cursor() as c:
                # Do not skip partially populated sessions.
                latest_day = max(x[0] for x in prepared)
                c.execute("SELECT count(*) FROM project_fabric.stock_summary_daily WHERE trade_date=%s",
                          (latest_day,))
                if int(c.fetchone()[0]) >= len({x[1] for x in prepared if x[0] == latest_day}):
                    return 0
                c.executemany("""INSERT INTO project_fabric.stock_summary_daily
                    (trade_date,ticker,open_price,high_price,low_price,close_price,
                     volume_shares,turnover_idr,frequency,foreign_buy_value_idr,
                     foreign_sell_value_idr,raw_json,source_service)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(trade_date,ticker) DO NOTHING""", prepared)
        return len(prepared)

    def save_history(self, ticker: str, payload: Any) -> int:
        ticker = ticker.strip().upper()
        if not _CODE.fullmatch(ticker):
            raise ValueError("Invalid IDX ticker")
        # _history filters out today's incomplete daily candle and invalid OHLCV.
        rows = _history(payload)
        if not rows:
            return 0
        raw_by_date = {
            str(row.get("date") or row.get("Date") or "")[:10]: row
            for row in _items(payload)
        }
        prepared = []
        for row in rows:
            raw = raw_by_date.get(row["date"], row)
            prepared.append((
                date.fromisoformat(row["date"]),ticker,
                row["open"],row["high"],row["low"],row["close"],row["volume"],
                _num(raw,"value","Value"),_num(raw,"frequency","Frequency"),
                _num(raw,"foreignBuyValue","ForeignBuyValue"),
                _num(raw,"foreignSellValue","ForeignSellValue"),
                Jsonb(raw),self.service,
            ))
        with self._connect() as cn:
            with cn.cursor() as c:
                c.executemany("""INSERT INTO project_fabric.stock_history_daily
                   (trade_date,ticker,open_price,high_price,low_price,close_price,
                    volume_shares,turnover_idr,frequency,foreign_buy_value_idr,
                    foreign_sell_value_idr,raw_json,source_service)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(trade_date,ticker) DO NOTHING""", prepared)
        return len(prepared)

    def summary_row(self, ticker: str) -> tuple[dict | None, date | None]:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""SELECT raw_json, trade_date FROM project_fabric.stock_summary_daily
                    WHERE ticker=%s ORDER BY trade_date DESC LIMIT 1""",(ticker,))
                row = c.fetchone()
        return (row[0],row[1]) if row else (None,None)

    def history_rows(self, ticker: str, limit: int = 120) -> list[dict]:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""SELECT raw_json FROM project_fabric.stock_history_daily
                  WHERE ticker=%s AND trade_date < (now() AT TIME ZONE 'Asia/Jakarta')::date
                  ORDER BY trade_date DESC LIMIT %s""",(ticker,min(500,max(1,limit))))
                rows=c.fetchall()
        return [r[0] for r in reversed(rows)]

    def latest_history_date(self, ticker: str) -> date | None:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("SELECT max(trade_date) FROM project_fabric.stock_history_daily WHERE ticker=%s",
                          (ticker,))
                return c.fetchone()[0]

    def compute_score(self, ticker: str) -> dict | None:
        ticker = ticker.strip().upper()
        history = self.history_rows(ticker)
        if len(history) < 30:
            return None
        features = history_features(ticker, {"items": history})
        if features["history_sessions"] < 30:
            return None
        # The history endpoint supplies real foreign BUY/SELL monetary values.
        # Prefer those to any approximate summary shares*close result.
        flow = institutional_flow(history[-1])
        summary, summary_date = self.summary_row(ticker)
        if flow["score"] is None and summary:
            flow = institutional_flow(summary)
        features["institutional_flow"] = flow
        scores = {k:features[k] for k in ("accumulation_persistence",
                  "breakout_readiness","institutional_flow","distribution_risk","entry_quality")}
        available = sum(1 for s in scores.values() if s.get("score") is not None)
        hist_date = date.fromisoformat(features["history_last_date"])
        trade_date = max(filter(None,(hist_date,summary_date)))
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""INSERT INTO project_fabric.universe_scores
                    (trade_date,ticker,schema_version,scores_json,available_factors,
                     history_last_date,source_service)
                    VALUES (%s,%s,1,%s,%s,%s,%s)
                    ON CONFLICT(trade_date,ticker,schema_version) DO UPDATE SET
                       scores_json=EXCLUDED.scores_json,
                       available_factors=EXCLUDED.available_factors,
                       history_last_date=EXCLUDED.history_last_date,
                       computed_at=now(),source_service=EXCLUDED.source_service""",
                    (trade_date,ticker,Jsonb(scores),available,hist_date,self.service))
        return {"ticker":ticker,"scores":scores,"history_last_date":str(hist_date),
                "market_date":str(trade_date),"available_factors":available}

    def latest_score(self, ticker: str) -> dict | None:
        ticker = ticker.strip().upper()
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""SELECT scores_json,history_last_date,trade_date,computed_at
                  FROM project_fabric.universe_scores WHERE ticker=%s AND schema_version=1
                  ORDER BY trade_date DESC LIMIT 1""",(ticker,))
                row=c.fetchone()
        if not row:
            return None
        scores,hist_date,market_date,computed_at=row
        return {"ticker":ticker,"scores":scores,
                "history_last_date":hist_date.isoformat() if hist_date else None,
                "summary_observed_at": market_date.isoformat(),
                "market_date":market_date.isoformat(),
                "computed_at":computed_at.isoformat(),
                "source":"neon-market-warehouse","archived":True,
                "available":any(isinstance(v,dict) and v.get("score") is not None
                                for v in scores.values())}

    def ticker_count(self) -> int:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("SELECT count(distinct ticker) FROM project_fabric.stock_history_daily")
                return int(c.fetchone()[0])

    def rebuild_available(self, maximum_tickers: int = 250) -> dict:
        with self._connect() as cn:
            with cn.cursor() as c:
                c.execute("""SELECT ticker FROM project_fabric.stock_history_daily
                    GROUP BY ticker HAVING count(*) >= 30
                    ORDER BY ticker LIMIT %s""",(maximum_tickers,))
                tickers=[r[0] for r in c.fetchall()]
        success=0
        failed=[]
        for ticker in tickers:
            try:
                if self.compute_score(ticker):
                    success+=1
            except Exception:
                failed.append(ticker)
        return {"eligible_tickers":len(tickers),"saved_scores":success,
                "failed_tickers":failed}
