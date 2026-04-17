#!/usr/bin/env python3
"""TradingView snap cookie bridge.

Extracts sessionid / sessionid_sign from the TradingView snap Electron app's
Cookies SQLite and writes them to the fetcher's .env atomically.

Author: Kristin + Claude Code | Created: 2026-04-17

Why this exists:
    TV cookies expire every 30-90 days. Manual re-extraction from DevTools is
    dyslexia-hostile. The snap app runs under strict confinement with no
    libsecret access, so Chromium's os_crypt falls through to PLAINTEXT storage
    in the `value` column (verified 2026-04-17). No decryption needed for the
    observed format; v10/v11 encrypted fallbacks are detected and reported.

Design notes:
    - stdlib only. No pycryptodome. No network I/O except via send_telegram.
    - Snapshot copy to /dev/shm before querying — avoids racing TV app writes
      and never leaves plaintext on disk across reboots.
    - immutable=1 URI — sqlite3 will not create journal/WAL next to our copy.
    - Atomic .env rewrite: tempfile -> flush -> fsync -> close -> rename.
      Explicit fsync closes the crash window between rename and the kernel's
      eventual writeback.
    - Log values only as length + sha256[:8] — never raw cookies.

Exit codes:
    0  success (rotated or unchanged)
    1  unexpected exception
    2  cookies file missing
    3  sessionid absent/empty — TV logged out in snap app (alert immediately)
    4  .env write failure
    5  sqlite error
    6  encrypted (v11 / unknown format) or validation failed (alert immediately)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ── Paths and constants ──────────────────────────────────────────

HOME = Path.home()
COOKIES_SRC = HOME / "snap/tradingview/current/.config/TradingView/Cookies"
FETCHER_DIR = HOME / "AIProjects/tradingview-fetcher"
ENV_PATH = FETCHER_DIR / ".env"
STATE_PATH = FETCHER_DIR / "cookie_bridge/state.json"
LOG_PATH = HOME / "logs/tv_cookie_bridge.log"
SHM_DIR = Path("/dev/shm")

HOST_KEY = ".tradingview.com"
COOKIE_TO_ENV = {
    "sessionid": "SESSION",
    "sessionid_sign": "SIGNATURE",
}

COPY_RETRIES = 2
COPY_RETRY_SLEEP = 0.5
ALERT_COOLDOWN_SECONDS = 24 * 3600
CONSECUTIVE_FAIL_THRESHOLD = 3

# Cookie sanity bounds (loose — flag only schema-level surprises)
SESSION_MIN_LEN = 20
SESSION_MAX_LEN = 64
SIGNATURE_MIN_LEN = 20
SIGNATURE_MAX_LEN = 128


# ── Logging ──────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    """Configure structured logger to file. Systemd journal captures stdout separately."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("tv_cookie_bridge")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if lg.handlers:
        return lg
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    return lg


logger = _setup_logger()


# ── State + Telegram ─────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    """Return state dict; default if file missing or corrupt."""
    if not STATE_PATH.exists():
        return {"consecutive_failures": 0, "last_alert_ts": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("state.json unreadable, resetting: %s", e)
        return {"consecutive_failures": 0, "last_alert_ts": 0}


def _save_state(state: dict[str, Any]) -> None:
    """Persist state atomically."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.new")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _send_alert(message: str, state: dict[str, Any], *, immediate: bool) -> None:
    """Send Telegram alert respecting cooldown.

    Reuses send_telegram() from check_staleness.py (same fetcher dir).
    """
    now = time.time()
    if now - state.get("last_alert_ts", 0) < ALERT_COOLDOWN_SECONDS:
        logger.info("alert suppressed by 24h cooldown: %s", message[:80])
        return
    if not immediate and state.get("consecutive_failures", 0) < CONSECUTIVE_FAIL_THRESHOLD:
        return
    try:
        sys.path.insert(0, str(FETCHER_DIR))
        from check_staleness import send_telegram  # type: ignore
        ok = send_telegram(message)
    except Exception as e:
        logger.error("send_telegram failed: %s", e)
        ok = False
    if ok:
        state["last_alert_ts"] = now
        _save_state(state)


# ── Cookie extraction ────────────────────────────────────────────

def _snapshot_cookies_db() -> Path:
    """Copy the live Cookies SQLite to /dev/shm with retries.

    Returns path to the snapshot. Caller must delete it when done.
    """
    if not COOKIES_SRC.exists():
        raise FileNotFoundError(f"cookies file missing: {COOKIES_SRC}")
    SHM_DIR.mkdir(parents=True, exist_ok=True)
    dst = SHM_DIR / f"tv_cookies_{os.getpid()}.sqlite"
    last_err: Exception | None = None
    for attempt in range(COPY_RETRIES + 1):
        try:
            shutil.copy2(COOKIES_SRC, dst)
            os.chmod(dst, 0o600)
            return dst
        except OSError as e:
            last_err = e
            if attempt < COPY_RETRIES:
                time.sleep(COPY_RETRY_SLEEP)
    assert last_err is not None
    raise last_err


def _extract_cookies(db_path: Path) -> dict[str, str]:
    """Return {cookie_name: plaintext_value} for the two cookies we need.

    Raises RuntimeError with a descriptive message for v11 / unknown / missing.
    """
    uri = f"file:{db_path}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        cur = con.cursor()
        placeholders = ",".join("?" * len(COOKIE_TO_ENV))
        rows = cur.execute(
            "SELECT name, value, encrypted_value "
            f"FROM cookies WHERE host_key=? AND name IN ({placeholders})",
            (HOST_KEY, *COOKIE_TO_ENV.keys()),
        ).fetchall()
    finally:
        con.close()

    by_name = {r[0]: (r[1], r[2]) for r in rows}
    result: dict[str, str] = {}
    for name in COOKIE_TO_ENV:
        if name not in by_name:
            raise RuntimeError(f"cookie {name!r} not present in SQLite")
        value, encrypted = by_name[name]
        if value:
            result[name] = value
            continue
        if not encrypted:
            raise LookupError(f"cookie {name!r} has empty value AND empty encrypted_value (logged out?)")
        prefix = bytes(encrypted[:3])
        if prefix == b"v10":
            raise RuntimeError(
                f"cookie {name!r} is v10-encrypted — snap TV re-enabled os_crypt peanuts fallback. "
                "Bridge needs pycryptodome + PBKDF2('peanuts','saltysalt',1,16) + AES-CBC decrypt. "
                "See cookie_bridge/README.md future-encryption section."
            )
        if prefix == b"v11":
            raise RuntimeError(
                f"cookie {name!r} is v11-encrypted — snap gained libsecret access via password-manager-service. "
                "Bridge needs libsecret key lookup. See README future-encryption section."
            )
        raise RuntimeError(f"cookie {name!r} has unknown encryption prefix {prefix!r}")
    return result


def _validate(cookies: dict[str, str]) -> None:
    """Length + printable-ASCII sanity checks. Raises ValueError on failure.

    Note: '=' and other punctuation are allowed (base64 padding in sessionid_sign
    is legitimate). Only whitespace/control chars break .env parsing.
    """
    sess = cookies["sessionid"]
    sig = cookies["sessionid_sign"]
    if not (SESSION_MIN_LEN <= len(sess) <= SESSION_MAX_LEN):
        raise ValueError(f"sessionid length {len(sess)} out of range")
    if not (SIGNATURE_MIN_LEN <= len(sig) <= SIGNATURE_MAX_LEN):
        raise ValueError(f"sessionid_sign length {len(sig)} out of range")
    for name, val in cookies.items():
        if (not val.isprintable()) or ("\n" in val) or ("\r" in val) or ("\0" in val):
            raise ValueError(f"{name} contains non-printable or unsafe characters")


# ── .env atomic write ────────────────────────────────────────────

def _read_env(path: Path) -> list[tuple[str, str | None]]:
    """Return list of (line_verbatim, key_if_any) — preserves comments/blanks."""
    if not path.exists():
        return []
    out: list[tuple[str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append((line, None))
            continue
        key = stripped.split("=", 1)[0].strip()
        out.append((line, key))
    return out


def _render_env(lines: list[tuple[str, str | None]], updates: dict[str, str]) -> tuple[str, bool]:
    """Apply updates to lines, return (rendered_text, changed_bool)."""
    remaining = dict(updates)
    out_lines: list[str] = []
    changed = False
    for verbatim, key in lines:
        if key is None or key not in remaining:
            out_lines.append(verbatim)
            continue
        new_val = remaining.pop(key)
        new_line = f"{key}={new_val}"
        if new_line != verbatim:
            changed = True
        out_lines.append(new_line)
    for key, val in remaining.items():
        out_lines.append(f"{key}={val}")
        changed = True
    return "\n".join(out_lines) + "\n", changed


def _atomic_write_env(path: Path, content: str) -> None:
    """Write content to path atomically: tempfile -> flush -> fsync -> close -> rename.

    fsync is explicit before rename to close the crash window between rename
    and the kernel's eventual writeback. fsync on the parent directory makes
    the rename itself durable across power loss.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".env.", suffix=".new", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Main ─────────────────────────────────────────────────────────

def _fingerprint(val: str) -> str:
    """Short sha256 prefix — safe to log."""
    return hashlib.sha256(val.encode("utf-8")).hexdigest()[:8]


def main() -> int:
    state = _load_state()
    snapshot: Path | None = None
    try:
        logger.info("run start pid=%d", os.getpid())
        try:
            snapshot = _snapshot_cookies_db()
        except FileNotFoundError as e:
            logger.error("%s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                "⚠️ TV cookie bridge: snap Cookies file missing. "
                "Is TradingView installed and opened at least once?",
                state, immediate=False,
            )
            return 2

        try:
            cookies = _extract_cookies(snapshot)
        except LookupError as e:
            logger.error("logged out: %s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                "🔐 TV cookie bridge: sessionid empty — you're logged out in the TradingView app. "
                "Re-login in the snap app; bridge will resume on next run.",
                state, immediate=True,
            )
            return 3
        except RuntimeError as e:
            logger.error("format error: %s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                f"❌ TV cookie bridge: cookie format changed — bridge needs update. {e}",
                state, immediate=True,
            )
            return 6
        except sqlite3.Error as e:
            logger.error("sqlite: %s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                f"⚠️ TV cookie bridge: SQLite error reading snap Cookies: {e}",
                state, immediate=False,
            )
            return 5

        try:
            _validate(cookies)
        except ValueError as e:
            logger.error("validation: %s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                f"❌ TV cookie bridge: cookie validation failed: {e}",
                state, immediate=True,
            )
            return 6

        logger.info(
            "extracted sessionid len=%d fp=%s ; sessionid_sign len=%d fp=%s",
            len(cookies["sessionid"]), _fingerprint(cookies["sessionid"]),
            len(cookies["sessionid_sign"]), _fingerprint(cookies["sessionid_sign"]),
        )

        updates = {COOKIE_TO_ENV[n]: v for n, v in cookies.items()}
        existing_lines = _read_env(ENV_PATH)
        rendered, changed = _render_env(existing_lines, updates)
        try:
            if changed or not ENV_PATH.exists():
                _atomic_write_env(ENV_PATH, rendered)
                logger.info("wrote .env rotated=True")
            else:
                logger.info("wrote .env rotated=False (values unchanged)")
        except OSError as e:
            logger.error(".env write: %s", e)
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            _save_state(state)
            _send_alert(
                f"⚠️ TV cookie bridge: .env write failed: {e}",
                state, immediate=False,
            )
            return 4

        state["consecutive_failures"] = 0
        _save_state(state)
        logger.info("run end ok")
        return 0

    except Exception as e:
        logger.exception("unexpected: %s", e)
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        _save_state(state)
        _send_alert(
            f"💥 TV cookie bridge: unexpected error: {e}",
            state, immediate=False,
        )
        return 1
    finally:
        if snapshot is not None:
            try:
                snapshot.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
