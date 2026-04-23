"""
config.py - Multi-account configuration for claude-usage-fleet.

Config is a JSON file (stdlib-only, no YAML parser needed). If the file is
absent, the fork falls back to single-account behavior matching upstream
phuryn/claude-usage.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".claude" / "accounts.json"

DEFAULT_THRESHOLDS = {"warn": 0.75, "critical": 0.95}

VALID_PLANS = {None, "api", "pro", "max_5x", "max_20x"}


class ConfigError(Exception):
    pass


def _single_account_fallback():
    return {
        "accounts": [{
            "name": "default",
            "path": str(Path.home() / ".claude"),
            "plan": None,
        }],
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "webhooks": [],
    }


_WIN_DRIVE_RE = None  # lazily imported


def _looks_like_windows_path(s):
    """Return True for 'C:\\...' or 'C:/...' style paths."""
    import re
    global _WIN_DRIVE_RE
    if _WIN_DRIVE_RE is None:
        _WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
    return bool(_WIN_DRIVE_RE.match(s))


def _resolve_path(p):
    """Expand ~ and return an absolute Path. Handles the WSL/Windows mixed
    case that the README advertises: on POSIX (including WSL), 'C:\\Users\\me'
    is mapped to '/mnt/c/Users/me' before pathlib.Path touches it, since
    PurePosixPath otherwise treats the drive-letter segment as a relative
    filename and silently points at the wrong directory.
    """
    s = os.path.expanduser(str(p))
    if os.name != "nt" and _looks_like_windows_path(s):
        drive = s[0].lower()
        remainder = s[2:].lstrip("\\/").replace("\\", "/")
        s = f"/mnt/{drive}/{remainder}"
    return Path(s.replace("\\", "/") if os.name != "nt" else s).resolve()


def _validate_account(acct, idx):
    if not isinstance(acct, dict):
        raise ConfigError(f"accounts[{idx}] must be an object")
    name = acct.get("name")
    path = acct.get("path")
    if not name or not isinstance(name, str):
        raise ConfigError(f"accounts[{idx}].name is required (string)")
    if not path or not isinstance(path, str):
        raise ConfigError(f"accounts[{idx}].path is required (string)")
    plan = acct.get("plan")
    if plan not in VALID_PLANS:
        raise ConfigError(
            f"accounts[{idx}].plan={plan!r} — must be one of {sorted(str(p) for p in VALID_PLANS)}"
        )
    return {"name": name, "path": str(_resolve_path(path)), "plan": plan}


def load_config(path=None, quiet=False):
    """Load accounts.json. Returns the fallback single-account config if missing.

    Structure:
        {
          "accounts": [{"name": "...", "path": "...", "plan": "..."|null}, ...],
          "thresholds": {"warn": 0.75, "critical": 0.95},
          "webhooks": [{"url": "...", "on": ["warn", "critical"]}, ...]
        }
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return _single_account_fallback()

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {cfg_path}: {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {cfg_path}: {e}")

    if not isinstance(raw, dict):
        raise ConfigError("accounts.json must be an object at the top level")

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("accounts.json must contain a non-empty 'accounts' list")

    accounts = []
    seen_names = set()
    seen_paths = set()
    for idx, acct in enumerate(accounts_raw):
        resolved = _validate_account(acct, idx)
        if resolved["name"] in seen_names:
            raise ConfigError(f"Duplicate account name: {resolved['name']!r}")
        if resolved["path"] in seen_paths:
            raise ConfigError(f"Duplicate account path: {resolved['path']!r}")
        seen_names.add(resolved["name"])
        seen_paths.add(resolved["path"])

        projects_dir = Path(resolved["path"]) / "projects"
        if not projects_dir.exists() and not quiet:
            print(
                f"  Warning: account {resolved['name']!r} — "
                f"{projects_dir} does not exist yet (will skip during scan).",
                file=sys.stderr,
            )

        accounts.append(resolved)

    thresholds = raw.get("thresholds") or {}
    merged_thresholds = dict(DEFAULT_THRESHOLDS)
    merged_thresholds.update({
        k: float(v) for k, v in thresholds.items() if k in DEFAULT_THRESHOLDS
    })
    if not (0 < merged_thresholds["warn"] < merged_thresholds["critical"] <= 1.0):
        raise ConfigError(
            f"thresholds invalid: warn={merged_thresholds['warn']}, "
            f"critical={merged_thresholds['critical']} — need 0 < warn < critical <= 1"
        )

    webhooks_raw = raw.get("webhooks") or []
    if not isinstance(webhooks_raw, list):
        raise ConfigError("webhooks must be a list")
    webhooks = []
    for idx, wh in enumerate(webhooks_raw):
        if not isinstance(wh, dict):
            raise ConfigError(f"webhooks[{idx}] must be an object")
        url = wh.get("url")
        if not url or not isinstance(url, str):
            raise ConfigError(f"webhooks[{idx}].url is required")
        levels = wh.get("on") or ["warn", "critical"]
        if not isinstance(levels, list) or not all(l in ("warn", "critical") for l in levels):
            raise ConfigError(f"webhooks[{idx}].on must be a list of 'warn'/'critical'")
        webhooks.append({"url": url, "on": list(levels)})

    return {"accounts": accounts, "thresholds": merged_thresholds, "webhooks": webhooks}


def config_summary_line(cfg):
    n = len(cfg["accounts"])
    return f"{n} account(s): " + ", ".join(a["name"] for a in cfg["accounts"])
