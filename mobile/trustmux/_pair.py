"""trustmux-pair — generate a one-time pairing code for a new device."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from trustmux._advertise import advertised_urls
from trustmux._ctl import (DEFAULT_PORT, Instance, daemon_info, direct_url,
                           resolve_port, warn_if_peer_blocked)



def admin(cmd: dict, inst: Instance | None = None) -> object:
    inst = inst or Instance()
    if not inst.sock.exists():
        print("Error: Trustmux daemon not running (socket not found)", file=sys.stderr)
        sys.exit(1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(str(inst.sock))
        except OSError as e:
            print(f"Error: cannot connect to Trustmux daemon: {e}", file=sys.stderr)
            sys.exit(1)
        s.sendall(json.dumps(cmd).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        s.settimeout(10)
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as e:
            print(f"Error: timeout waiting for daemon response: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            return json.loads(b"".join(chunks))
        except json.JSONDecodeError as e:
            print(f"Error: malformed response from daemon: {e}", file=sys.stderr)
            sys.exit(1)


def _ts_url() -> str:
    """Return the HTTPS URL from tailscale serve, or empty string if unavailable."""
    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            stderr=subprocess.DEVNULL, timeout=5
        )
        d = json.loads(out)
        name = d.get("Self", {}).get("DNSName", "").rstrip(".")
        if name:
            return f"https://{name}/"
    except Exception:
        pass
    return ""


def _pair_url(inst: Instance | None = None) -> str:
    """Return the URL a phone should open to reach this daemon.

    An advertised address wins outright: it is the operator's answer to this
    exact question, and the one the daemon's certificate was built around.

    Otherwise, `tailscale serve` fronts the daemon only in the default start
    mode, which the daemon reports as a loopback bind with an https scheme.  In
    start-local and start-direct the tailnet name does not answer, so use the
    daemon's own address and port instead.
    """
    inst = inst or Instance()
    info = daemon_info(inst) or {}
    advertised = advertised_urls(info)
    if advertised:
        return advertised[0]
    served = info.get("host") in ("127.0.0.1", "localhost", "::1") \
        and info.get("scheme") == "https"
    if served or not info:
        ts = _ts_url()
        if ts:
            return ts
    # direct_url() prefers info's own port when present, so resolve_port()'s
    # (possibly daemon-querying) fallback is only actually needed when info
    # has none -- avoids a second, redundant admin-socket round trip in the
    # common case where info was already fetched above.
    port = DEFAULT_PORT if info else resolve_port(inst=inst)
    return direct_url(port, info)


def _print_qr(url: str) -> None:
    """Print a QR code for url using qrencode if available, else skip."""
    if shutil.which("qrencode"):
        try:
            subprocess.run(
                ["qrencode", "-t", "ANSIUTF8", "-m", "2", url],
                check=True
            )
            return
        except Exception:
            pass
    # qrcode Python library fallback
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        pass


def _poll(cmd: dict, inst: Instance | None = None) -> dict | None:
    """Like admin(), but returns None on failure instead of exiting.

    A daemon that goes away mid-wait should end the wait, not crash it.
    """
    inst = inst or Instance()
    if not inst.sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(inst.sock))
            s.sendall(json.dumps(cmd).encode() + b"\n")
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        resp = json.loads(b"".join(chunks))
    except (OSError, json.JSONDecodeError):
        return None
    return resp if isinstance(resp, dict) else None


def _wait_for_pair(timeout: float, inst: Instance | None = None) -> str:
    """Wait for a device to pair, up to timeout seconds.

    Returns the paired device's IP, or "" if nothing paired.
    """
    inst = inst or Instance()
    deadline = time.monotonic() + timeout
    while True:
        status = _poll({"action": "pair_status"}, inst)
        if status is None:
            return ""                      # daemon gone -- nothing left to wait for
        state = status.get("state")
        if state == "paired":
            return status.get("ip") or "unknown"
        if state is None:
            # Daemon predates pair_status; wait out the code instead of polling.
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            return ""
        if state != "pending":
            return ""                      # expired
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(1.0, remaining))


def _clear() -> None:
    """Clear the (now-useless) code and QR off the screen."""
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)


def main(inst: Instance | None = None):
    inst = inst or Instance()
    data = admin({"action": "pair_generate"}, inst)
    if not isinstance(data, dict) or "error" in data:
        print(f"Error: {data.get('error', data)}", file=sys.stderr)
        sys.exit(1)
    code = data["code"]
    ttl = data["expires_in"]
    mins = ttl // 60
    url = _pair_url(inst)

    pair_url = f"{url}#{code.replace('-', '')}"
    bar = "═" * 52
    print(f"\n{bar}")
    print(f"  Trustmux pairing code:  {code}  (valid {mins} min)")
    print(f"  Connect:                {pair_url}")
    print(f"{bar}\n")

    warn_if_peer_blocked()

    _print_qr(pair_url)
    print(f"\n  [ waiting up to {ttl}s for a device to pair ]")

    try:
        ip = _wait_for_pair(ttl, inst)
    except KeyboardInterrupt:
        print()
        ip = ""

    _clear()
    if not ip:
        print("no client paired")
        sys.exit(1)
    print(f"pair accepted from {ip}")


if __name__ == "__main__":
    main()
