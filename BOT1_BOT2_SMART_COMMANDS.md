# Bot 1 & Bot 2 — Smart Ranking V2

Introduced five-factor, cache-only confirmation for Bot 1 and Bot 2 using
Project Fabric / Neon Universe Intelligence V1. **No new ZAPI calls, no extra
scheduled GitHub Actions, no independent broker claims, no auto-trading.**

## Telegram commands

| Command | Result |
| --- | --- |
| `/fund ADRO`, `ADRO FUND`, or `BOT1 FUND ADRO` | Read-only Neon fundamental score versus adaptive sector benchmark; no ZAPI call |
| `/full ADRO`, `ADRO FULL`, or `BOT1 FULL ADRO` | Fundamental overlay + compact broker/Universe evidence + unchanged Foreign Entry and technical hard gates |
| `BOT1 SMART ADRO` | Compact broker 5D/20D net values, visible broker-flow label, Universe score and distribution warning |
| `BOT2 SMART` | Top 10 ranked among existing Bot 2 market prefilter winners |
| `BOT2 SMART 20` | Top 20 ranked among the same winners; cap 20 |
| `/intel ADRO` or `UNIVERSE ADRO` or `ADRO INTEL` | Five detailed Universe scores with coverage and historical freshness |
| `ADRO FOREIGN ENTRY` | Existing actual 5-day foreign+entry hard gates; adds Universe priority and risks if data exists |
| `FOREIGN ENTRY` | Existing watchlist scan now sorted by risk-adjusted priority, not just raw entry score, when 3+ valid Universe factors exist |
| `FOREIGN ENTRY BOT2` | Same prefilter and entry gates; eligible shortlist sorted by risk-adjusted priority |
| `ADRO BOT2 DATA` | Existing rank/correction output plus cached Universe context |
| `BOT2 MARKET` | Existing market intersection/ranking **unchanged**; use SMART to quality-rank |

All other legacy commands (TOP FREQ, NET FOREIGN, KOREKSI, SMARTMONEY,
FIBO, broker codes, /add, /refresh, etc.) continue unchanged.

## Fundamental sector overlay

The fundamental score uses an adaptive benchmark:

- exact provider sector when at least 8 issuers are available;
- 50/50 exact-sector and macro-sector percentiles for 4–7 issuers;
- macro sector below 4 issuers, then IDX global if the macro group is also too small.

Metrics are winsorized at peer P5/P95 and converted to sector percentiles.
Default weights are Valuation 20%, Quality 30%, Growth 25%, Safety 15% and
three-year Consistency 10%. Financials exclude conventional current ratio
and D/E safety comparisons; technology, commodity and property policies use
their documented sector-specific weights. Missing inputs are re-normalized,
but coverage below 70% returns `DATA TERBATAS`.

The ranking overlay is +5 for scores >=75, +2 for 60–74, 0 for 45–59 and -8
below 45. Repeated negative profit and operating cash flow produces a -12
structural warning and blocks the *fundamental swing recommendation*. This
does not bypass or modify any existing Bot 1 hard gate.

`BOT1 FULL` now prints one combined decision panel. Its final ranking priority
is the existing Entry+Universe priority plus the discrete fundamental overlay,
clamped to 0–100. It also shows the unchanged technical components: Broker
Flow /35, Trend /25, Momentum /15, Volume /10 and Risk/Reward /15. A strong
fundamental score can improve ranking but can never turn a failed technical,
foreign, synchronization or R:R hard gate into PASS.

The Telegram FULL output is intentionally compact and separated by blank lines
into Decision, Score, Technical, Fundamental, Foreign and Setup blocks. Detailed
subreports are not duplicated; use `/fund TICKER`, `/intel TICKER`, or
`TICKER FOREIGN ENTRY` when drill-down is needed.

## How ranking works

Universe weighted score (re-normalize *available* weights; require >= 3
independent valid factors):
- Accumulation Persistence 25% (price-volume proxy, NOT broker inventory)
- Breakout Readiness 25%
- Institutional Foreign Flow 20% (only explicitly reported rupiah value; estimated shares-times-close excluded from rank)
- Distribution Risk inverse 15% (100 - risk)
- Entry Quality 15%

New priority = 75% existing score + 25% Universe score, minus 12 points
if distribution risk >= 70, capped 0..100.

**Bot 1 broker summary does not reinterpret broker buy/sell as official
holdings.** Existing Foreign Entry scoring and hard gates (net 5D positive,
3/5 days foreign net buy, net 1D positive, base score, PASS setup, R:R>=1.5,
synchronized market history) are unchanged. Missing or stale Universe data
never prevents a verified legacy signal from being considered.

**Bot 2** maintains Top 50 frequency + Top 30 foreign ranking + -0.3% to
-5% open-to-close correction and all Bot 1 entry gates. BOT2 SMART is an
additional rank view, not a new discovery universe or buy command.

## Quality controls

- Failed Neon read => `DATA TERBATAS` and no extra ZAPI calls.
- Historic OHLCV features are ignored if their date is older than 8 calendar
  days relative to the requested snapshot.
- Foreign shares x close remains marked as **estimated**, and never counts as
  confirmed reported foreign rupiah values.
- At least three fresh features are needed before showing a Universe ranking
  score; 1 or 2 produce `N/A` instead of a fictitious 0.
- Ranking is not win rate and no return/accuracy guarantee exists.
- Telegram group settings and Bot 4 alert format are unaffected.

Use `/help` for the current command list.

