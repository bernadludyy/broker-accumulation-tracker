# Tape / Footprint Bot 1

Send `/tape ELTY` to the owner's private Telegram bot. `/tape ELTY 60`
filters to the last 60 minutes relative to the latest available print, even
outside trading hours. Aliases: `TAPE`, `/footprint`, `/haka`, `/dna`.
The result is one PNG dashboard plus plain-text explanations. If photo delivery
or rendering fails, text remains available. Existing `/flow ELTY` stays EOD.

## Data and request budget

Uses existing ZAPI key and gateway: `finance:pluang/tradebook` (ALL), `summary`,
`chart`, `orderbook`, then at most 3 pages of `running-trades` with `minLot=0`
and both BUY/SELL sides. No scheduler, AI calls or full-market scan. At most
7 gateway operations; existing bounded GET retries can increase HTTP hits.
Repeated cursors stop pagination. Sequences deduplicate prints across pages;
identical prints without sequence IDs remain separate because they may be real.

Provider documentation: https://zapi.ink/api/finance/pluang?ep=running-trades

## Calculations and scope

* 1 lot = 100 shares; classified value = price × lots × 100.
* HAKA = provider BUY, HAKI = provider SELL. Unknown sides are skipped.
* Default totals/footprint/VWAP use the tradebook snapshot when available.
  VWAP = classified side value ÷ classified side shares.
* POC is the price with the largest **classified side value**, not total-volume POC.
* Tape CVD = cumulative BUY value − SELL value, starting at zero at the oldest
  captured print. It is always labelled partial; never presented as full-day CVD.
* `60` uses tape-only totals and footprint so windowed and daily scopes cannot mix.
* Net-buy minutes count minutes with transactions in the captured sample only.
* Large prints ≥Rp20 million and size buckets are prints, not reconstructed orders.
* Orderbook shows current resting queues, not executed transaction flow.
* Tradebook, quote and tape are separate requests and can differ in timing.
* Time-only tape rows do not prove the trading date. The chart's explicit date
  supplies a labelled session reference only. Old dates and unknown dates remain visible.
* Explicit nonregular boards, invalid prices/lots/sides and invalid times are
  excluded. Provider does not guarantee full separation of crossing/negotiated
  trades; this limit is visible on every report.

Rule labels are descriptive, not a proprietary DNA score: ≥60% buy value means
buy dominance; ≤40% means sell dominance. With matching price direction >0.5%
from open, label markup/pressure. ≥65% buy or ≤35% sell with price within 0.5%
of open suggests possible passive absorption, explicitly described as a hypothesis.
Broker identity, insider identity, original order reconstruction and iceberg/wall
scores cannot be established and are not fabricated.

## Validation

Offline tests cover arithmetic, partial-vs-snapshot scope, pagination, repeated
cursors, stable-ID dedupe, unknown sides, dates/time zones, authorization, grammar,
photo confirmation/fallback and PNG validity. `bot1_tape_smoke.yml` runs on an
explicit `deploy(bot1-tape):` main commit or manual workflow invocation; it executes
`/tape ELTY` through the real command parser, validates real provider trades and
PNG, then verifies Telegram API photo and text receipts. Artifacts contain market
metrics and receipt message IDs only, no credentials.
