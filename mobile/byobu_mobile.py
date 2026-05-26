#!/usr/bin/env python3
"""byobu-mobile daemon — mobile companion for Byobu/tmux sessions."""

import argparse
import asyncio
from datetime import datetime
import getpass
import glob
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Pairing & session state
# ---------------------------------------------------------------------------

_pair_code: str = ""
_pair_code_expiry: float = 0.0        # wall-clock time, for human display only
_pair_code_mono_expiry: float = 0.0   # monotonic time, for expiry check
_pair_attempts: int = 0
_MAX_PAIR_ATTEMPTS: int = 10
_PAIR_CODE_TTL: int = 300           # 5 minutes
_sessions: dict[str, dict] = {}    # token → {ip, paired_at, label}
_https_mode: bool = False           # set by --https; enables Secure cookie

CONFIG_DIR  = Path.home() / ".config" / "byobu-mobile"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
ADMIN_SOCK  = CONFIG_DIR / "byobu-mobile.sock"

def _load_tokens() -> None:
    if not TOKENS_FILE.exists():
        return
    try:
        data = json.loads(TOKENS_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        valid = {
            t: s for t, s in data.items()
            if isinstance(t, str) and isinstance(s, dict)
            and "ip" in s and "paired_at" in s
        }
        skipped = len(data) - len(valid)
        if skipped:
            print(f"Warning: skipped {skipped} malformed record(s) in {TOKENS_FILE}", flush=True)
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
    _pair_code_expiry = time.time() + _PAIR_CODE_TTL       # wall clock, display only
    _pair_code_mono_expiry = time.monotonic() + _PAIR_CODE_TTL  # monotonic, expiry check
    _pair_attempts = 0
    return _pair_code

def _print_pair_code() -> None:
    fmt = f"{_pair_code[:3]}-{_pair_code[3:]}"
    expiry = datetime.fromtimestamp(_pair_code_expiry).strftime("%H:%M:%S")
    bar = "═" * 50
    print(f"\n{bar}")
    print(f"  Byobu Mobile pairing code:  {fmt}  (expires {expiry})")
    print(f"{bar}\n", flush=True)

def _valid_session(request: Request) -> bool:
    token = request.cookies.get("byobu_mobile_session", "")
    return bool(token and token in _sessions)

# ---------------------------------------------------------------------------
# Auth + security headers middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    public = {"/", "/pair", "/ping", "/byobu.svg",
              "/manifest.json", "/sw.js", "/machines"}
    if request.url.path.startswith("/icons/"):
        public.add(request.url.path)
    if request.url.path not in public and not _valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.get("/ping")
async def ping(request: Request):
    if _valid_session(request):
        return JSONResponse({"auth": True})
    return JSONResponse({"auth": False}, status_code=401)

@app.post("/pair")
async def pair(request: Request):
    global _pair_attempts, _pair_code, _pair_code_expiry, _pair_code_mono_expiry
    if not _pair_code:
        return JSONResponse({"error": "no pairing code active — run byobu-mobile-pair"}, status_code=403)
    if time.monotonic() > _pair_code_mono_expiry:
        _pair_code = ""
        return JSONResponse({"error": "pairing code expired — run byobu-mobile-pair again"}, status_code=403)
    if _pair_attempts >= _MAX_PAIR_ATTEMPTS:
        return JSONResponse({"error": "too many attempts — run byobu-mobile-pair again"}, status_code=429)
    body_bytes = await request.body()
    if len(body_bytes) > 1024:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    code = re.sub(r"\D", "", body.get("code", ""))
    if code != _pair_code:
        _pair_attempts += 1
        left = _MAX_PAIR_ATTEMPTS - _pair_attempts
        return JSONResponse({"error": f"wrong code — {left} attempts left"}, status_code=403)
    # Valid — issue permanent session token; invalidate code (one device per code)
    token = secrets.token_urlsafe(32)
    label = request.headers.get("user-agent", "")[:120]
    ip = request.client.host if request.client else "unknown"
    _sessions[token] = {
        "ip": ip,
        "paired_at": time.time(),
        "label": label,
    }
    _save_tokens()
    _pair_code = ""
    _pair_code_expiry = 0.0
    _pair_code_mono_expiry = 0.0
    _pair_attempts = 0
    print(f"✓ Byobu Mobile: device paired ({ip})", flush=True)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "byobu_mobile_session", token,
        max_age=10 * 365 * 86400,
        httponly=True,
        samesite="strict",
        secure=_https_mode,
    )
    return resp

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

# Strip ANSI escape sequences and carriage returns from terminal output
ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[mGKHFJABCDsuhrPX@L]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[()][AB012]'
    r'|\x1b[MDEHO78=>]'
    r'|\r'
)

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)

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
                "#{pane_id}\t#{pane_index}\t#{pane_active}\t#{pane_current_command}\t#{pane_pid}")
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
        cmd = parts[3] if len(parts) > 3 else ""
        panes.append({
            "id": pane_id_str,
            "index": idx,
            "active": active,
            "command": cmd,
        })
    return panes

def tmux_capture_pane(pane_id: str, history_lines: int = 200) -> str:
    raw = _tmux("capture-pane", "-t", pane_id, "-p", "-S", f"-{history_lines}")
    return strip_ansi(raw)

def tmux_new_session(name: str) -> None:
    _tmux("new-session", "-d", "-s", name)

def tmux_new_window(session_id: str, name: str = "") -> None:
    args = ["new-window", "-t", session_id]
    if name:
        args += ["-n", name]
    _tmux(*args)

def tmux_new_pane(window_id: str) -> None:
    _tmux("split-window", "-t", window_id)

def tmux_send_keys(pane_id: str, keys: str, enter: bool = True) -> None:
    _tmux("send-keys", "-t", pane_id, "-l", keys)
    if enter:
        _tmux("send-keys", "-t", pane_id, "Enter")

# ---------------------------------------------------------------------------
# Byobu status line — reads pre-computed cache from /dev/shm
# ---------------------------------------------------------------------------

BYOBU_METRICS = [
    "hostname", "ip_address", "release", "uptime",
    "load_average", "cpu_freq", "cpu_temp",
    "memory", "disk", "network",
]

_BG = {
    "black": "#1e1e1e",   "red": "#b03030",    "green": "#2a7a2a",
    "yellow": "#8a8000",  "blue": "#2050b0",    "magenta": "#7a2a7a",
    "cyan": "#1a7070",    "white": "#8a9090",
    "brightblack": "#484848", "brightred": "#cc4444",   "brightgreen": "#44bb44",
    "brightyellow": "#cccc00", "brightblue": "#4466cc",  "brightmagenta": "#bb44bb",
    "brightcyan": "#44bbbb",   "brightwhite": "#dddddd",
}
_LIGHT_BG = {"white", "brightwhite", "brightgreen", "brightyellow", "brightcyan", "brightblue"}
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

def read_byobu_status() -> list[dict]:
    shm = _byobu_shm()
    if not shm:
        return []
    status_dir = shm / "status.tmux"
    if not status_dir.is_dir():
        return []

    chips = []
    for name in BYOBU_METRICS:
        fpath = status_dir / name
        if not fpath.exists():
            continue
        try:
            raw = fpath.read_text()
        except OSError:
            continue

        text = _TMUX_ATTR.sub("", raw).strip()
        text = re.sub(r'([A-Za-z])(\d)', r'\1 \2', text)
        if not text:
            continue

        bg_name = _first_attr(raw, "bg=")
        if bg_name and _CSS_HEX_RE.match(bg_name):
            bg_css = bg_name
        else:
            bg_css = _BG.get(bg_name or "", "#2d2d2d")

        text_css = "#111111" if bg_name in _LIGHT_BG else "#eeeeee"
        chips.append({"label": name, "text": text, "bg": bg_css, "color": text_css})

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chips.insert(0, {"label": "datetime", "text": now, "bg": "#2d2d2d", "color": "#eeeeee"})
    return chips

# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

STATIC = Path(__file__).parent / "static"
MACHINES_FILE = CONFIG_DIR / "machines.json"

@app.get("/")
async def index():
    content = await asyncio.to_thread((STATIC / "index.html").read_text)
    return HTMLResponse(content)

@app.get("/byobu.svg")
async def byobu_svg():
    from fastapi.responses import Response
    content = await asyncio.to_thread((STATIC / "byobu.svg").read_bytes)
    return Response(content=content, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})

@app.get("/manifest.json")
async def manifest():
    from fastapi.responses import Response
    content = await asyncio.to_thread((STATIC / "manifest.json").read_bytes)
    return Response(content=content, media_type="application/manifest+json",
                    headers={"Cache-Control": "max-age=3600"})

@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import Response
    content = await asyncio.to_thread((STATIC / "sw.js").read_bytes)
    # Service workers must not be cached aggressively — browsers re-check on every load.
    return Response(content=content, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})

@app.get("/icons/{filename}")
async def icons(filename: str):
    from fastapi.responses import Response
    if not re.match(r'^[\w\-]+\.png$', filename):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = STATIC / "icons" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = await asyncio.to_thread(path.read_bytes)
    return Response(content=content, media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})

@app.get("/machines")
async def machines(request: Request):
    """Return this machine + any configured siblings for the machine selector."""
    try:
        host = request.headers.get("host", "").split(":")[0]
        current_url = f"{'https' if _https_mode else 'http'}://{request.headers.get('host', 'localhost')}"
        siblings = []
        if MACHINES_FILE.exists():
            raw = json.loads(await asyncio.to_thread(MACHINES_FILE.read_text))
            if isinstance(raw, list):
                siblings = [s for s in raw if isinstance(s, dict) and "name" in s and "url" in s]
        result = [{"name": "this machine", "url": current_url, "current": True}] + [
            {"name": s["name"], "url": s["url"].rstrip("/"), "current": False}
            for s in siblings
        ]
        # Give the current machine a better name if siblings define one for this host
        for s in siblings:
            if s.get("url", "").rstrip("/") == current_url.rstrip("/"):
                result[0]["name"] = s["name"]
                break
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/status")
async def status():
    chips = await asyncio.to_thread(read_byobu_status)
    return JSONResponse(chips)

# ---------------------------------------------------------------------------
# WebSocket — one connection per browser tab
# ---------------------------------------------------------------------------

_MAX_HISTORY_LINES = 10_000
_TMUX_ID_RE = re.compile(r"^[$@%]\d+$")

_WS_RATE_WINDOW = 1.0   # seconds
_WS_RATE_LIMIT  = 20    # max messages per window

def _valid_tmux_id(s: str) -> bool:
    return bool(s and _TMUX_ID_RE.match(s))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("byobu_mobile_session", "")
    await websocket.accept()
    if not token or token not in _sessions:
        await websocket.close(code=4401)
        return
    stream_task: Optional[asyncio.Task] = None

    # Per-connection rate limiting
    _rate_window_start = time.monotonic()
    _rate_count = 0

    async def send(obj: dict):
        await websocket.send_text(json.dumps(obj))

    async def stream_pane(pane_id: str, history_lines: int):
        try:
            content = await asyncio.to_thread(tmux_capture_pane, pane_id, history_lines)
            await send({"type": "snapshot", "pane_id": pane_id, "data": content})
            last = content
            while True:
                await asyncio.sleep(0.5)
                content = await asyncio.to_thread(tmux_capture_pane, pane_id, history_lines)
                if content != last:
                    await send({"type": "update", "pane_id": pane_id, "data": content})
                    last = content
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await send({"type": "error", "message": f"pane stream lost: {e}"})

    try:
        sessions = await asyncio.to_thread(tmux_list_sessions)
        await send({"type": "sessions", "data": sessions})

        async for raw in websocket.iter_text():
            # Re-check token on every message — catches revocation mid-session
            if token not in _sessions:
                await websocket.close(code=4401)
                return

            # Rate limiting: max _WS_RATE_LIMIT messages per _WS_RATE_WINDOW seconds
            now = time.monotonic()
            if now - _rate_window_start >= _WS_RATE_WINDOW:
                _rate_window_start = now
                _rate_count = 0
            _rate_count += 1
            if _rate_count > _WS_RATE_LIMIT:
                await send({"type": "error", "message": "rate limit exceeded"})
                continue

            if len(raw) > 16_384:
                await send({"type": "error", "message": "message too large"})
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send({"type": "error", "message": "invalid JSON"})
                continue
            if not isinstance(msg, dict):
                await send({"type": "error", "message": "invalid JSON"})
                continue

            mtype = msg.get("type")

            try:
                if mtype == "list_sessions":
                    sessions = await asyncio.to_thread(tmux_list_sessions)
                    await send({"type": "sessions", "data": sessions})

                elif mtype == "subscribe":
                    pane_id = msg.get("pane_id", "")
                    if not _valid_tmux_id(pane_id):
                        await send({"type": "error", "message": "invalid pane_id"})
                    else:
                        try:
                            lines = max(1, min(int(msg.get("lines", 300)), _MAX_HISTORY_LINES))
                        except (ValueError, TypeError):
                            lines = 300
                        if stream_task:
                            stream_task.cancel()
                            await asyncio.gather(stream_task, return_exceptions=True)
                        stream_task = asyncio.create_task(stream_pane(pane_id, lines))

                elif mtype == "new_session":
                    name = str(msg.get("name", "")).strip()[:128]
                    if name:
                        await asyncio.to_thread(tmux_new_session, name)
                        sessions_list = await asyncio.to_thread(tmux_list_sessions)
                        new_id = next((s["id"] for s in sessions_list if s["name"] == name), None)
                        await send({"type": "sessions", "data": sessions_list, "new_session": new_id})
                    else:
                        await send({"type": "error", "message": "session name required"})

                elif mtype == "new_window":
                    sid = msg.get("session_id", "")
                    if not _valid_tmux_id(sid):
                        await send({"type": "error", "message": "invalid session_id"})
                    else:
                        name = str(msg.get("name", "")).strip()[:128]
                        await asyncio.to_thread(tmux_new_window, sid, name)
                        sessions_list = await asyncio.to_thread(tmux_list_sessions)
                        await send({"type": "sessions", "data": sessions_list})

                elif mtype == "new_pane":
                    wid = msg.get("window_id", "")
                    if not _valid_tmux_id(wid):
                        await send({"type": "error", "message": "invalid window_id"})
                    else:
                        await asyncio.to_thread(tmux_new_pane, wid)
                        sessions_list = await asyncio.to_thread(tmux_list_sessions)
                        await send({"type": "sessions", "data": sessions_list})

                elif mtype == "send_keys":
                    pane_id = msg.get("pane_id", "")
                    if not _valid_tmux_id(pane_id):
                        await send({"type": "error", "message": "invalid pane_id"})
                    else:
                        keys = str(msg.get("keys", ""))[:4096]
                        enter = bool(msg.get("enter", True))
                        await asyncio.to_thread(tmux_send_keys, pane_id, keys, enter)

            except Exception as e:
                await send({"type": "error", "message": f"command failed: {e}"})

    except WebSocketDisconnect:
        pass
    finally:
        if stream_task:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)

# ---------------------------------------------------------------------------
# Admin Unix socket — byobu-mobile-pair / byobu-mobile-unpair only
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
    except Exception as e:
        writer.write(json.dumps({"error": f"internal error: {e}"}).encode() + b"\n")
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
# Entry point
# ---------------------------------------------------------------------------

async def _amain(host: str, port: int, https: bool) -> None:
    global _https_mode
    _https_mode = https
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=https,
        forwarded_allow_ips="127.0.0.1" if https else None,
    )
    server = uvicorn.Server(config)
    admin_task = asyncio.create_task(_run_admin_server())
    try:
        await server.serve()
    finally:
        admin_task.cancel()
        try:
            await admin_task
        except asyncio.CancelledError:
            pass

def main():
    parser = argparse.ArgumentParser(description="Byobu Mobile daemon")
    parser.add_argument("--host", default=None,
                        help="Bind address (default: Tailscale IP; 127.0.0.1 with --https)")
    parser.add_argument("--port", type=int, default=7432,
                        help="Port (default: 7432)")
    parser.add_argument("--https", action="store_true",
                        help="HTTPS mode: Secure cookie + trust proxy headers (use with tailscale serve)")
    args = parser.parse_args()

    _load_tokens()

    host = args.host
    if not host:
        if args.https:
            # In HTTPS mode the daemon sits behind tailscale serve on localhost
            host = "127.0.0.1"
            print("Byobu Mobile: HTTPS mode — binding to localhost (tailscale serve proxy)")
        else:
            host = _tailscale_ip()
            if host:
                print(f"Byobu Mobile: binding to Tailscale IP {host}")
            else:
                host = "127.0.0.1"
                print("Byobu Mobile: Tailscale not found, binding to localhost only")

    print(f"Byobu Mobile daemon on {host}:{args.port} — run 'byobu-mobile-pair' to pair a device.", flush=True)
    asyncio.run(_amain(host, args.port, args.https))

if __name__ == "__main__":
    main()
