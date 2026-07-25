"""trustmux-pair — generate a one-time pairing code for a new device."""
import json
import os
import shutil
import socket
import subprocess
import sys
import termios
import tty
from pathlib import Path

from trustmux._ctl import DEFAULT_PORT, daemon_info, direct_url, resolve_port, warn_if_peer_blocked

SOCK = Path.home() / ".config" / "trustmux" / "trustmux.sock"


def admin(cmd: dict) -> object:
    if not SOCK.exists():
        print("Error: Trustmux daemon not running (socket not found)", file=sys.stderr)
        sys.exit(1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(str(SOCK))
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


def _pair_url() -> str:
    """Return the URL a phone should open to reach this daemon.

    `tailscale serve` fronts the daemon only in the default start mode, which
    the daemon reports as a loopback bind with an https scheme.  In start-local
    and start-direct the tailnet name does not answer, so use the daemon's own
    address and port instead.
    """
    info = daemon_info() or {}
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
    port = DEFAULT_PORT if info else resolve_port()
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


def _wait_and_clear() -> None:
    """Wait for a keypress, then clear the screen."""
    if not sys.stdin.isatty():
        return
    print("  [ press any key to clear ]")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print("\033[2J\033[H", end="", flush=True)


def main():
    data = admin({"action": "pair_generate"})
    if not isinstance(data, dict) or "error" in data:
        print(f"Error: {data.get('error', data)}", file=sys.stderr)
        sys.exit(1)
    code = data["code"]
    mins = data["expires_in"] // 60
    url = _pair_url()

    pair_url = f"{url}#{code.replace('-', '')}"
    bar = "═" * 52
    print(f"\n{bar}")
    print(f"  Trustmux pairing code:  {code}  (valid {mins} min)")
    print(f"  Connect:                {pair_url}")
    print(f"{bar}\n")

    warn_if_peer_blocked()

    _print_qr(pair_url)
    print()

    _wait_and_clear()


if __name__ == "__main__":
    main()
