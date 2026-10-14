# Buddy: /eod refresh (manual EOD warehouse sync)

Buddy Bot 1 supports `/eod refresh`, `EOD REFRESH`, and `/eod status` in the authorized private Telegram chat.

- `/eod status`: read-only date and count from Neon `project_fabric.stock_summary_daily` (0 ZAPI).
- `/eod refresh`: after 17:00 WIB on a weekday, check Neon first. If the target trade date already has sufficiently complete data, return immediately with 0 ZAPI. Otherwise request one exact-date IDX `stock-summary` snapshot, verify date/coverage/uniqueness, store it durably in the existing warehouse, then update the shared raw summary cache.
- The gateway may retry transient ZAPI errors, so actual upstream attempt counts can exceed 1 on failures. No API calls before 17:00 WIB, on weekends, or when the archive is already complete.
- The existing Telegram Query Worker (`.github/workflows/telegram_query.yml`) processes the request using **existing Bot 1** `ZPI_API_KEY` and `PROJECT_FABRIC_DATABASE_URL` secrets. No new GitHub token, cross-repo trigger or recurring job is needed.
- The command uses the same worker and concurrency queue as all other Buddy commands; GitHub Actions minutes accrue only when a Telegram command is received. There is no new schedule.
- `SETUP RANK` reads `MAX(trade_date)` from `project_fabric.stock_summary_daily`, so it picks up the completed session after this command succeeds.
- This updates **stock-summary EOD for Buddy/SETUP RANK and shared Neon consumers**; Bot 4's separate `universe_scores` and EOD entry-plan finalizer continue on their own workflow. The Telegram reply explicitly distinguishes them.

## Safety

The command checks `authorized(chat_id)` even within `command_result`, and the worker already rejects updates from other chats. The post-close gate, exact trade-date check, a baseline completeness threshold, duplicate ticker checks, and API result metadata checks prevent partial/current-session or yesterday's data from being labeled today's EOD.

An error does **not** redate old rows. Test with `python -m unittest tests.test_eod_refresh -v` plus repository regression CI. Do not run production refresh during CI.

## Example

```
/eod status
/eod refresh
SETUP RANK
```
