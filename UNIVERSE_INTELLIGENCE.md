# IDX Universe Intelligence V1

This is NOT a new bot, workflow, cron job, or Telegram alert.
The existing Bot 4 collector is the producer for the same Project Fabric / Neon shared-v1 namespace.

## Five research-inspired feature families

1. Accumulation Persistence (Pulse-CLI): 10-session up-volume and rising-low proxy. Does not assert broker accumulation.
2. Breakout Readiness (NSE-Breakout-Monitor): 5-session range contraction against prior 5 sessions, proximity to prior 20-session high.
3. Institutional Flow (Stockbit MCP): IDX foreign buy/sell values, or share quantities multiplied by closing price with an ESTIMATED source label. Broker concentration is null until independent broker data is available.
4. Distribution Risk (SmartMoneyTracker): weak close on high volume and adverse five-session OBV direction. HIGH score means HIGHER risk.
5. Entry Quality (NSE Scanner): current completed close relative to 20-session mean in ATR units, and distance to recent support. It is NOT a trade recommendation; VWAP and reward/risk are null when data is missing.

Each computed score is 0-100 or null when unavailable. Historical scores use completed IDX daily candles only; there is no look-ahead to partial current-session data.

## Producer

Bot 4 publishes a single summary-level index whenever the existing market collector requests full IDX stock-summary; its existing stock-history fetch opportunistically publishes history-derived intelligence per ticker.

There are NO additional upstream ZAPI calls, requests for historical tickers not already fetched, or extra GitHub Actions / cron invocations. All optional writes are best-effort and cannot change the Bot 4 alert pipeline or filter eligibility.

## Consumers

Both repositories share identical Python module project_fabric/universe_intelligence.py.

In the broker-accumulation-tracker repository, which also hosts Bot 2:
    import tracker
    intel = tracker.peek_universe_intelligence("ABCD")

The read is cache-only (up to two Neon cache keys). Existing Telegram output, universe ranking and entry thresholds remain unchanged.

## Cache contract

Summary key endpoint: universe:intelligence:summary:v1, params scope=IDX, max age 900s.
History key endpoint: universe:intelligence:history:v1, params ticker=CODE, max age 259200s.
Both use DataGateway's canonical key hash, namespace shared-v1, and schema_version 1.
Source/freshness fields must be checked by consumers before they claim live confirmation.

Only tickers with histories fetched by the existing Bot 4 pipeline receive all 4 daily-history features. Missing history must be displayed as unavailable, not interpreted as a weak/bearish signal.

## Rollout

Feature branches and PR CI first. Automatic publication begins only after merge and the next valid market workflow run; there is no forced weekend scan.
No change to Bot 4 Telegram formatting design, which remains deferred.

Research references:
- https://github.com/sukirman1901/Pulse-CLI
- https://github.com/brrrezy/NSE-Breakout-Monitor
- https://github.com/INo-xious/stockbit-mcp
- https://github.com/RTsien/SmartMoneyTracker
- https://github.com/AtharvKhanna/nse-scanner
