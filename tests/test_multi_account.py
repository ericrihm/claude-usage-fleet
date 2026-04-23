"""Tests for multi-account scanning, per-account filtering, and config loading.

Uses tempfile.TemporaryDirectory for DBs and profile paths — never touches
the real ~/.claude/usage.db.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
import dashboard
import scanner


FIXTURES = Path(__file__).parent / "fixtures"


def _make_profile(root, account_name, fixture_filename):
    """Mirror a fixture into a fake CLAUDE_CONFIG_DIR layout.

    root/<account_name>/projects/some-project/<fixture_filename>
    """
    profile = root / account_name
    project = profile / "projects" / "proj"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture_filename, project / fixture_filename)
    return profile


class TestConfigLoad(unittest.TestCase):
    def test_missing_file_returns_single_default(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config.load_config(path=Path(td) / "nope.json", quiet=True)
        self.assertEqual(len(cfg["accounts"]), 1)
        self.assertEqual(cfg["accounts"][0]["name"], "default")
        self.assertEqual(cfg["webhooks"], [])

    def test_load_two_accounts(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            _make_profile(td_path, "a1", "acct_opus.jsonl")
            _make_profile(td_path, "a2", "acct_sonnet.jsonl")
            cfg_file = td_path / "accounts.json"
            cfg_file.write_text(json.dumps({
                "accounts": [
                    {"name": "a1", "path": str(td_path / "a1"), "plan": "max_20x"},
                    {"name": "a2", "path": str(td_path / "a2"), "plan": "pro"},
                ],
                "thresholds": {"warn": 0.5, "critical": 0.9},
                "webhooks": [],
            }))
            cfg = config.load_config(path=cfg_file, quiet=True)
        self.assertEqual([a["name"] for a in cfg["accounts"]], ["a1", "a2"])
        self.assertEqual(cfg["thresholds"], {"warn": 0.5, "critical": 0.9})

    def test_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / "accounts.json"
            cfg_file.write_text(json.dumps({
                "accounts": [
                    {"name": "x", "path": str(Path(td) / "p1"), "plan": None},
                    {"name": "x", "path": str(Path(td) / "p2"), "plan": None},
                ],
            }))
            with self.assertRaises(config.ConfigError):
                config.load_config(path=cfg_file, quiet=True)

    def test_windows_path_in_wsl_maps_to_mnt(self):
        """The README promises that WSL users can put 'C:\\Users\\me\\.claude'
        in accounts.json. On POSIX, that string must be rewritten to
        '/mnt/c/Users/me/.claude' before being handed to pathlib — otherwise
        PurePosixPath treats it as a single relative filename.
        """
        if os.name == "nt":
            self.skipTest("WSL-mapping behavior only applies on POSIX hosts")
        resolved = config._resolve_path(r"C:\Users\me\.claude-acct3")
        self.assertTrue(str(resolved).startswith("/mnt/c/Users/me"),
                        f"expected /mnt/c mapping, got {resolved!r}")

    def test_invalid_plan_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / "accounts.json"
            cfg_file.write_text(json.dumps({
                "accounts": [{"name": "x", "path": str(Path(td)), "plan": "enterprise"}],
            }))
            with self.assertRaises(config.ConfigError):
                config.load_config(path=cfg_file, quiet=True)


class TestMigration(unittest.TestCase):
    def test_adds_account_column_to_both_tables(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
            conn = scanner.get_db(dbp)
            scanner.init_db(conn)
            sess_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
            turn_cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()]
            conn.close()
        self.assertIn("account", sess_cols)
        self.assertIn("account", turn_cols)

    def test_migration_backfills_legacy_rows_to_default(self):
        # Simulate a pre-migration DB with rows that have no account column.
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "usage.db"
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
            conn.execute(
                "INSERT INTO sessions(session_id, project_name) VALUES ('legacy-1', 'p')"
            )
            conn.execute(
                "INSERT INTO turns(session_id, timestamp, model, input_tokens, output_tokens,"
                " cache_read_tokens, cache_creation_tokens) VALUES ('legacy-1','2026-01-01',"
                " 'claude-opus-4-6', 10, 20, 0, 0)"
            )
            conn.commit()
            conn.close()

            conn = scanner.get_db(dbp)
            scanner.init_db(conn)
            sess_acct = conn.execute(
                "SELECT account FROM sessions WHERE session_id='legacy-1'"
            ).fetchone()[0]
            turn_acct = conn.execute(
                "SELECT account FROM turns WHERE session_id='legacy-1'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(sess_acct, "default")
        self.assertEqual(turn_acct, "default")


class TestScanAll(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = Path(self.td)
        _make_profile(self.root, "alpha", "acct_opus.jsonl")
        _make_profile(self.root, "beta", "acct_sonnet.jsonl")
        self.db = self.root / "usage.db"
        self.cfg = {
            "accounts": [
                {"name": "alpha", "path": str(self.root / "alpha"), "plan": "max_20x"},
                {"name": "beta",  "path": str(self.root / "beta"),  "plan": "pro"},
            ],
            "thresholds": {"warn": 0.75, "critical": 0.95},
            "webhooks": [],
        }

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_scan_all_tags_rows_with_correct_account(self):
        rows = scanner.scan_all(self.cfg, db_path=self.db, verbose=False)
        self.assertEqual(len(rows), 2)

        conn = sqlite3.connect(self.db)
        try:
            counts = dict(conn.execute(
                "SELECT account, COUNT(*) FROM turns GROUP BY account"
            ).fetchall())
        finally:
            conn.close()
        self.assertEqual(counts.get("alpha"), 3)
        self.assertEqual(counts.get("beta"), 3)

    def test_rescan_is_idempotent(self):
        scanner.scan_all(self.cfg, db_path=self.db, verbose=False)
        scanner.scan_all(self.cfg, db_path=self.db, verbose=False)

        conn = sqlite3.connect(self.db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(total, 6, "rescan duplicated turns")

    def test_same_session_id_different_accounts_coexist(self):
        # Give alpha and beta the exact same session_id to stress-test the
        # (account, session_id) scoping.
        proj_a = self.root / "alpha" / "projects" / "proj2"
        proj_b = self.root / "beta"  / "projects" / "proj2"
        proj_a.mkdir(parents=True, exist_ok=True)
        proj_b.mkdir(parents=True, exist_ok=True)
        shared = {
            "type": "assistant",
            "sessionId": "shared-xyz",
            "timestamp": "2026-04-22T12:00:00Z",
            "cwd": "/p",
            "message": {
                "id": "",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [],
            },
        }
        (proj_a / "s.jsonl").write_text(json.dumps(shared) + "\n")
        (proj_b / "s.jsonl").write_text(json.dumps(shared) + "\n")

        scanner.scan_all(self.cfg, db_path=self.db, verbose=False)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT account FROM sessions WHERE session_id='shared-xyz' ORDER BY account"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], ["alpha", "beta"])

    def test_rename_account_rescans_transcripts(self):
        """Renaming an account in accounts.json (keeping the same path) must
        NOT leave the renamed account showing empty because processed_files
        thinks the JSONL was already ingested."""
        scanner.scan_all(self.cfg, db_path=self.db, verbose=False)

        renamed = {
            "accounts": [
                {"name": "alpha-renamed", "path": str(self.root / "alpha"),
                 "plan": "max_20x"},
                {"name": "beta", "path": str(self.root / "beta"), "plan": "pro"},
            ],
            "thresholds": {"warn": 0.75, "critical": 0.95},
            "webhooks": [],
        }
        scanner.scan_all(renamed, db_path=self.db, verbose=False)

        conn = sqlite3.connect(self.db)
        try:
            accounts = {r[0] for r in conn.execute(
                "SELECT DISTINCT account FROM turns"
            ).fetchall()}
        finally:
            conn.close()
        self.assertIn("alpha-renamed", accounts,
                      "rename stranded data under old account name")

    def test_disjoint_paths_asserted(self):
        cfg = dict(self.cfg)
        cfg["accounts"] = [
            {"name": "a", "path": str(self.root / "alpha"), "plan": None},
            {"name": "b", "path": str(self.root / "alpha"), "plan": None},
        ]
        with self.assertRaises(AssertionError):
            scanner.scan_all(cfg, db_path=self.db, verbose=False)

    def test_dashboard_account_filter_scopes_sql(self):
        scanner.scan_all(self.cfg, db_path=self.db, verbose=False)
        d_all = dashboard.get_dashboard_data(db_path=self.db)
        d_alpha = dashboard.get_dashboard_data(db_path=self.db, account="alpha")
        self.assertEqual(len(d_all["sessions_all"]), 2)
        self.assertEqual(len(d_alpha["sessions_all"]), 1)
        self.assertEqual(d_alpha["sessions_all"][0]["account"], "alpha")


if __name__ == "__main__":
    unittest.main()
