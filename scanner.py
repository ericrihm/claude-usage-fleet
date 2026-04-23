"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
DB_PATH = Path.home() / ".claude" / "usage.db"
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]


def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)
    # Add message_id column if upgrading from older schema
    try:
        conn.execute("SELECT message_id FROM turns LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE turns ADD COLUMN message_id TEXT")

    # Multi-account migration: add `account` column to sessions and turns.
    # Existing rows are backfilled to 'default' so single-account installs keep working.
    for table in ("sessions", "turns"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "account" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN account TEXT NOT NULL DEFAULT 'default'"
            )
    # processed_files gets an `account` column and a composite PK. Upstream
    # keyed solely on `path`, which would let a rename in accounts.json
    # strand every transcript under the old account name (scanner would see
    # "path already processed" and skip). With (account, path) as the key,
    # renaming triggers a fresh scan under the new name while leaving the
    # old account's rows alone.
    pf_info = conn.execute("PRAGMA table_info(processed_files)").fetchall()
    pf_cols = [r[1] for r in pf_info]
    pf_pk = [r[1] for r in pf_info if r[5] > 0]
    if pf_pk != ["account", "path"]:
        account_select = "COALESCE(account, 'default')" if "account" in pf_cols else "'default'"
        conn.execute("BEGIN")
        try:
            conn.executescript(f"""
                CREATE TABLE processed_files_new (
                    path     TEXT NOT NULL,
                    mtime    REAL,
                    lines    INTEGER,
                    account  TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY (account, path)
                );
                INSERT OR IGNORE INTO processed_files_new (path, mtime, lines, account)
                SELECT path, mtime, lines, {account_select}
                FROM processed_files;
                DROP TABLE processed_files;
                ALTER TABLE processed_files_new RENAME TO processed_files;
            """)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # The CREATE TABLE above declares session_id as the sole PK. For fresh multi-
    # account installs, and for legacy installs being upgraded, we need the PK
    # to be (account, session_id) so the same session_id can appear under two
    # different profiles. SQLite can't redefine a PK in place — detect and
    # recreate the sessions table once. Idempotent: skips if already migrated.
    pk_cols = [
        r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall() if r[5] > 0
    ]
    if pk_cols == ["session_id"]:
        conn.execute("BEGIN")
        try:
            conn.executescript("""
                CREATE TABLE sessions_new (
                    session_id      TEXT NOT NULL,
                    project_name    TEXT,
                    first_timestamp TEXT,
                    last_timestamp  TEXT,
                    git_branch      TEXT,
                    total_input_tokens      INTEGER DEFAULT 0,
                    total_output_tokens     INTEGER DEFAULT 0,
                    total_cache_read        INTEGER DEFAULT 0,
                    total_cache_creation    INTEGER DEFAULT 0,
                    model           TEXT,
                    turn_count      INTEGER DEFAULT 0,
                    account         TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY (account, session_id)
                );
                INSERT INTO sessions_new
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     account)
                SELECT session_id, project_name, first_timestamp, last_timestamp,
                       git_branch, total_input_tokens, total_output_tokens,
                       total_cache_read, total_cache_creation, model, turn_count,
                       COALESCE(account, 'default')
                FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;
                CREATE INDEX IF NOT EXISTS idx_sessions_first
                    ON sessions(first_timestamp);
            """)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Account-scoped unique index for message_id: same API message won't be double-
    # inserted within one account, but two accounts that happen to share a file
    # (symlink, copy) each keep their own row.
    conn.execute("DROP INDEX IF EXISTS idx_turns_message_id")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_account_message_id
        ON turns(account, message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)

    # Indexes supporting per-account filtering in the dashboard.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_account ON turns(account)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account)")
    conn.commit()


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(filepath):
    """Parse a JSONL file and return (session_metas, turns, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract tool name from content if present
                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        session_meta[session_id]["model"] = model

                    turn = {
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "tool_name": tool_name,
                        "cwd": cwd,
                        "message_id": message_id,
                    }

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, line_count


def aggregate_sessions(session_metas, turns, account="default"):
    """Aggregate turn data back into session-level stats.

    account is attached to every returned session so the caller can persist it.
    """
    from collections import defaultdict

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            s["model"] = t["model"]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats, "account": account})
    return result


def upsert_sessions(conn, sessions, account="default"):
    for s in sessions:
        acct = s.get("account", account)
        # Check if session exists *for this account*. Same session_id under a
        # different account is a distinct row.
        existing = conn.execute(
            "SELECT total_input_tokens FROM sessions "
            "WHERE session_id = ? AND account = ?",
            (s["session_id"], acct)
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     account)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], acct,
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = COALESCE(?, model)
                WHERE session_id = ? AND account = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], s["model"],
                s["session_id"], acct,
            ))


def insert_turns(conn, turns, account="default"):
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd,
             message_id, account)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("account", account))
        for t in turns
    ])


def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True,
         account="default", _conn=None):
    """Scan a single account's JSONL tree. Pass `account` to tag all rows.

    When `_conn` is provided, scan reuses that connection (and does not close it).
    Used by scan_all() to keep all accounts in one transaction.
    """
    conn = _conn if _conn is not None else get_db(db_path)
    if _conn is None:
        init_db(conn)

    if projects_dirs:
        dirs_to_scan = [Path(d) for d in projects_dirs]
    elif projects_dir:
        dirs_to_scan = [Path(projects_dir)]
    else:
        dirs_to_scan = DEFAULT_PROJECTS_DIRS

    jsonl_files = []
    for d in dirs_to_scan:
        if not d.exists():
            continue
        if verbose:
            print(f"Scanning {d} ...")
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ? AND account = ?",
            (filepath, account)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {filepath}")

        if is_new:
            # New file: full parse (single read, returns line count)
            session_metas, turns, line_count = parse_jsonl_file(filepath)

            if turns or session_metas:
                for t in turns:
                    t["account"] = account
                sessions = aggregate_sessions(session_metas, turns, account=account)
                upsert_sessions(conn, sessions, account=account)
                insert_turns(conn, turns, account=account)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
                new_files += 1

        else:
            # Updated file: read once, process only new lines
            old_lines = row["lines"] if row else 0
            seen_messages = {}  # message_id -> turn (dedup streaming)
            turns_no_id = []
            new_session_metas = {}
            line_count = 0

            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    for line_count, line in enumerate(f, 1):
                        if line_count <= old_lines:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        rtype = record.get("type")
                        if rtype not in ("assistant", "user"):
                            continue

                        session_id = record.get("sessionId")
                        if not session_id:
                            continue

                        timestamp = record.get("timestamp", "")
                        cwd = record.get("cwd", "")

                        # Track session metadata from new lines
                        if session_id not in new_session_metas:
                            new_session_metas[session_id] = {
                                "session_id": session_id,
                                "project_name": project_name_from_cwd(cwd),
                                "first_timestamp": timestamp,
                                "last_timestamp": timestamp,
                                "git_branch": record.get("gitBranch", ""),
                                "model": None,
                            }
                        else:
                            meta = new_session_metas[session_id]
                            if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                                meta["last_timestamp"] = timestamp

                        if rtype == "assistant":
                            msg = record.get("message", {})
                            usage = msg.get("usage", {})
                            model = msg.get("model", "")
                            message_id = msg.get("id", "")

                            input_tokens = usage.get("input_tokens", 0) or 0
                            output_tokens = usage.get("output_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                            if input_tokens + output_tokens + cache_read + cache_creation == 0:
                                continue

                            tool_name = None
                            for item in msg.get("content", []):
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    tool_name = item.get("name")
                                    break

                            if model:
                                new_session_metas[session_id]["model"] = model

                            turn = {
                                "session_id": session_id,
                                "timestamp": timestamp,
                                "model": model,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cache_read_tokens": cache_read,
                                "cache_creation_tokens": cache_creation,
                                "tool_name": tool_name,
                                "cwd": cwd,
                                "message_id": message_id,
                            }

                            if message_id:
                                seen_messages[message_id] = turn
                            else:
                                turns_no_id.append(turn)
            except Exception as e:
                print(f"  Warning: {e}")

            if line_count <= old_lines:
                # File didn't grow (mtime changed but no new content)
                conn.execute(
                    "UPDATE processed_files SET mtime = ? WHERE path = ? AND account = ?",
                    (mtime, filepath, account),
                )
                conn.commit()
                skipped_files += 1
                continue

            new_turns = turns_no_id + list(seen_messages.values())

            if new_turns or new_session_metas:
                for t in new_turns:
                    t["account"] = account
                sessions = aggregate_sessions(
                    list(new_session_metas.values()), new_turns, account=account,
                )
                upsert_sessions(conn, sessions, account=account)
                insert_turns(conn, new_turns, account=account)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(new_turns)
            updated_files += 1

        # Record file as processed (line_count already known from the single read)
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines, account)
            VALUES (?, ?, ?, ?)
        """, (filepath, mtime, line_count, account))
        conn.commit()

    # Recompute session totals from actual turns in DB.
    # This ensures correctness when INSERT OR IGNORE skips duplicate turns
    # but upsert_sessions had already added their tokens additively.
    # Scoped to this account so concurrent scans on other accounts aren't touched.
    if new_files or updated_files:
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id AND turns.account = sessions.account), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id AND turns.account = sessions.account), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.session_id = sessions.session_id AND turns.account = sessions.account), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.session_id = sessions.session_id AND turns.account = sessions.account), 0),
                turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id AND turns.account = sessions.account), 0)
            WHERE account = ?
        """, (account,))
        conn.commit()

    if verbose:
        print(f"\nScan complete for account {account!r}:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    if _conn is None:
        conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions),
            "account": account}


def scan_all(config, db_path=DB_PATH, verbose=True):
    """Scan every account in `config["accounts"]` into one shared DB.

    Prints a summary table at the end. Paths are asserted disjoint to prevent
    processed_files.path collisions across accounts.
    """
    conn = get_db(db_path)
    init_db(conn)

    paths_seen = set()
    for acct in config["accounts"]:
        resolved = str(Path(acct["path"]).expanduser().resolve())
        assert resolved not in paths_seen, (
            f"Configured account paths must be disjoint; {acct['name']!r} -> "
            f"{resolved} was already claimed by another account"
        )
        paths_seen.add(resolved)

    rows = []
    for acct in config["accounts"]:
        projects_dir = Path(acct["path"]).expanduser() / "projects"
        if not projects_dir.exists():
            if verbose:
                print(f"  [SKIP] {acct['name']}: {projects_dir} does not exist")
            rows.append((acct["name"], 0, 0, 0, 0))
            continue
        result = scan(
            projects_dir=projects_dir,
            db_path=db_path,
            verbose=verbose,
            account=acct["name"],
            _conn=conn,
        )
        rows.append((acct["name"], result["new"], result["updated"],
                     result["skipped"], result["turns"]))

    if verbose:
        print()
        print(f"  {'ACCOUNT':<16} {'NEW':>6} {'UPD':>6} {'SKIP':>6} {'TURNS':>8}")
        print(f"  {'-'*16} {'-'*6} {'-'*6} {'-'*6} {'-'*8}")
        for name, new, upd, skip, turns in rows:
            print(f"  {name:<16} {new:>6} {upd:>6} {skip:>6} {turns:>8}")
        print()

    conn.close()
    return rows


if __name__ == "__main__":
    import sys
    projects_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--projects-dir" and i + 1 < len(sys.argv[1:]):
            projects_dir = Path(sys.argv[i + 2])
            break
    scan(projects_dir=projects_dir)
