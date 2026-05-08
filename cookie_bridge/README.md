# TradingView Snap Cookie Bridge

Reads `sessionid` / `sessionid_sign` from the TradingView snap desktop app and
writes them to `~/AIProjects/tradingview-fetcher/.env` so `fetch_data.js` never
runs on expired cookies.

## How it works

1. Snapshot `~/snap/tradingview/current/.config/TradingView/Cookies` to `/dev/shm`
   (tmpfs — wiped on reboot, 0600 perms).
2. Open read-only (`mode=ro&immutable=1`) via `sqlite3` stdlib.
3. `SELECT value FROM cookies WHERE host_key='.tradingview.com' AND name IN ('sessionid','sessionid_sign')`.
4. Validate lengths + printable-ASCII (allowing `=`, `:`, `+` — base64 signature
   padding is legitimate).
5. Atomic write to `.env`: `tempfile -> flush -> fsync -> close -> rename -> fsync(dir)`.
6. Delete snapshot from `/dev/shm`.

A systemd user timer runs the bridge every 6h. Manual trigger:
```bash
systemctl --user start tradingview-cookie-bridge.service
# or via supervisor:
curl -s -X POST http://127.0.0.1:5591/restart/tradingview-cookie-bridge
```

## Why this architecture

- **No headless Chrome / Playwright** — doesn't need a password, doesn't trigger TV's bot detection.
- **No password storage** — we read cookies from an already-authenticated app session.
- **stdlib only** — no pycryptodome dependency until / unless encryption appears (see below).
- **Plaintext is not our choice** — it's how the snap app stores cookies today. See *Security note*.

## Current encryption state (verified 2026-04-17)

The snap runs under strict confinement without `password-manager-service`
interface access, so Chromium can't reach libsecret/gnome-keyring. Electron
falls through to **plaintext storage in the `value` column** —
`encrypted_value` is 0 bytes for every TV cookie. The bridge reads `value`
directly.

## Future encryption handling

If a future TV/Electron update starts encrypting cookies, the bridge detects
the prefix in `encrypted_value` and exits 6 with a clear Telegram alert. No
silent wrong-guess decryption.

**v10 prefix** (os_crypt "peanuts" fallback, no libsecret required):
- Key: `PBKDF2_HMAC(sha1, b"peanuts", b"saltysalt", 1, 16)`
- IV: `b" " * 16` (16 spaces, 0x20)
- Cipher: AES-128-CBC, PKCS#7 padding
- Plaintext = `AES_CBC_decrypt(encrypted_value[3:], key=derived, iv=16_spaces)` stripped of PKCS#7.

**v11 prefix** (libsecret-managed key):
- Key lives in gnome-keyring under schema `chrome_libsecret_os_crypt_password_v2`.
- Snap needs `password-manager-service` interface plugged first:
  `sudo snap connect tradingview:password-manager-service :password-manager-service`
- Then look up key via `secretstorage` (Python) or `libsecret` and decrypt as v10.

In either case: add `pycryptodome` (and `secretstorage` for v11) to a small
`cookie_bridge/venv/` and update `snap_cookie_bridge.py`'s `_extract_cookies()`.

## Recovery matrix

| Symptom | Cause | Fix |
|---|---|---|
| Bridge exit 2 | Cookies file missing | Open TradingView snap app once |
| Bridge exit 3, Telegram "re-login" | Logged out in snap app | Sign in again in TradingView window |
| Bridge exit 6, "format changed" | Encryption re-enabled or column renamed | See "Future encryption handling" above |
| `.env` not picked up by fetcher | Fetcher was already running | `cd ~/AIProjects/tradingview-fetcher && node fetch_data.js` (fresh run reads new env) |
| `/status` shows bridge red | Timer stopped or service disabled | `systemctl --user status tradingview-cookie-bridge.timer` + `tail ~/logs/tv_cookie_bridge.log` |

## Files

| Path | Purpose |
|---|---|
| `cookie_bridge/snap_cookie_bridge.py` | Bridge script |
| `cookie_bridge/state.json` | `{"consecutive_failures", "last_alert_ts"}` for alert throttling (gitignored) |
| `~/.config/systemd/user/tradingview-cookie-bridge.service` | Oneshot runner |
| `~/.config/systemd/user/tradingview-cookie-bridge.timer` | 6h schedule |
| `~/logs/tv_cookie_bridge.log` | Structured timestamped log |

## Security note

- Cookies never appear in logs — only length + sha256[:8] fingerprint.
- Snapshot copy lives in `/dev/shm` (memory-backed, 0600, deleted after each run).
- `.env` written with mode 0600. Directory `fsync` so rename is durable before return.
- Bridge refuses to run if `encrypted_value` column ever contains data — no guesswork.
