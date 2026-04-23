"""
alerts.py - Threshold-based webhook alerts for per-account block usage.

A "block" in Claude Code Max/Pro terminology is a rolling 5-hour window measured
from the first assistant turn. We approximate this as a trailing 5-hour window
from now — close enough for alerting purposes; the exact block boundary would
require tracking window-start state per account, which is more machinery than
is worth it for warn/critical fires.

Plan token limits are approximations derived from community monitoring tools
(Maciek-roboblog/Claude-Code-Usage-Monitor) — Anthropic does not publish
official numbers. These are total tokens (input + output + cache) per 5h block.
"""

import json
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta


def _iso_cutoff(window_hours):
    """Format a cutoff timestamp the same way JSONL records do: '...T...Z'.

    SQLite's datetime('now') returns 'YYYY-MM-DD HH:MM:SS' (space separator),
    but transcripts store 'YYYY-MM-DDTHH:MM:SSZ'. Comparing the two as TEXT
    is wrong: 'T' (0x54) sorts after ' ' (0x20), so turns from the same UTC
    day slip through a naive `timestamp >= datetime('now', '-Nh')` filter.
    Generate the cutoff in Python so the comparison lines up.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

# Plan limits: total tokens per 5-hour block. Source: Maciek-roboblog monitor.
# These are best-effort community-calibrated estimates — revise if Anthropic
# publishes official figures.
PLAN_LIMITS = {
    "pro":      44_000,
    "max_5x":   88_000,
    "max_20x":  220_000,
    # api and None are intentionally absent — callers check plan before limiting.
}

BLOCK_WINDOW_HOURS = 5


def compute_block_tokens(conn, account, window_hours=BLOCK_WINDOW_HOURS):
    """Sum of input+output+cache_read+cache_creation tokens in the last N hours
    for one account. 0 if there's no recent activity."""
    row = conn.execute("""
        SELECT COALESCE(SUM(
            input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
        ), 0) as total
        FROM turns
        WHERE account = ? AND timestamp >= ?
    """, (account, _iso_cutoff(window_hours))).fetchone()
    return int(row[0] or 0)


def compute_block_usage(conn, account, plan, window_hours=BLOCK_WINDOW_HOURS):
    """Return fraction-of-plan used. None if the plan has no limit (api, null)."""
    limit = PLAN_LIMITS.get(plan)
    if not limit:
        return None
    tokens = compute_block_tokens(conn, account, window_hours)
    return tokens / limit


def _ensure_alert_state_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_state (
            account         TEXT PRIMARY KEY,
            last_level      TEXT,
            last_fired_at   TEXT
        )
    """)
    # Per-webhook delivery log — one row per (account, level, url) tuple that
    # has been successfully delivered. Lets us retry only the destinations
    # that failed without re-spamming the ones that already succeeded.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_deliveries (
            account         TEXT NOT NULL,
            level           TEXT NOT NULL,
            url             TEXT NOT NULL,
            delivered_at    TEXT NOT NULL,
            PRIMARY KEY (account, level, url)
        )
    """)
    conn.commit()


def _undelivered_webhooks(conn, config, account, level):
    """Return the subset of configured webhooks that still need delivery for
    this (account, level) tuple."""
    already = {
        r[0] for r in conn.execute(
            "SELECT url FROM alert_deliveries WHERE account = ? AND level = ?",
            (account, level),
        ).fetchall()
    }
    return [wh for wh in config.get("webhooks", [])
            if level in wh.get("on", ["warn", "critical"])
            and wh["url"] not in already]


def _record_delivery(conn, account, level, url):
    conn.execute("""
        INSERT OR REPLACE INTO alert_deliveries (account, level, url, delivered_at)
        VALUES (?, ?, ?, ?)
    """, (account, level, url, datetime.now(timezone.utc).isoformat()))


def _reset_deliveries_below(conn, account, level):
    """When an account drops below warn, clear all delivery records so a
    future re-cross fires fresh notifications to every webhook."""
    conn.execute(
        "DELETE FROM alert_deliveries WHERE account = ?", (account,),
    )


def _level_for(fraction, thresholds):
    if fraction is None:
        return None
    if fraction >= thresholds["critical"]:
        return "critical"
    if fraction >= thresholds["warn"]:
        return "warn"
    return None


def _block_reset_at(now=None):
    """Approximate reset of the trailing 5h block — 5 hours from now."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(hours=BLOCK_WINDOW_HOURS)).isoformat()


def _post_webhook(url, payload, timeout=5):
    """Post JSON to a webhook. Returns (ok, status_or_error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "claude-usage-fleet"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except urllib.error.URLError as e:
        return False, f"url {e.reason}"
    except Exception as e:
        return False, f"err {e}"


def _fire(webhooks, account, level, fraction):
    """Post the alert to each webhook in the supplied list.

    The caller (check_and_fire) pre-filters to only the URLs that still need
    delivery for this (account, level), so this function just does the IO
    and returns per-webhook results.
    """
    payload = {
        "account":         account,
        "level":           level,
        "usage_fraction":  round(fraction, 4),
        "block_reset_at":  _block_reset_at(),
    }
    results = []
    for wh in webhooks:
        ok, info = _post_webhook(wh["url"], payload)
        results.append({"url": wh["url"], "ok": ok, "info": info})
    return payload, results


def check_and_fire(config, db_path, quiet=True):
    """Iterate configured accounts; fire one webhook per account that just
    crossed into a new level. Returns a list of (account, level, results)
    tuples describing what was fired.

    Runs the scanner migration first so a legacy (pre-fork) usage.db gets its
    `account` column added before we query it — otherwise `python cli.py
    alerts` against an upstream-shaped DB would crash with 'no such column:
    account'.
    """
    import scanner
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    _ensure_alert_state_table(conn)

    fired = []
    thresholds = config["thresholds"]
    try:
        for acct in config["accounts"]:
            plan = acct.get("plan")
            if plan in (None, "api"):
                continue
            fraction = compute_block_usage(conn, acct["name"], plan)
            if fraction is None:
                continue

            level = _level_for(fraction, thresholds)

            prev = conn.execute(
                "SELECT last_level FROM alert_state WHERE account = ?",
                (acct["name"],),
            ).fetchone()
            prev_level = prev[0] if prev else None

            # Dedup lives in two layers:
            #   * alert_state.last_level is the "I've been in this level"
            #     marker — only flips forward once at least one delivery
            #     succeeds (so we don't look like we handled a level we
            #     couldn't deliver for anyone).
            #   * alert_deliveries is per-webhook — a later tick retries
            #     only the URLs that haven't been marked delivered yet.
            rank = {None: 0, "warn": 1, "critical": 2}
            is_upgrade = level and rank[level] > rank[prev_level]
            is_stuck_retry = (
                level and rank[level] == rank[prev_level] and prev_level
            )

            if is_upgrade or is_stuck_retry:
                pending = _undelivered_webhooks(conn, config, acct["name"], level)
                if not pending:
                    # No webhook subscribes to this level (empty list or all
                    # already delivered). On a real upgrade, still advance
                    # last_level so the state machine doesn't loop forever
                    # treating it as "still in retry" — but don't record a
                    # phantom fire attempt.
                    if is_upgrade:
                        conn.execute("""
                            INSERT INTO alert_state (account, last_level, last_fired_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT(account) DO UPDATE SET
                                last_level = excluded.last_level,
                                last_fired_at = excluded.last_fired_at
                        """, (acct["name"], level,
                              datetime.now(timezone.utc).isoformat()))
                        conn.commit()
                    # Fall through to the next account.
                    continue
                payload, results = _fire(pending, acct["name"], level, fraction)
                for r in results:
                    if r.get("ok"):
                        _record_delivery(conn, acct["name"], level, r["url"])
                any_ok = any(r.get("ok") for r in results)
                if any_ok or is_upgrade:
                    # First delivery success OR first time noticing the
                    # crossing — advance last_level. Keeps subsequent
                    # ticks in the "retry only pending" branch.
                    new_level_state = level if any_ok else prev_level
                    conn.execute("""
                        INSERT INTO alert_state (account, last_level, last_fired_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(account) DO UPDATE SET
                            last_level = excluded.last_level,
                            last_fired_at = excluded.last_fired_at
                    """, (acct["name"], new_level_state,
                          datetime.now(timezone.utc).isoformat()))
                conn.commit()
                fired.append((acct["name"], level, results))
                if not quiet:
                    label = "alert" if any_ok else "alert (pending retries)"
                    print(f"  {label}: {acct['name']} -> {level} ({fraction:.0%})")
            elif prev and rank.get(level, 0) < rank[prev_level]:
                # Downgrade (critical -> warn, warn -> None, or critical ->
                # None). Clear delivery history for any level strictly higher
                # than the new state so a re-cross fires fresh. Also reset
                # last_level so the next upgrade is detected as one.
                stale_levels = [
                    lvl for lvl, r in rank.items()
                    if lvl is not None and r > rank.get(level, 0)
                ]
                for stale in stale_levels:
                    conn.execute(
                        "DELETE FROM alert_deliveries "
                        "WHERE account = ? AND level = ?",
                        (acct["name"], stale),
                    )
                conn.execute(
                    "UPDATE alert_state SET last_level = ? WHERE account = ?",
                    (level, acct["name"]),
                )
                conn.commit()
    finally:
        conn.close()

    return fired


def fire_test(config, db_path, account, level):
    """Synthesize a test webhook payload — bypasses thresholds and dedup."""
    fraction = 0.99 if level == "critical" else 0.78
    webhooks = [wh for wh in config.get("webhooks", [])
                if level in wh.get("on", ["warn", "critical"])]
    payload, results = _fire(webhooks, account, level, fraction)
    return payload, results
