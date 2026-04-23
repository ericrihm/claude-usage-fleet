# Changelog

## Fork (claude-usage-fleet) — 2026-04-23

Divergence from upstream [phuryn/claude-usage](https://github.com/phuryn/claude-usage):

- **Multi-account config** via `~/.claude/accounts.json` (stdlib JSON, no PyYAML). Missing file → behaves like upstream on a single "default" account.
- **Schema migration**: both `sessions` and `turns` gain an `account` column, backfilled to `'default'` for existing rows. The `sessions` primary key is rewritten to `(account, session_id)` so the same session_id can appear under two different profiles. Legacy DBs are migrated once via a `CREATE TABLE / INSERT SELECT / RENAME` transaction.
- **`scan_all(config)`** iterates every configured account, tags each row, and prints a per-account summary table. Paths are asserted disjoint so `processed_files` can't collide across accounts.
- **Dashboard account filter**: new `Account` dropdown, `?account=<name>` URL parameter, `/api/accounts` and `/api/compare?window=5h|24h|7d` endpoints, and a `Compare Accounts` tab with a stacked bar chart.
- **Header progress strip**: one pill per configured account showing tokens used against the 5-hour block limit, colored against the configured thresholds.
- **Webhook alerts**: new `alerts.py` + `python cli.py alerts` command. Fires one JSON POST per threshold crossing with `{account, level, usage_fraction, block_reset_at}`. Dedup state lives in a new `alert_state` table. Plan limits (`pro` 44k / `max_5x` 88k / `max_20x` 220k tokens per 5h block) are community-calibrated approximations from the [Maciek-roboblog Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor).
- **Tests**: adds `tests/test_multi_account.py` and `tests/test_alerts.py` alongside upstream's existing suite. All tests use `tempfile.TemporaryDirectory` — never touch the real `~/.claude/usage.db`.

---

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
