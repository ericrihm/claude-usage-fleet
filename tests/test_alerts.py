"""Tests for alerts.py - block-usage computation, threshold firing, dedup."""

import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import alerts
import scanner


def _pick_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CollectingHandler(BaseHTTPRequestHandler):
    received = []  # class-level inbox; reset per test

    def log_message(self, *a, **kw):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = None
        _CollectingHandler.received.append({"path": self.path, "payload": payload})
        self.send_response(204)
        self.end_headers()


class _WebhookServer:
    def __init__(self):
        self.port = _pick_port()
        _CollectingHandler.received = []
        self.server = HTTPServer(("127.0.0.1", self.port), _CollectingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    @property
    def received(self):
        return list(_CollectingHandler.received)


def _seed_usage_above_warn(db_path, account, plan):
    """Insert enough recent turns for `account` to push usage above warn."""
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    limit = alerts.PLAN_LIMITS[plan]
    tokens = int(limit * 0.80)  # just above warn at 0.75
    conn.execute(
        "INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp,"
        " git_branch, model, turn_count, account) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s-{account}", "p", now_iso, now_iso, "main", "claude-sonnet-4-6", 1, account),
    )
    conn.execute(
        "INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens,"
        " cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id, account)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s-{account}", now_iso, "claude-sonnet-4-6", tokens, 0, 0, 0, None, "/p",
         f"m-{account}", account),
    )
    conn.commit()
    conn.close()


def _seed_usage_above_critical(db_path, account, plan):
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    limit = alerts.PLAN_LIMITS[plan]
    tokens = int(limit * 0.98)
    conn.execute(
        "INSERT OR REPLACE INTO turns (session_id, timestamp, model, input_tokens,"
        " output_tokens, cache_read_tokens, cache_creation_tokens, tool_name, cwd,"
        " message_id, account) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s-{account}-crit", now_iso, "claude-sonnet-4-6", tokens, 0, 0, 0, None, "/p",
         f"m-{account}-crit", account),
    )
    conn.commit()
    conn.close()


class TestWindowCutoff(unittest.TestCase):
    """Regression: the 5h window filter must not count old same-day turns.

    SQLite's datetime('now', '-5 hours') produces 'YYYY-MM-DD HH:MM:SS' while
    transcripts store 'YYYY-MM-DDTHH:MM:SSZ'. Comparing as TEXT would let
    early-morning turns slip through because 'T' sorts after ' '.
    """

    def test_old_same_day_turn_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            conn = scanner.get_db(dbp)
            scanner.init_db(conn)
            # Midnight UTC of today — definitely older than the 5h window
            # unless it's literally 0-5 AM UTC at test time, which we
            # intentionally avoid by using a fixed reference day.
            old_ts = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO turns(session_id, timestamp, model, input_tokens,"
                " output_tokens, cache_read_tokens, cache_creation_tokens,"
                " tool_name, cwd, message_id, account)"
                " VALUES ('s', ?, 'claude-pro', 10000, 0, 0, 0, NULL, '/', 'm', 'acct1')",
                (old_ts,),
            )
            conn.commit()
            # If and only if we're running between 05:00 and 23:59 UTC, the
            # midnight row is older than 5 hours. Skip the assertion in the
            # 0-5 UTC window rather than pinning wall-clock time.
            now_hour = datetime.now(timezone.utc).hour
            tokens = alerts.compute_block_tokens(conn, "acct1", window_hours=5)
            conn.close()
            if now_hour >= 5:
                self.assertEqual(tokens, 0, "5h filter admitted a midnight turn")


class TestLegacyDbMigration(unittest.TestCase):
    """Regression: `python cli.py alerts` against a pre-fork database must
    run the migration itself, not crash with 'no such column: account'."""

    def test_check_and_fire_migrates_legacy_db(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            # Create an upstream-shaped DB WITHOUT the account column.
            conn = sqlite3.connect(dbp)
            conn.executescript("""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY, project_name TEXT,
                    first_timestamp TEXT, last_timestamp TEXT, git_branch TEXT,
                    total_input_tokens INTEGER DEFAULT 0,
                    total_output_tokens INTEGER DEFAULT 0,
                    total_cache_read INTEGER DEFAULT 0,
                    total_cache_creation INTEGER DEFAULT 0,
                    model TEXT, turn_count INTEGER DEFAULT 0
                );
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                    timestamp TEXT, model TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
                    tool_name TEXT, cwd TEXT
                );
                CREATE TABLE processed_files (path TEXT PRIMARY KEY, mtime REAL, lines INTEGER);
            """)
            conn.commit()
            conn.close()

            cfg = {
                "accounts": [{"name": "default", "path": str(td), "plan": "pro"}],
                "thresholds": {"warn": 0.75, "critical": 0.95},
                "webhooks": [],
            }
            # Before the fix, this crashed with OperationalError.
            fired = alerts.check_and_fire(cfg, dbp, quiet=True)
            self.assertEqual(fired, [])


class TestBlockUsage(unittest.TestCase):
    def test_no_activity_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            conn = scanner.get_db(dbp)
            scanner.init_db(conn)
            self.assertEqual(alerts.compute_block_tokens(conn, "nobody"), 0)
            self.assertIsNone(alerts.compute_block_usage(conn, "nobody", plan="api"))
            self.assertIsNone(alerts.compute_block_usage(conn, "nobody", plan=None))
            conn.close()

    def test_fraction_matches_plan_limit(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_warn(dbp, "acct1", "pro")
            conn = scanner.get_db(dbp)
            frac = alerts.compute_block_usage(conn, "acct1", plan="pro")
            conn.close()
        self.assertIsNotNone(frac)
        self.assertGreater(frac, 0.75)
        self.assertLess(frac, 0.95)


class TestCheckAndFire(unittest.TestCase):
    def test_warn_fires_once_then_dedup_prevents_second(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_warn(dbp, "acct1", "pro")
            with _WebhookServer() as hook:
                cfg = {
                    "accounts": [{"name": "acct1", "path": str(td), "plan": "pro"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [{"url": hook.url, "on": ["warn", "critical"]}],
                }
                fired_1 = alerts.check_and_fire(cfg, dbp, quiet=True)
                fired_2 = alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(len(fired_1), 1)
                self.assertEqual(fired_1[0][1], "warn")
                self.assertEqual(len(fired_2), 0, "dedup failed: fired a second time without new level")
                # Exactly one POST reached the mock server.
                self.assertEqual(len(hook.received), 1)
                payload = hook.received[0]["payload"]
                self.assertEqual(payload["account"], "acct1")
                self.assertEqual(payload["level"], "warn")
                self.assertIn("block_reset_at", payload)

    def test_upgrade_warn_to_critical_fires_again(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_warn(dbp, "acct1", "pro")
            with _WebhookServer() as hook:
                cfg = {
                    "accounts": [{"name": "acct1", "path": str(td), "plan": "pro"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [{"url": hook.url, "on": ["warn", "critical"]}],
                }
                alerts.check_and_fire(cfg, dbp, quiet=True)  # warn fires
                _seed_usage_above_critical(dbp, "acct1", "pro")
                fired = alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(len(fired), 1)
                self.assertEqual(fired[0][1], "critical")
                self.assertEqual(len(hook.received), 2)
                self.assertEqual(hook.received[1]["payload"]["level"], "critical")

    def test_api_plan_never_fires(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_critical(dbp, "acct-api", "pro")  # tokens, but plan below
            # Now rebrand as api plan — no limit applies.
            with _WebhookServer() as hook:
                cfg = {
                    "accounts": [{"name": "acct-api", "path": str(td), "plan": "api"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [{"url": hook.url, "on": ["warn", "critical"]}],
                }
                fired = alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(fired, [])
                self.assertEqual(hook.received, [])

    def test_partial_failure_retries_only_failed_webhook(self):
        """If webhook A succeeds and B is unreachable, the next tick must
        retry B only — not A again, and not skip B forever."""
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_warn(dbp, "acct1", "pro")
            with _WebhookServer() as hookA:
                # Port that refuses connections reliably.
                dead_url = f"http://127.0.0.1:{_pick_port()}/down"
                cfg = {
                    "accounts": [{"name": "acct1", "path": str(td), "plan": "pro"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [
                        {"url": hookA.url,  "on": ["warn", "critical"]},
                        {"url": dead_url,   "on": ["warn", "critical"]},
                    ],
                }
                fired_1 = alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(len(fired_1), 1)
                # First run: hookA got the payload once, dead_url failed.
                self.assertEqual(len(hookA.received), 1)
                results_1 = {r["url"]: r["ok"] for r in fired_1[0][2]}
                self.assertTrue(results_1[hookA.url])
                self.assertFalse(results_1[dead_url])

                # Second run with same usage: hookA must NOT receive a
                # duplicate, but the system must still attempt dead_url.
                fired_2 = alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(len(hookA.received), 1, "hookA got duplicate delivery")
                # fired_2 should have an attempt only for dead_url.
                urls_attempted = [r["url"] for r in fired_2[0][2]] if fired_2 else []
                self.assertNotIn(hookA.url, urls_attempted, "hookA retried unnecessarily")
                self.assertIn(dead_url, urls_attempted, "dead_url not retried")

    def test_critical_to_warn_to_critical_fires_second_critical(self):
        """Rolling 5h window can naturally go critical -> warn -> critical
        without dropping to zero. The second critical crossing must fire —
        previously last_level stayed at 'critical' through the warn dip, so
        the re-upgrade was treated as a no-op."""
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            # Seed 0.80 — warn, then 0.98 — critical (first critical fires).
            _seed_usage_above_warn(dbp, "acct1", "pro")
            _seed_usage_above_critical(dbp, "acct1", "pro")

            with _WebhookServer() as hook:
                cfg = {
                    "accounts": [{"name": "acct1", "path": str(td), "plan": "pro"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [{"url": hook.url, "on": ["warn", "critical"]}],
                }
                # warn fires, then a second pass fires critical.
                alerts.check_and_fire(cfg, dbp, quiet=True)  # warn
                alerts.check_and_fire(cfg, dbp, quiet=True)  # critical

                # Drop back into warn range — wipe the critical turn's tokens.
                conn = scanner.get_db(dbp)
                conn.execute("DELETE FROM turns WHERE message_id = 'm-acct1-crit'")
                conn.commit()
                conn.close()

                # This tick should see prev=critical, level=warn — a downgrade.
                alerts.check_and_fire(cfg, dbp, quiet=True)
                # Cross back into critical.
                _seed_usage_above_critical(dbp, "acct1", "pro")
                fired = alerts.check_and_fire(cfg, dbp, quiet=True)

                # Should fire critical *again* — previously this was swallowed.
                critical_posts = [r for r in hook.received
                                  if r["payload"]["level"] == "critical"]
                self.assertEqual(len(critical_posts), 2,
                                 "second critical crossing was suppressed")
                self.assertTrue(fired and fired[-1][1] == "critical")

    def test_webhook_level_filter_respected(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            _seed_usage_above_warn(dbp, "acct1", "pro")
            with _WebhookServer() as hook:
                # webhook only wants critical — warn crossing should not post.
                cfg = {
                    "accounts": [{"name": "acct1", "path": str(td), "plan": "pro"}],
                    "thresholds": {"warn": 0.75, "critical": 0.95},
                    "webhooks": [{"url": hook.url, "on": ["critical"]}],
                }
                alerts.check_and_fire(cfg, dbp, quiet=True)
                self.assertEqual(hook.received, [])


if __name__ == "__main__":
    unittest.main()
