# Bot 1 TradingAgents v0.7.1 - manual review pilot

This module is an isolated, OFF-BY-DEFAULT research layer. No Telegram integration, no GitHub Actions cron, no auto-trading, no database migrations, no ZAPI requests, and no changes to the existing Bot 1 dispatcher or workflow.

## Architectural choice

The original TradingAgentsGraph for stocks reads from external Yahoo/SEC sources. That would violate the requested cache-only IDX setup. Instead this pilot imports the OFFICIAL TradingAgents v0.7.1 LLM client factory and creates three narrowly scoped analyst prompts: Bull Researcher, Bear Researcher, Risk Manager. It is not the full upstream graph or its backtest.

Neon sources: project_fabric.stock_summary_daily and project_fabric.stock_history_daily. Their verified completed EOD bars are merged by date (daily summary overrides duplicate older history). Any internal gap over 7 calendar days is rejected. On that specific continuity failure, the Telegram adapter performs one bounded ticker-history repair: it checks the six-hour shared Project Fabric cache first and makes at most one logical ZAPI fetch when the cache is absent, archives completed sessions, then reruns every strict validation. Summary staleness, invalid EOD, and other failures never trigger repair. A single REPEATABLE READ READ ONLY transaction limits each validation read to a completed EOD day. SQLite broker_daily is also read-only.

Point-in-time guards require at least 30 price sessions, matching completed summary, a recent broker snapshot with >=3 sessions, positive indicative broker net activity, bullish market regime, price above MA20, valid volume, and RSI less than 80. Missing data causes WAIT, not invented values. Broker net buy is transaction activity, NOT holdings. Financial statements are omitted pending publication-time verification.

## Local dry run - 0 AI calls

Install Python 3.12 dependencies from requirements_github.txt, supply PROJECT_FABRIC_DATABASE_URL from a read-only Neon role, and use a recent local copy of broker_tracker.db.

    python -m ai_decision_engine --ticker BBRI
    python -m ai_decision_engine --ticker BBRI --as-of 2026-10-28

Dry run reports snapshot, gate, action WAIT and ai_calls 0; it does NOT generate an AI trade recommendation.

## Optional manual API experiment

Separately install requirements_ai_decision.txt, which pins the official v0.7.1 upstream commit. Provide OPENAI_API_KEY as an environment secret, as well as BOT1_AI_ENABLED=1 and BOT1_AI_REMOTE=1. Then, and only then:

    python -m ai_decision_engine --ticker BBRI --remote

Remote mode: at most 3 LLM requests per run, up to 350 generated tokens/request, 0 retries. These are separately billed OpenAI API calls. The software does not provide a global spend cap or deduplication across repeated manual runs. Never enable in scheduled production Actions.

## Test and release gates

    python -m unittest discover -s tests -p test_ai_decision_engine.py -v
    python -m unittest discover -s tests -v

Then review CI on draft PR, check Neon dry-run data freshness using a read-only credential, and measure token consumption for one remote ticker. Compare decisions with Bot 1 rule-based analysis and run out-of-sample paper trading with fees/slippage. Do not merge, deploy, or enable automatic alerts before validation.

Known limits: No intraday data, no full TradingAgents graph, no direct trade execution, no portfolio position sizing, and no proof of profitability.


## Telegram commands (owner's private chat only)
```
AI STATUS
AI CHECK BBRI
/ai ADRO
BOT1 AI CHECK DSSA
AI RUN BBRI
/ai_run BBRI
```

- `AI CHECK` and `/ai TICKER` use the latest completed EOD from Neon + broker SQLite and make 0 LLM requests. Normally ZAPI usage is 0. If and only if the ticker's history is missing, disconnected, or has a gap over seven calendar days, the command repairs that one ticker through the shared cache (0 ZAPI on a cache hit, otherwise normally 1 ZAPI request) and validates again. The response reports the actual count and explicitly says NOT RUN so it is not confused with an AI decision.
- `AI RUN` and `/ai_run TICKER` require Repository Variables `BOT1_AI_ENABLED=1` and `BOT1_AI_REMOTE=1` plus Secret `OPENAI_API_KEY`. Otherwise they return a disabled message with **no paid calls**.
- The existing Telegram Query Worker checks whether the incoming request is from `TELEGRAM_CHAT_ID` before installing the pinned TradingAgents dependency. No heavy dependency is installed for other Bot 1 commands.
- `AI RUN` sends the output to the existing authorized Telegram destination only. A single command can make up to 3 paid requests. Repeated commands may incur further costs; no daily price cap has been implemented.
- No additional schedule, market refresh, Neon write, automatic order, or default paid activation was added. Non-AI Bot 1 commands remain on their current route.
- Test before enabling paid RUN: use `AI CHECK` with a known EOD and inspect the gateway freshness, broker dates, pricing and hard-gate behavior. Optional CLI smoke tests with v0.7.1 client belong in a disposable environment.

## Tests
```
python -m unittest discover -s tests -p 'test_ai_telegram.py' -v
python -m unittest discover -s tests -v
```
