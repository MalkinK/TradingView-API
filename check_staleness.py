#!/usr/bin/env python3
"""
check_staleness.py — Monitor TradingView cache freshness, alert via Telegram.

Checks all OHLCV cache files in ~/shared/tv_cache/ for staleness.
Sends a Telegram alert if any file is stale for >1 hour during market hours.
Rate-limited to 1 alert per hour via marker file.

Designed to run from cron: */15 * * * 1-5

Author: Claude Code
Created: 2026-04-11
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────
TV_CACHE_DIR = os.path.join(os.path.expanduser("~"), "shared", "tv_cache")
LOG_FILE = os.path.join(os.path.expanduser("~"), "logs", "tv_staleness.log")
ALERT_MARKER = "/tmp/tv_stale_alert_sent"
ALERT_COOLDOWN_SECONDS = 3600  # 1 hour between alerts

# Staleness threshold: alert if data older than this (hours)
# Cron runs every 2h (0 */2 * * 1-5). Threshold = 2.5h gives buffer for cron lag.
STALE_THRESHOLD_HOURS = 2.5

# CME Globex hours: Sun 17:00 CT – Fri 16:00 CT (almost 23h/day)
# Simplified: weekdays are always "market hours" for alerting purposes.
# Weekend = Saturday all day + Sunday before 17:00 CT (UTC-6 winter / UTC-5 summer)

# Path to ibkr_stack env_loader for credential access
IBKR_SCRIPTS_DIR = os.path.join(os.path.expanduser("~"), "ibkr", "scripts")


def log(msg: str) -> None:
    """Append timestamped message to log file and stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_market_hours() -> bool:
    """Check if current time is within CME Globex trading hours.

    Globex runs Sun 17:00 CT – Fri 16:00 CT with a daily 15-min break.
    For alerting purposes: skip Saturday entirely and Sunday before 18:00 UTC
    (17:00 CT ≈ 23:00 UTC winter, 22:00 UTC summer — use 18:00 UTC as safe estimate).
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon ... 6=Sun

    # Saturday: no market
    if weekday == 5:
        return False

    # Sunday: market opens at ~17:00 CT (22:00-23:00 UTC)
    if weekday == 6 and now_utc.hour < 22:
        return False

    # Friday: market closes at ~16:00 CT (21:00-22:00 UTC)
    if weekday == 4 and now_utc.hour >= 22:
        return False

    return True


def check_cache_files() -> list[dict]:
    """Check all TV cache files for staleness.

    Returns:
        List of dicts with keys: file, symbol, age_hours, stale, cached_at.
    """
    results = []
    if not os.path.isdir(TV_CACHE_DIR):
        log(f"Cache directory not found: {TV_CACHE_DIR}")
        return results

    for filename in sorted(os.listdir(TV_CACHE_DIR)):
        if not filename.endswith(".json"):
            continue

        filepath = os.path.join(TV_CACHE_DIR, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            results.append({
                "file": filename, "symbol": filename, "age_hours": None,
                "stale": True, "cached_at": None, "error": str(e),
            })
            continue

        fetched_at_str = data.get("fetched_at")
        if not fetched_at_str:
            results.append({
                "file": filename, "symbol": data.get("symbol", filename),
                "age_hours": None, "stale": True, "cached_at": None,
            })
            continue

        try:
            cached_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        except (ValueError, AttributeError):
            results.append({
                "file": filename, "symbol": data.get("symbol", filename),
                "age_hours": None, "stale": True, "cached_at": fetched_at_str,
            })
            continue

        results.append({
            "file": filename,
            "symbol": data.get("symbol", filename),
            "age_hours": round(age_hours, 1),
            "stale": age_hours > STALE_THRESHOLD_HOURS,
            "cached_at": fetched_at_str,
        })

    return results


def can_send_alert() -> bool:
    """Check if cooldown period has passed since last alert."""
    if not os.path.exists(ALERT_MARKER):
        return True
    try:
        marker_age = time.time() - os.path.getmtime(ALERT_MARKER)
        return marker_age > ALERT_COOLDOWN_SECONDS
    except OSError:
        return True


def mark_alert_sent() -> None:
    """Touch marker file to record alert timestamp."""
    with open(ALERT_MARKER, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def send_telegram(message: str) -> bool:
    """Send Telegram alert using encrypted env credentials."""
    # Try loading credentials via ibkr_stack env_loader
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        sys.path.insert(0, IBKR_SCRIPTS_DIR)
        try:
            from env_loader import get_env_var
            bot_token = get_env_var("TELEGRAM_BOT_TOKEN")
            chat_id = get_env_var("TELEGRAM_CHAT_ID")
        except ImportError:
            log("Cannot load env_loader — Telegram alert skipped")
            return False

    if not bot_token or not chat_id:
        log("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — alert skipped")
        return False

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            success = resp.status == 200
            if success:
                log("Telegram alert sent successfully")
            return success
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def main() -> int:
    """Check TV cache staleness and alert if needed.

    Returns:
        0 if all fresh, 1 if stale files found, 2 if alert sent.
    """
    log("=== TradingView Cache Staleness Check ===")

    if not is_market_hours():
        log("Outside market hours — skipping check")
        return 0

    results = check_cache_files()
    if not results:
        log("No cache files found")
        return 1

    stale_files = [r for r in results if r["stale"]]
    fresh_files = [r for r in results if not r["stale"]]

    log(f"Cache status: {len(fresh_files)} fresh, {len(stale_files)} stale out of {len(results)} files")

    if not stale_files:
        log("All cache files are fresh")
        return 0

    for sf in stale_files:
        age_str = f"{sf['age_hours']}h" if sf["age_hours"] is not None else "unknown age"
        log(f"  STALE: {sf['file']} ({age_str})")

    # Send Telegram alert if cooldown allows
    if can_send_alert():
        stale_list = "\n".join(
            f"  • {sf['file']}: {sf['age_hours']}h old" if sf['age_hours'] else f"  • {sf['file']}: no timestamp"
            for sf in stale_files
        )
        msg = (
            f"⚠️ <b>TradingView Cache Stale</b>\n\n"
            f"{len(stale_files)}/{len(results)} cache files are stale "
            f"(threshold: {STALE_THRESHOLD_HOURS}h):\n\n"
            f"{stale_list}\n\n"
            f"Run: <code>bash ~/AIProjects/shadow-grab-dashboard/refresh_data.sh</code>"
        )
        send_telegram(msg)
        mark_alert_sent()
        return 2

    log("Alert cooldown active — not sending duplicate")
    return 1


if __name__ == "__main__":
    sys.exit(main())
