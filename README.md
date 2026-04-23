# Claude Code Usage Dashboard — Fleet Edition

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

**Pro and Max subscribers get a progress bar. This gives you the full picture — across every account you run.**

This is a fork of [phuryn/claude-usage](https://github.com/phuryn/claude-usage) that adds multi-account support: track several `CLAUDE_CONFIG_DIR` profiles in one dashboard, compare them side-by-side, and fire webhooks when any of them approaches its 5-hour block limit. See [CHANGELOG.md](CHANGELOG.md) for the list of additions.

Claude Code writes detailed usage logs locally — token counts, models, sessions, projects — regardless of your plan. This dashboard reads those logs and turns them into charts and cost estimates. Works on API, Pro, and Max plans.

![Claude Usage Dashboard](docs/screenshot.png)

**Created by:** [The Product Compass Newsletter](https://www.productcompass.pm)

---

## What this tracks

Works on **API, Pro, and Max plans** — Claude Code writes local usage logs regardless of subscription type. This tool reads those logs and gives you visibility that Anthropic's UI doesn't provide.

Captures usage from:
- **Claude Code CLI** (`claude` command in terminal)
- **VS Code extension** (Claude Code sidebar)
- **Dispatched Code sessions** (sessions routed through Claude Code)

**Not captured:**
- **Cowork sessions** — these run server-side and do not write local JSONL transcripts

---

## Requirements

- Python 3.8+
- No third-party packages — uses only the standard library (`sqlite3`, `http.server`, `json`, `pathlib`)

> Anyone running Claude Code already has Python installed.

## Quick Start

No `pip install`, no virtual environment, no build step.

### Windows
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python cli.py dashboard
```

### macOS / Linux
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python3 cli.py dashboard
```

---

## Usage

> On macOS/Linux, use `python3` instead of `python` in all commands below.

```
# Scan JSONL files and populate the database (~/.claude/usage.db)
python cli.py scan

# Show today's usage summary by model (in terminal)
python cli.py today

# Show all-time statistics (in terminal)
python cli.py stats

# Check per-account 5h block usage and fire webhook alerts
python cli.py alerts

# Scan + open browser dashboard at http://localhost:8080
python cli.py dashboard

# Custom host and port via environment variables
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

# Scan a custom projects directory
python cli.py scan --projects-dir /path/to/transcripts
```

The scanner is incremental — it tracks each file's path and modification time, so re-running `scan` is fast and only processes new or changed files.

By default, the scanner checks both `~/.claude/projects/` and the Xcode Claude integration directory (`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects/`), skipping any that don't exist. Use `--projects-dir` to scan a custom location instead.

---

## How it works

Claude Code writes one JSONL file per session to `~/.claude/projects/`. Each line is a JSON record; `assistant`-type records contain:
- `message.usage.input_tokens` — raw prompt tokens
- `message.usage.output_tokens` — generated tokens
- `message.usage.cache_creation_input_tokens` — tokens written to prompt cache
- `message.usage.cache_read_input_tokens` — tokens served from prompt cache
- `message.model` — the model used (e.g. `claude-sonnet-4-6`)

`scanner.py` parses those files and stores the data in a SQLite database at `~/.claude/usage.db`.

`dashboard.py` serves a single-page dashboard on `localhost:8080` with Chart.js charts (loaded from CDN). It auto-refreshes every 30 seconds and supports model filtering with bookmarkable URLs. The bind address and port can be overridden with `HOST` and `PORT` environment variables (defaults: `localhost`, `8080`).

---

## Cost estimates

Costs are calculated using **Anthropic API pricing as of April 2026** ([claude.com/pricing#api](https://claude.com/pricing#api)).

**Only models whose name contains `opus`, `sonnet`, or `haiku` are included in cost calculations.** Local models, unknown models, and any other model names are excluded (shown as `n/a`).

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|------------|-----------|
| claude-opus-4-6 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-sonnet-4-6 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-5 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |

> **Note:** These are API prices. If you use Claude Code via a Max or Pro subscription, your actual cost structure is different (subscription-based, not per-token).

---

## Multi-account setup

If you run Claude Code under more than one profile (different subscriptions, separate accounts per client, etc.), point each profile at its own `CLAUDE_CONFIG_DIR` and list them in `~/.claude/accounts.json`:

```json
{
  "accounts": [
    {"name": "acct1", "path": "~/.claude-acct1", "plan": "max_20x"},
    {"name": "acct2", "path": "~/.claude-acct2", "plan": "max_5x"},
    {"name": "acct3", "path": "/mnt/c/Users/me/.claude-acct3", "plan": "pro"},
    {"name": "acct4", "path": "/mnt/c/Users/me/.claude-acct4", "plan": "pro"}
  ],
  "thresholds": { "warn": 0.75, "critical": 0.95 },
  "webhooks": [
    {"url": "https://ntfy.sh/your-topic-here", "on": ["warn", "critical"]}
  ]
}
```

Copy `accounts.json.example` for a starting point. When `~/.claude/accounts.json` is absent the fork behaves exactly like upstream — one "default" account pointing at `~/.claude/projects/`. `accounts.json` is in `.gitignore`.

- **Paths** accept `~` expansion and both POSIX (`/home/me/...`) and Windows (`C:\Users\me\...`) formats. WSL users can mix WSL-native paths and `/mnt/c/...` paths in the same config — the scanner normalizes via `pathlib.Path`.
- **`extra_paths`** (optional per-account list) scans additional transcript roots under the same account name. macOS users who run Claude Code from both the CLI and the Xcode integration can list `~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig` here to pull in both sources.
- **Plans** are `api`, `pro`, `max_5x`, `max_20x`, or omitted. Plan determines the denominator for the 5-hour block progress bars (`pro ≈ 44k`, `max_5x ≈ 88k`, `max_20x ≈ 220k` tokens per 5h block — community estimates; `api` and omitted have no limit and are never alerted on).
- **Thresholds** drive the header strip colors (green below `warn`, yellow at `warn`, red at `critical`) and whether `alerts` fires a webhook.
- **Webhooks** get a JSON POST with `{account, level, usage_fraction, block_reset_at}` when the account first crosses each threshold. Alert state is stored in an `alert_state` SQLite table so re-runs don't re-spam; downgrading below the threshold silently resets so a later re-cross fires again.

Running `python cli.py scan` walks every configured account and prints a summary table; `python cli.py dashboard` shows all accounts by default with a filter dropdown and a Compare Accounts tab. Schedule `python cli.py alerts` in cron (Linux/macOS) or Task Scheduler (Windows) to get webhooks between manual runs.

**Renaming an account:** if you change an account's `name` in `accounts.json` while keeping the same path, the old name's historical rows stay in the DB (so they still show up in filtered views) and the scanner warns on the next run. Click **Rescan** in the dashboard or delete `~/.claude/usage.db` to rebuild under only the current names.

---

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Parses JSONL transcripts, writes to `~/.claude/usage.db`; `scan_all()` walks every configured account |
| `dashboard.py` | HTTP server + single-page HTML/JS dashboard with account filter and Compare tab |
| `cli.py` | `scan`, `today`, `stats`, `alerts`, `dashboard` commands |
| `config.py` | Loads `accounts.json`; falls back to single-account defaults when missing |
| `alerts.py` | Per-account block usage, threshold crossings, webhook firing with dedup |
| `accounts.json.example` | Reference config with 4 profiles and a webhook entry |
