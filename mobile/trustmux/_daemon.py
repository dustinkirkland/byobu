#!/usr/bin/env python3
"""Trustmux daemon — mobile companion for Byobu/tmux sessions."""

import argparse
import asyncio
import base64
from datetime import datetime
import getpass
import glob
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import tornado.httpserver
import tornado.web
import tornado.websocket

# ---------------------------------------------------------------------------
# Pairing & session state
# ---------------------------------------------------------------------------

_pair_code: str = ""
_pair_code_expiry: float = 0.0        # wall-clock time, for human display only
_pair_code_mono_expiry: float = 0.0   # monotonic time, for expiry check
_pair_attempts: int = 0
_MAX_PAIR_ATTEMPTS: int = 3
_PAIR_CODE_TTL: int = 180             # 3 minutes
_TOKEN_EXPIRY_DAYS: int = 90          # sessions expire after 90 days of inactivity
_sessions: dict[str, dict] = {}      # token → {ip, paired_at, label, last_used}
_https_mode: bool = False             # set by --https; enables Secure cookie

CONFIG_DIR    = Path.home() / ".config" / "trustmux"
TOKENS_FILE   = CONFIG_DIR / "tokens.json"
ADMIN_SOCK    = CONFIG_DIR / "trustmux.sock"
MACHINES_FILE = CONFIG_DIR / "machines.json"
_INSTALLED_STATIC = Path("/usr/share/trustmux/static")
_DEV_STATIC       = Path(__file__).parent / "static"
STATIC            = _INSTALLED_STATIC if _INSTALLED_STATIC.is_dir() else _DEV_STATIC

def _get_server_tz() -> str:
    try:
        with open("/etc/timezone") as _f:
            return _f.read().strip()
    except Exception:
        return "UTC"

_SERVER_TZ = _get_server_tz()

def _load_tokens() -> None:
    if not TOKENS_FILE.exists():
        return
    try:
        data = json.loads(TOKENS_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        expiry_cutoff = time.time() - _TOKEN_EXPIRY_DAYS * 86400
        valid = {
            t: s for t, s in data.items()
            if isinstance(t, str) and isinstance(s, dict)
            and "ip" in s and "paired_at" in s
            and float(s.get("last_used", s["paired_at"])) > expiry_cutoff
        }
        expired = sum(
            1 for t, s in data.items()
            if isinstance(t, str) and isinstance(s, dict)
            and "paired_at" in s
            and float(s.get("last_used", s["paired_at"])) <= expiry_cutoff
        )
        malformed = len(data) - len(valid) - expired
        if expired:
            print(f"Info: expired {expired} stale session(s) from {TOKENS_FILE}", flush=True)
        if malformed:
            print(f"Warning: skipped {malformed} malformed record(s) in {TOKENS_FILE}", flush=True)
        _sessions.update(valid)
    except Exception as e:
        print(f"Warning: could not load {TOKENS_FILE}: {e} — all sessions lost", flush=True)

def _save_tokens() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_DIR.chmod(0o700)
    tmp = TOKENS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_sessions, f, indent=2)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, TOKENS_FILE)

def _generate_pair_code() -> str:
    global _pair_code, _pair_code_expiry, _pair_code_mono_expiry, _pair_attempts
    _pair_code = f"{secrets.randbelow(1000000):06d}"
    _pair_code_expiry = time.time() + _PAIR_CODE_TTL
    _pair_code_mono_expiry = time.monotonic() + _PAIR_CODE_TTL
    _pair_attempts = 0
    return _pair_code

def _print_pair_code() -> None:
    fmt = f"{_pair_code[:3]}-{_pair_code[3:]}"
    expiry = datetime.fromtimestamp(_pair_code_expiry).strftime("%H:%M:%S")
    bar = "═" * 50
    print(f"\n{bar}")
    print(f"  Trustmux pairing code:  {fmt}  (expires {expiry})")
    print(f"{bar}\n", flush=True)

def _valid_session_token(token: str) -> bool:
    if not token:
        return False
    token_bytes = token.encode()
    expiry_cutoff = time.time() - _TOKEN_EXPIRY_DAYS * 86400
    for k, s in _sessions.items():
        if hmac.compare_digest(token_bytes, k.encode()):
            if float(s.get("last_used", s.get("paired_at", 0))) <= expiry_cutoff:
                return False
            s["last_used"] = time.time()
            return True
    return False

# ---------------------------------------------------------------------------
# Tailscale IP detection
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')

def _tailscale_ip() -> str | None:
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            ip = r.stdout.strip().splitlines()[0].strip()
            if _IPV4_RE.match(ip):
                return ip
    except Exception:
        pass
    try:
        r = subprocess.run(["ip", "-4", "addr", "show", "tailscale0"],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
        if m and _IPV4_RE.match(m.group(1)):
            return m.group(1)
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[mGKHFJABCDsuhrPX@L]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[()][AB012]'
    r'|\x1b[MDEHO78=>]'
    r'|\r'
)

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)

def _smarter_pane_name(pane_pid_str: str, fallback: str) -> str:
    """Walk /proc tree from pane_pid to find the leaf foreground process name.

    tmux's pane_current_command only sees the direct child of the shell (e.g.
    'sh' when running a shell-script wrapper like 'claude').  Reading
    /proc/<pid>/task/<pid>/children lets us follow the chain down to the real
    foreground process without spawning extra subprocesses.
    """
    try:
        pid = int(pane_pid_str)
    except (ValueError, TypeError):
        return fallback
    seen: set[int] = set()
    for _ in range(6):
        if pid in seen:
            break
        seen.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            break
        if not children:
            try:
                name = Path(f"/proc/{pid}/comm").read_text().strip()
                return name or fallback
            except OSError:
                break
        try:
            pid = int(children[0])
        except (ValueError, IndexError):
            break
    return fallback

# ---------------------------------------------------------------------------
# tmux interface — thin wrappers around the tmux CLI
# ---------------------------------------------------------------------------

def _tmux(*args) -> str:
    result = subprocess.run(
        ["tmux"] + list(args),
        capture_output=True, text=True, timeout=5
    )
    return result.stdout

def tmux_list_sessions() -> list[dict]:
    raw = _tmux("list-sessions", "-F",
                "#{session_id}\t#{session_name}\t#{session_attached}")
    sessions = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sid, name = parts[0], parts[1]
        attached = parts[2] == "1" if len(parts) > 2 else False
        sessions.append({
            "id": sid,
            "name": name,
            "attached": attached,
            "windows": tmux_list_windows(sid),
        })
    return sessions

def tmux_list_windows(session_id: str) -> list[dict]:
    raw = _tmux("list-windows", "-t", session_id, "-F",
                "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}")
    windows = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        wid, idx, name, active = parts[0], parts[1], parts[2], parts[3]
        try:
            index = int(idx)
        except ValueError:
            continue
        windows.append({
            "id": wid,
            "index": index,
            "name": name,
            "active": active == "1",
            "panes": tmux_list_panes(wid),
        })
    return windows

def tmux_list_panes(window_id: str) -> list[dict]:
    raw = _tmux("list-panes", "-t", window_id, "-F",
                "#{pane_id}\t#{pane_index}\t#{pane_active}\t#{pane_current_command}\t#{pane_pid}\t#{pane_dead}")
    panes = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pane_id_str = parts[0]
        try:
            idx = int(parts[1])
        except ValueError:
            continue
        active = parts[2] == "1"
        cmd     = parts[3] if len(parts) > 3 else ""
        pid_str = parts[4] if len(parts) > 4 else ""
        dead    = parts[5] == "1" if len(parts) > 5 else False
        if not dead and pid_str:
            cmd = _smarter_pane_name(pid_str, cmd)
        panes.append({
            "id": pane_id_str,
            "index": idx,
            "active": active,
            "command": cmd,
            "dead": dead,
        })
    return panes

def tmux_capture_pane(pane_id: str, history_lines: int = 200, ansi: bool = False) -> str:
    if ansi:
        raw = _tmux("capture-pane", "-t", pane_id, "-p", "-e", "-S", f"-{history_lines}")
    else:
        raw = _tmux("capture-pane", "-t", pane_id, "-p", "-S", f"-{history_lines}")
        raw = strip_ansi(raw)
    return raw

def tmux_new_session(name: str) -> None:
    _tmux("new-session", "-d", "-s", name)

def tmux_new_window(session_id: str, name: str = "") -> None:
    args = ["new-window", "-t", session_id]
    if name:
        args += ["-n", name]
    _tmux(*args)

def tmux_new_pane(window_id: str) -> None:
    _tmux("split-window", "-t", window_id)

def tmux_kill_pane(pane_id: str) -> None:
    _tmux("kill-pane", "-t", pane_id)

def tmux_kill_window(window_id: str) -> None:
    _tmux("kill-window", "-t", window_id)

def tmux_kill_session(session_id: str) -> None:
    _tmux("kill-session", "-t", session_id)

def tmux_send_keys(pane_id: str, keys: str, enter: bool = True, literal: bool = True) -> None:
    if literal:
        _tmux("send-keys", "-t", pane_id, "-l", keys)
    else:
        _tmux("send-keys", "-t", pane_id, keys)
    if enter:
        _tmux("send-keys", "-t", pane_id, "Enter")

def tmux_rename_window(window_id: str, name: str) -> None:
    _tmux("rename-window", "-t", window_id, name)

def tmux_rename_session(session_id: str, name: str) -> None:
    _tmux("rename-session", "-t", session_id, name)

# ---------------------------------------------------------------------------
# Byobu status line — reads pre-computed cache from /dev/shm
# ---------------------------------------------------------------------------

_BG = {
    "black": "#1e1e1e",   "red": "#b03030",    "green": "#2a7a2a",
    "yellow": "#8a8000",  "blue": "#2050b0",    "magenta": "#7a2a7a",
    "cyan": "#1a7070",    "white": "#8a9090",
    "brightblack": "#484848", "brightred": "#cc4444",   "brightgreen": "#44bb44",
    "brightyellow": "#cccc00", "brightblue": "#4466cc",  "brightmagenta": "#bb44bb",
    "brightcyan": "#44bbbb",   "brightwhite": "#dddddd",
}
_LIGHT_BG = {"white", "brightwhite", "brightgreen", "brightyellow", "brightcyan", "brightblue"}

def _colour256_to_css(name: str) -> str | None:
    """Convert a tmux colour<N> 256-color name to a CSS hex string."""
    if not name.startswith("colour"):
        return None
    try:
        n = int(name[6:])
    except ValueError:
        return None
    if 16 <= n <= 231:
        # 6×6×6 RGB cube
        n -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[n // 36], levels[(n % 36) // 6], levels[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    if 232 <= n <= 255:
        # grayscale ramp
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    return None
_TMUX_ATTR = re.compile(r"#\[[^\]]*\]")
_CSS_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

def _first_attr(content: str, prefix: str) -> str | None:
    for m in _TMUX_ATTR.finditer(content):
        inner = m.group(0)[2:-1]
        for part in inner.split(","):
            part = part.strip()
            if part.startswith(prefix):
                val = part[len(prefix):]
                return val if val else None
    return None

def _byobu_shm() -> Path | None:
    user = getpass.getuser()
    uid = os.getuid()
    for hit in sorted(glob.glob(f"/dev/shm/byobu-{user}-*")):
        try:
            if os.stat(hit).st_uid == uid:
                return Path(hit)
        except OSError:
            continue
    return None

def _read_byobu_status_config() -> tuple[list[str], list[str]]:
    """Parse user's byobu status config; return (left_metrics, right_metrics)."""
    left_raw  = "logo release session"
    right_raw = "uptime load_average cpu_count cpu_freq memory disk date time"
    for path in [
        Path.home() / ".config" / "byobu" / "status",
        Path.home() / ".byobu" / "status",
        Path("/usr/share/byobu/status/status"),
    ]:
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("tmux_left="):
                    left_raw = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("tmux_right=") and not line.startswith("#"):
                    right_raw = line.split("=", 1)[1].strip().strip('"')
            break
        except OSError:
            continue

    def _parse(raw: str) -> list[str]:
        return [m for m in raw.split() if m and not m.startswith("#")]

    return _parse(left_raw), _parse(right_raw)

def _make_chip(name: str, shm: Path) -> dict | None:
    if not _BYOBU_METRIC_RE.match(name):
        return None
    status_dir = shm / "status.tmux"
    if not status_dir.is_dir():
        return None
    fpath = status_dir / name
    if not fpath.exists():
        return None
    try:
        raw = fpath.read_text()
    except OSError:
        return None
    text = _TMUX_ATTR.sub("", raw).strip()
    text = re.sub(r'([KMGT])(\d)', r'\1 \2', text)
    if not text:
        return None
    bg_name = _first_attr(raw, "bg=")
    if bg_name and _CSS_HEX_RE.match(bg_name):
        bg_css = bg_name
    elif bg_name and bg_name.startswith("colour"):
        bg_css = _colour256_to_css(bg_name) or "#2d2d2d"
    else:
        bg_css = _BG.get(bg_name or "", "#2d2d2d")
    text_css = "#111111" if bg_name in _LIGHT_BG else "#eeeeee"
    return {"label": name, "text": text, "bg": bg_css, "color": text_css}

def read_byobu_status() -> dict:
    shm = _byobu_shm()
    left_names, right_names = _read_byobu_status_config()

    def _chips(names: list[str]) -> list[dict]:
        if not shm:
            return []
        return [c for name in names if (c := _make_chip(name, shm))]

    return {"left": _chips(left_names), "right": _chips(right_names)}

# ---------------------------------------------------------------------------
# Tornado HTTP handlers
# ---------------------------------------------------------------------------

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self'"
)

class BaseHandler(tornado.web.RequestHandler):
    """All handlers inherit this for security headers."""

    def set_default_headers(self):
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "DENY")
        self.set_header("Content-Security-Policy", _CSP)
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if _https_mode:
            self.set_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def json(self, obj: object, status: int = 200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(obj))


class BaseAuthHandler(BaseHandler):
    """Protected handlers inherit this; unauthenticated requests get 401."""

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header("Cache-Control", "no-store")

    def prepare(self):
        token = self.get_cookie("trustmux_session") or ""
        if not _valid_session_token(token):
            self.json({"error": "unauthorized"}, 401)


# ── public endpoints ─────────────────────────────────────────────────────────

class IndexHandler(BaseHandler):
    async def get(self):
        content = await asyncio.to_thread((STATIC / "index.html").read_text)
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.finish(content)


class SvgHandler(BaseHandler):
    async def get(self):
        content = await asyncio.to_thread((STATIC / "trustmux.svg").read_bytes)
        self.set_header("Content-Type", "image/svg+xml")
        self.set_header("Cache-Control", "max-age=86400")
        self.finish(content)


class ManifestHandler(BaseHandler):
    def get(self):
        hostname = socket.gethostname().split('.')[0]
        manifest = {
            "name":             f"Trustmux · {hostname}",
            "short_name":       hostname,
            "description":      "Monitor and interact with your tmux/Byobu sessions from your phone.",
            "start_url":        "/",
            "display":          "standalone",
            "background_color": "#141414",
            "theme_color":      "#141414",
            "icons": [
                {"src": "/icons/icon-192.png?v=3", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/icons/icon-512.png?v=3", "sizes": "512x512",
                 "type": "image/png", "purpose": "any maskable"},
            ],
        }
        self.set_header("Content-Type", "application/manifest+json")
        self.set_header("Cache-Control", "no-cache")
        self.finish(json.dumps(manifest))


class ServiceWorkerHandler(BaseHandler):
    async def get(self):
        content = await asyncio.to_thread((STATIC / "sw.js").read_bytes)
        self.set_header("Content-Type", "application/javascript")
        # Service workers must not be cached — browser re-checks on every load.
        self.set_header("Cache-Control", "no-cache")
        self.finish(content)


class AppJsHandler(BaseHandler):
    async def get(self):
        content = await asyncio.to_thread((STATIC / "app.js").read_bytes)
        self.set_header("Content-Type", "application/javascript")
        self.set_header("Cache-Control", "no-cache")
        self.finish(content)


class IconHandler(BaseHandler):
    async def get(self, filename: str):
        if not re.match(r'^[\w\-]+\.png$', filename):
            return self.json({"error": "not found"}, 404)
        path = STATIC / "icons" / filename
        if not path.exists():
            return self.json({"error": "not found"}, 404)
        content = await asyncio.to_thread(path.read_bytes)
        self.set_header("Content-Type", "image/png")
        self.set_header("Cache-Control", "no-cache")
        self.finish(content)


class PingHandler(BaseHandler):
    def get(self):
        self.set_header("Cache-Control", "no-store")
        token = self.get_cookie("trustmux_session") or ""
        if _valid_session_token(token):
            self.json({"auth": True, "hostname": socket.gethostname()})
        else:
            self.json({"auth": False}, 401)


class PairHandler(BaseHandler):
    async def post(self):
        global _pair_attempts, _pair_code, _pair_code_expiry, _pair_code_mono_expiry
        if not _pair_code:
            return self.json({"error": "no pairing code active — run trustmux-pair"}, 403)
        if time.monotonic() > _pair_code_mono_expiry:
            _pair_code = ""
            return self.json({"error": "pairing code expired — run trustmux-pair again"}, 403)
        if _pair_attempts >= _MAX_PAIR_ATTEMPTS:
            return self.json({"error": "too many attempts — run trustmux-pair again"}, 429)
        body_bytes = self.request.body
        if len(body_bytes) > 1024:
            return self.json({"error": "request too large"}, 413)
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            return self.json({"error": "invalid JSON"}, 400)
        if not isinstance(body, dict):
            return self.json({"error": "invalid JSON"}, 400)
        code = re.sub(r"\D", "", body.get("code", ""))
        if code != _pair_code:
            _pair_attempts += 1
            left = _MAX_PAIR_ATTEMPTS - _pair_attempts
            await asyncio.sleep(0.5)  # slow brute-force attempts
            return self.json({"error": f"wrong code — {left} attempts left"}, 403)
        # Valid — issue permanent session token; invalidate code (one device per code)
        token = secrets.token_urlsafe(32)
        label = self.request.headers.get("User-Agent", "")[:120]
        ip = self.request.remote_ip  # respects xheaders automatically
        now = time.time()
        _sessions[token] = {
            "ip": ip,
            "paired_at": now,
            "last_used": now,
            "label": label,
        }
        await asyncio.to_thread(_save_tokens)
        _pair_code = ""
        _pair_code_expiry = 0.0
        _pair_code_mono_expiry = 0.0
        _pair_attempts = 0
        print(f"✓ Trustmux: device paired ({ip})", flush=True)
        self.set_cookie(
            "trustmux_session", token,
            expires_days=_TOKEN_EXPIRY_DAYS,
            httponly=True,
            samesite="Strict",
            secure=_https_mode,
        )
        self.json({"ok": True})


class MachinesHandler(BaseAuthHandler):
    async def get(self):
        try:
            current_url = f"{self.request.protocol}://{self.request.host}"
            siblings = []
            if MACHINES_FILE.exists():
                raw = json.loads(await asyncio.to_thread(MACHINES_FILE.read_text))
                if isinstance(raw, list):
                    siblings = [s for s in raw
                                if isinstance(s, dict) and "name" in s and "url" in s
                                and re.match(r'^https://', s["url"])]
            result = [{"name": "this machine", "url": current_url, "current": True}] + [
                {"name": s["name"], "url": s["url"].rstrip("/"), "current": False}
                for s in siblings
            ]
            for s in siblings:
                if s.get("url", "").rstrip("/") == current_url.rstrip("/"):
                    result[0]["name"] = s["name"]
                    break
            self.json(result)
        except Exception:
            self.json({"error": "internal error"}, 500)


# ── protected endpoints ───────────────────────────────────────────────────────

class StatusHandler(BaseAuthHandler):
    async def get(self):
        chips = await asyncio.to_thread(read_byobu_status)
        self.json(chips)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

_MAX_HISTORY_LINES = 10_000
_TMUX_ID_RE = re.compile(r"^[$@%]\d+$")
_WS_RATE_WINDOW = 1.0   # seconds
_WS_RATE_LIMIT  = 20    # max messages per window

def _valid_tmux_id(s: str) -> bool:
    return bool(s and _TMUX_ID_RE.match(s))

_TMUX_NAME_BAD = re.compile(r'[:.@%\n\r]')
_BYOBU_METRIC_RE = re.compile(r'^[a-zA-Z0-9_]+$')

def _valid_tmux_name(s: str) -> bool:
    return bool(s) and not _TMUX_NAME_BAD.search(s)


class WsHandler(tornado.websocket.WebSocketHandler):
    """One WebSocket connection per browser tab.

    check_origin() is intentionally left at Tornado's default, which requires
    Origin == Host. This is a security measure against cross-site WebSocket
    hijacking and is correct for our setup in all modes.
    """

    async def get(self, *args, **kwargs):
        # Chrome uses HTTP/2 for HTTPS connections. HTTP/2 WebSocket (RFC 8441)
        # uses CONNECT + :protocol:websocket and omits Sec-WebSocket-Key.
        # Tailscale serve translates H2→H1.1 but doesn't generate the missing
        # key, causing Tornado to reject the handshake with 400. Inject it here.
        hdrs = self.request.headers
        if not hdrs.get("Sec-WebSocket-Key"):
            hdrs["Sec-WebSocket-Key"] = base64.b64encode(os.urandom(16)).decode()
        if not hdrs.get("Upgrade"):
            hdrs["Upgrade"] = "websocket"
        if "upgrade" not in hdrs.get("Connection", "").lower():
            hdrs["Connection"] = "Upgrade"
        await super().get(*args, **kwargs)

    def open(self):
        self._stream_task: asyncio.Task | None = None
        self._topo_task: asyncio.Task | None = None
        self._auth_timer: asyncio.Task | None = None
        self._rate_window_start = time.monotonic()
        self._rate_count = 0
        token = self.get_cookie("trustmux_session") or ""
        if token:
            # Cookie present: accept if valid, reject immediately if not
            if _valid_session_token(token):
                self._token = token
                self._authenticated = True
                self._start_streams()
            else:
                self.close(4401, "unauthorized")
        else:
            # No cookie: native client authenticates via first-message auth frame
            self._token = None
            self._authenticated = False
            self._auth_timer = asyncio.ensure_future(self._auth_timeout())

    def _start_streams(self):
        asyncio.ensure_future(self._send_sessions())
        self._topo_task = asyncio.ensure_future(self._poll_topology())

    async def _auth_timeout(self):
        await asyncio.sleep(10)
        if not getattr(self, "_authenticated", False):
            self.close(4401, "authentication timeout")

    def on_message(self, raw):
        asyncio.ensure_future(self._handle(raw))

    def on_close(self):
        if getattr(self, "_stream_task", None):
            self._stream_task.cancel()
        if getattr(self, "_topo_task", None):
            self._topo_task.cancel()
        if getattr(self, "_auth_timer", None):
            self._auth_timer.cancel()

    def _send(self, obj: dict):
        try:
            obj["server_ts"] = int(time.time() * 1000)
            obj["server_tz"] = _SERVER_TZ
            obj["server_tz_offset_s"] = int(datetime.now().astimezone().utcoffset().total_seconds())
            self.write_message(json.dumps(obj))
        except tornado.websocket.WebSocketClosedError:
            pass

    async def _send_sessions(self):
        sessions = await asyncio.to_thread(tmux_list_sessions)
        self._send({"type": "sessions", "data": sessions})

    async def _poll_topology(self):
        try:
            sessions = await asyncio.to_thread(tmux_list_sessions)
            last = json.dumps(sessions, sort_keys=True)
        except Exception:
            last = None
        while True:
            await asyncio.sleep(2)
            try:
                sessions = await asyncio.to_thread(tmux_list_sessions)
                key = json.dumps(sessions, sort_keys=True)
                if key != last:
                    last = key
                    self._send({"type": "sessions", "data": sessions})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _stream_pane(self, pane_id: str, history_lines: int, ansi: bool = False):
        try:
            content = await asyncio.to_thread(tmux_capture_pane, pane_id, history_lines, ansi)
            self._send({"type": "snapshot", "pane_id": pane_id, "data": content})
            last = content
            while True:
                await asyncio.sleep(0.5)
                content = await asyncio.to_thread(tmux_capture_pane, pane_id, history_lines, ansi)
                if content != last:
                    self._send({"type": "update", "pane_id": pane_id, "data": content})
                    last = content
        except asyncio.CancelledError:
            raise
        except Exception:
            self._send({"type": "error", "message": "pane stream error"})

    async def _handle(self, raw: str):
        # Handle unauthenticated state — expect auth message first
        if not getattr(self, "_authenticated", False):
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("type") == "auth":
                    token = str(msg.get("token", ""))
                    if _valid_session_token(token):
                        if self._auth_timer:
                            self._auth_timer.cancel()
                            self._auth_timer = None
                        self._token = token
                        self._authenticated = True
                        self._start_streams()
                        return
            except Exception:
                pass
            self.close(4401, "unauthorized")
            return

        # Re-check token on every message — catches revocation mid-session
        if not _valid_session_token(getattr(self, "_token", "")):
            self.close(4401, "unauthorized")
            return

        # Rate limiting: fixed window, per connection
        now = time.monotonic()
        if now - self._rate_window_start >= _WS_RATE_WINDOW:
            self._rate_window_start = now
            self._rate_count = 0
        self._rate_count += 1
        if self._rate_count > _WS_RATE_LIMIT:
            self._send({"type": "error", "message": "rate limit exceeded"})
            return

        if len(raw) > 16_384:
            self._send({"type": "error", "message": "message too large"})
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._send({"type": "error", "message": "invalid JSON"})
            return
        finally:
            del raw  # drop the raw WebSocket frame as early as possible
        if not isinstance(msg, dict):
            self._send({"type": "error", "message": "invalid JSON"})
            return

        mtype = msg.get("type")

        try:
            if mtype == "list_sessions":
                sessions = await asyncio.to_thread(tmux_list_sessions)
                self._send({"type": "sessions", "data": sessions})

            elif mtype == "subscribe":
                pane_id = msg.get("pane_id", "")
                if not _valid_tmux_id(pane_id):
                    self._send({"type": "error", "message": "invalid pane_id"})
                else:
                    try:
                        lines = max(1, min(int(msg.get("lines", 300)), _MAX_HISTORY_LINES))
                    except (ValueError, TypeError):
                        lines = 300
                    ansi = bool(msg.get("ansi", False))
                    if self._stream_task:
                        self._stream_task.cancel()
                        await asyncio.gather(self._stream_task, return_exceptions=True)
                    self._stream_task = asyncio.ensure_future(
                        self._stream_pane(pane_id, lines, ansi)
                    )

            elif mtype == "new_session":
                name = str(msg.get("name", "")).strip()[:128]
                if not name:
                    self._send({"type": "error", "message": "session name required"})
                elif not _valid_tmux_name(name):
                    self._send({"type": "error", "message": "invalid session name"})
                else:
                    await asyncio.to_thread(tmux_new_session, name)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    new_id = next((s["id"] for s in sessions_list if s["name"] == name), None)
                    self._send({"type": "sessions", "data": sessions_list, "new_session": new_id})

            elif mtype == "new_window":
                sid = msg.get("session_id", "")
                if not _valid_tmux_id(sid):
                    self._send({"type": "error", "message": "invalid session_id"})
                else:
                    name = str(msg.get("name", "")).strip()[:128]
                    if name and not _valid_tmux_name(name):
                        self._send({"type": "error", "message": "invalid window name"})
                        return
                    await asyncio.to_thread(tmux_new_window, sid, name)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    # Find the newly created pane: last window (highest index) in this session
                    new_pane_id = None
                    for s in sessions_list:
                        if s["id"] == sid and s.get("windows"):
                            last_win = max(s["windows"], key=lambda w: w["index"])
                            if last_win.get("panes"):
                                new_pane_id = last_win["panes"][0]["id"]
                            break
                    self._send({"type": "sessions", "data": sessions_list, "new_pane": new_pane_id})

            elif mtype == "new_pane":
                wid = msg.get("window_id", "")
                if not _valid_tmux_id(wid):
                    self._send({"type": "error", "message": "invalid window_id"})
                else:
                    # Snapshot pane IDs before split to identify the new one
                    panes_before = {p["id"] for p in await asyncio.to_thread(tmux_list_panes, wid)}
                    await asyncio.to_thread(tmux_new_pane, wid)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    new_pane_id = None
                    for s in sessions_list:
                        for w in s.get("windows", []):
                            if w["id"] == wid:
                                for p in w.get("panes", []):
                                    if p["id"] not in panes_before:
                                        new_pane_id = p["id"]
                                break
                    self._send({"type": "sessions", "data": sessions_list, "new_pane": new_pane_id})

            elif mtype == "kill_pane":
                pane_id = msg.get("pane_id", "")
                if not _valid_tmux_id(pane_id):
                    self._send({"type": "error", "message": "invalid pane_id"})
                else:
                    await asyncio.to_thread(tmux_kill_pane, pane_id)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    self._send({"type": "sessions", "data": sessions_list})

            elif mtype == "kill_window":
                wid = msg.get("window_id", "")
                if not _valid_tmux_id(wid):
                    self._send({"type": "error", "message": "invalid window_id"})
                else:
                    await asyncio.to_thread(tmux_kill_window, wid)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    self._send({"type": "sessions", "data": sessions_list})

            elif mtype == "kill_session":
                sid = msg.get("session_id", "")
                if not _valid_tmux_id(sid):
                    self._send({"type": "error", "message": "invalid session_id"})
                else:
                    await asyncio.to_thread(tmux_kill_session, sid)
                    sessions_list = await asyncio.to_thread(tmux_list_sessions)
                    self._send({"type": "sessions", "data": sessions_list})

            elif mtype == "send_keys":
                pane_id = msg.get("pane_id", "")
                if not _valid_tmux_id(pane_id):
                    self._send({"type": "error", "message": "invalid pane_id"})
                else:
                    keys = str(msg.get("keys", ""))[:4096]
                    enter   = bool(msg.get("enter", True))
                    literal = bool(msg.get("literal", True))
                    await asyncio.to_thread(tmux_send_keys, pane_id, keys, enter, literal)
                    del keys  # release sensitive content as early as possible

            elif mtype == "rename_window":
                wid = msg.get("window_id", "")
                if not _valid_tmux_id(wid):
                    self._send({"type": "error", "message": "invalid window_id"})
                else:
                    name = str(msg.get("name", "")).strip()[:128]
                    if not name or not _valid_tmux_name(name):
                        self._send({"type": "error", "message": "invalid window name"})
                    else:
                        await asyncio.to_thread(tmux_rename_window, wid, name)
                        sessions_list = await asyncio.to_thread(tmux_list_sessions)
                        self._send({"type": "sessions", "data": sessions_list})

            elif mtype == "rename_session":
                sid = msg.get("session_id", "")
                if not _valid_tmux_id(sid):
                    self._send({"type": "error", "message": "invalid session_id"})
                else:
                    name = str(msg.get("name", "")).strip()[:128]
                    if not name or not _valid_tmux_name(name):
                        self._send({"type": "error", "message": "invalid session name"})
                    else:
                        await asyncio.to_thread(tmux_rename_session, sid, name)
                        sessions_list = await asyncio.to_thread(tmux_list_sessions)
                        self._send({"type": "sessions", "data": sessions_list})

        except Exception:
            self._send({"type": "error", "message": "command failed"})


# ---------------------------------------------------------------------------
# Admin Unix socket — trustmux-pair / trustmux-unpair only
# Socket is 0o600; only the owning user can connect.
# No TCP exposure: management traffic never touches the network.
# ---------------------------------------------------------------------------

async def _handle_admin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
        cmd = json.loads(raw)
    except Exception:
        writer.write(b'{"error":"bad request"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    if not isinstance(cmd, dict):
        writer.write(b'{"error":"bad request"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    try:
        action = cmd.get("action", "")

        if action == "pair_generate":
            code = _generate_pair_code()
            _print_pair_code()
            resp: object = {"code": f"{code[:3]}-{code[3:]}", "expires_in": _PAIR_CODE_TTL}

        elif action == "sessions_list":
            resp = [
                {
                    "token":      t[:8] + "…",
                    "token_full": t,
                    "ip":         s["ip"],
                    "paired_at":  datetime.fromtimestamp(float(s["paired_at"])).strftime("%Y-%m-%d %H:%M:%S"),
                    "label":      s.get("label", ""),
                }
                for t, s in _sessions.items()
            ]

        elif action == "sessions_delete":
            token = cmd.get("token")   # None = clear all; non-empty string = specific token
            if token is None:
                count = len(_sessions)
                _sessions.clear()
                _save_tokens()
                resp = {"ok": True, "removed": count}
            elif token:
                if token in _sessions:
                    del _sessions[token]
                    _save_tokens()
                    resp = {"ok": True}
                else:
                    resp = {"error": "session not found"}
            else:
                resp = {"error": "token must be null (clear all) or a non-empty string"}

        else:
            resp = {"error": f"unknown action: {action!r}"}

        writer.write(json.dumps(resp).encode() + b"\n")
    except Exception:
        writer.write(b'{"error":"internal error"}\n')
    finally:
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def _run_admin_server() -> None:
    if ADMIN_SOCK.exists():
        ADMIN_SOCK.unlink()
    old_mask = os.umask(0o177)  # socket created with mode 0o600
    try:
        server = await asyncio.start_unix_server(_handle_admin, path=str(ADMIN_SOCK))
    finally:
        os.umask(old_mask)
    ADMIN_SOCK.chmod(0o600)  # belt and suspenders
    try:
        async with server:
            await server.serve_forever()
    finally:
        ADMIN_SOCK.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------

def _make_app() -> tornado.web.Application:
    return tornado.web.Application([
        (r"/",               IndexHandler),
        (r"/trustmux\.svg",  SvgHandler),
        (r"/manifest\.json", ManifestHandler),
        (r"/sw\.js",         ServiceWorkerHandler),
        (r"/app\.js",        AppJsHandler),
        (r"/icons/(.+)",     IconHandler),
        (r"/ping",           PingHandler),
        (r"/pair",           PairHandler),
        (r"/machines",       MachinesHandler),
        (r"/status",         StatusHandler),
        (r"/ws",             WsHandler),
    ])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _ensure_self_signed_cert(lan_ip: str) -> tuple:
    """Generate a self-signed TLS cert for lan_ip. Returns (cert_path, ssl_ctx)."""
    import ssl as _ssl
    import ipaddress as _ipaddress
    import datetime as _datetime
    from cryptography import x509 as _x509
    from cryptography.x509.oid import NameOID as _NameOID
    from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ec as _ec

    cert = CONFIG_DIR / "cert.pem"
    key  = CONFIG_DIR / "key.pem"
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    san_parts = [f"IP:{lan_ip}", "IP:127.0.0.1", "DNS:localhost"]
    fqdn = socket.getfqdn()
    if fqdn and fqdn not in ("localhost", lan_ip):
        san_parts.append(f"DNS:{fqdn}")
    hostname = socket.gethostname().split(".")[0]
    if hostname and hostname not in ("localhost",):
        san_parts.append(f"DNS:{hostname}")
    ts_ip = _tailscale_ip()
    if ts_ip and ts_ip != lan_ip:
        san_parts.append(f"IP:{ts_ip}")
    try:
        private_key = _ec.generate_private_key(_ec.SECP256R1())
        san_list = []
        for part in san_parts:
            if part.startswith("IP:"):
                san_list.append(_x509.IPAddress(_ipaddress.ip_address(part[3:])))
            elif part.startswith("DNS:"):
                san_list.append(_x509.DNSName(part[4:]))
        subject = issuer = _x509.Name([
            _x509.NameAttribute(_NameOID.COMMON_NAME, "trustmux"),
        ])
        now = _datetime.datetime.now(_datetime.timezone.utc)
        cert_obj = (
            _x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(_x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + _datetime.timedelta(days=3650))
            .add_extension(_x509.SubjectAlternativeName(san_list), critical=False)
            .sign(private_key, _hashes.SHA256())
        )
        key.write_bytes(private_key.private_bytes(
            encoding=_ser.Encoding.PEM,
            format=_ser.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=_ser.NoEncryption(),
        ))
        cert.write_bytes(cert_obj.public_bytes(_ser.Encoding.PEM))
        cert.chmod(0o644)
        key.chmod(0o600)
    except Exception as e:
        print(f"Error: TLS cert generation failed ({e})", flush=True)
        print("Trustmux refuses to start without encryption. Install 'cryptography': pip install --upgrade cryptography", flush=True)
        sys.exit(1)
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    print(f"Trustmux: self-signed TLS cert generated for {lan_ip}", flush=True)
    return cert, ctx


async def _amain(host: str, port: int, https: bool, ssl_ctx=None) -> None:
    global _https_mode
    _https_mode = https or ssl_ctx is not None
    app = _make_app()
    # xheaders=True: trust X-Forwarded-For/Proto from tailscale serve proxy.
    # Only set in --https mode; in direct mode leave False to prevent spoofing.
    server = tornado.httpserver.HTTPServer(app, xheaders=https,
                                           ssl_options=ssl_ctx,
                                           max_body_size=65536)
    server.listen(port, address=host)
    admin_task = asyncio.create_task(_run_admin_server())
    try:
        await asyncio.Event().wait()   # run until cancelled (Ctrl-C / SIGTERM)
    finally:
        admin_task.cancel()
        try:
            await admin_task
        except asyncio.CancelledError:
            pass
        server.stop()


def main():
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        _version = _pkg_version("trustmux")
    except PackageNotFoundError:
        _version = "dev"

    parser = argparse.ArgumentParser(description="Trustmux daemon")
    parser.add_argument("--version", action="version", version=f"trustmux {_version}")
    parser.add_argument("--host", default=None,
                        help="Bind address (default: Tailscale IP; 127.0.0.1 with --https)")
    parser.add_argument("--port", type=int, default=7432,
                        help="Port (default: 7432)")
    parser.add_argument("--https", action="store_true",
                        help="HTTPS mode: Secure cookie + trust proxy headers (use with tailscale serve)")
    parser.add_argument("--self-signed", action="store_true",
                        help="Generate a self-signed TLS cert for direct HTTPS without Tailscale")
    args = parser.parse_args()

    _load_tokens()

    host = args.host
    if not host:
        if args.https:
            host = "127.0.0.1"
            print("Trustmux: HTTPS mode — binding to localhost (tailscale serve proxy)")
        else:
            host = _tailscale_ip()
            if host:
                print(f"Trustmux: binding to Tailscale IP {host}")
            else:
                host = "127.0.0.1"
                print("Trustmux: Tailscale not found, binding to localhost only")

    ssl_ctx = None
    if args.self_signed:
        lan_ip = host if host not in ("0.0.0.0", None) else _tailscale_ip() or "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        _, ssl_ctx = _ensure_self_signed_cert(lan_ip)

    scheme = "https" if (args.https or ssl_ctx) else "http"
    print(f"Trustmux daemon on {scheme}://{host}:{args.port} — run 'trustmux-pair' to pair a device.", flush=True)
    asyncio.run(_amain(host, args.port, args.https, ssl_ctx))


if __name__ == "__main__":
    main()
