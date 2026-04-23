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
    conn.commit()


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


def _fire(config, account, level, fraction):
    """Post the alert to every webhook whose `on` list includes the level."""
    payload = {
        "account":         account,
        "level":           level,
        "usage_fraction":  round(fraction, 4),
        "block_reset_at":  _block_reset_at(),
    }
    results = []
    for wh in config.get("webhooks", []):
        if level in wh.get("on", ["warn", "critical"]):
            ok, info = _post_webhook(wh["url"], payload)
            results.append({"url": wh["url"], "ok": ok, "info": info})
    return payload, results


def check_and_fire(config, db_path, quiet=True):
    """Iterate configured accounts; fire one webhook per account that just
    crossed into a new level. Returns a list of (account, level, results)
    tuples describing what was fired.
    """
    conn = sqlite3.connect(db_path)
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

            # Dedup: only fire when transitioning to a higher level. Going
            # back down silently resets state so a future re-cross fires again.
            rank = {None: 0, "warn": 1, "critical": 2}
            if level and rank[level] > rank[prev_level]:
                payload, results = _fire(config, acct["name"], level, fraction)
                # Only mark the crossing as "sent" if at least one delivery
                # actually succeeded. Otherwise (webhooks list empty, or all
                # endpoints returned errors) leave the previous state alone
                # so the next tick retries.
                any_ok = any(r.get("ok") for r in results)
                if any_ok:
                    conn.execute("""
                        INSERT INTO alert_state (account, last_level, last_fired_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(account) DO UPDATE SET
                            last_level = excluded.last_level,
                            last_fired_at = excluded.last_fired_at
                    """, (acct["name"], level, datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                    fired.append((acct["name"], level, results))
                    if not quiet:
                        print(f"  alert: {acct['name']} -> {level} ({fraction:.0%})")
                else:
                    # Still report the attempt so the cli.py summary shows
                    # the failure, but don't poison state.
                    fired.append((acct["name"], level, results))
                    if not quiet:
                        print(f"  alert (all deliveries failed): {acct['name']} -> {level}")
            elif level and level == prev_level:
                # Same level — touch last_fired_at only so the row doesn't go stale.
                pass
            elif prev and not level:
                # Dropped below warn — clear state so a later re-cross fires.
                conn.execute(
                    "UPDATE alert_state SET last_level = NULL WHERE account = ?",
                    (acct["name"],),
                )
                conn.commit()
    finally:
        conn.close()

    return fired


def fire_test(config, db_path, account, level):
    """Synthesize a test webhook payload — bypasses thresholds and dedup."""
    fraction = 0.99 if level == "critical" else 0.78
    payload, results = _fire(config, account, level, fraction)
    return payload, results
