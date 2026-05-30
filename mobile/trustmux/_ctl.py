"""trustmux-ctl — manage the Trustmux daemon."""
import argparse
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PORT       = 7432
SERVE_PORT = 443  # tailscale serve terminates TLS on :443
CONFIG_DIR = Path.home() / ".config" / "trustmux"
LOGFILE    = CONFIG_DIR / "trustmux.log"
PIDFILE    = Path("/tmp/trustmux.pid")
TOKENS_FILE = CONFIG_DIR / "tokens.json"


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOGFILE.touch()
    LOGFILE.chmod(0o600)


def _pid() -> int | None:
    """Return PID of process listening on PORT, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", f"-ti:{PORT}"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        if out:
            return int(out.splitlines()[0])
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        pass
    # Fallback: check PIDFILE
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
    return None


def _ts_host() -> str:
    """Return Tailscale DNS name, or empty string."""
    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        return json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
    except Exception:
        return ""


def _peer_acl_allows_tcp(port: int = SERVE_PORT) -> bool | None:
    """Check whether the current tailnet ACL permits peer devices to reach this
    node on tcp:<port>.

    Returns True if at least one packet-filter rule allows it, False if no rule
    does, or None if the check could not be performed (no tailscale binary,
    unexpected netmap shape, etc.) — callers should treat None as "no warning."
    """
    try:
        out = subprocess.check_output(
            ["tailscale", "debug", "netmap"],
            stderr=subprocess.DEVNULL, timeout=3, text=True,
        )
        nm = json.loads(out)
    except Exception:
        return None

    self_ips: set[str] = set()
    for cidr in (nm.get("SelfNode") or {}).get("Addresses") or []:
        self_ips.add(cidr.split("/")[0])

    rules = nm.get("PacketFilter")
    if not rules:
        return None

    for r in rules:
        protos = r.get("IPProto") or []
        # Empty IPProto means "any protocol" in Tailscale's filter format.
        if protos and 6 not in protos:
            continue
        for dst in r.get("Dsts") or []:
            ports = dst.get("Ports") or {}
            first, last = ports.get("First"), ports.get("Last")
            if first is None or last is None:
                continue
            if not (first <= port <= last):
                continue
            if self_ips:
                try:
                    net = ipaddress.ip_network(dst.get("Net", ""), strict=False)
                    if not any(ipaddress.ip_address(ip) in net for ip in self_ips):
                        continue
                except ValueError:
                    continue
            return True
    return False


def warn_if_peer_blocked(port: int = SERVE_PORT, stream=sys.stderr) -> None:
    """Print an actionable warning if peer access to tcp:<port> appears to be
    blocked by the tailnet ACL. Silent when the check passes or cannot run.

    Without this warning, an ACL that omits the serve port produces a confusing
    failure mode: the daemon and `tailscale serve` are healthy, `curl` from the
    same host succeeds (loopback bypasses ACL evaluation), but peer browsers
    see ERR_NETWORK_CHANGED or "Site cannot be reached" because tailscaled
    silently drops the incoming TCP with no RST.
    """
    if _peer_acl_allows_tcp(port) is not False:
        return
    print("", file=stream)
    print(f"warning: your tailnet ACL does not appear to allow tcp:{port} to this device.", file=stream)
    print( "         Peer devices will silently fail to connect; browsers show", file=stream)
    print( "         ERR_NETWORK_CHANGED or 'site cannot be reached.'", file=stream)
    print( "", file=stream)
    print( "         Edit your tailnet policy at:", file=stream)
    print( "           https://login.tailscale.com/admin/acls/file", file=stream)
    print( "", file=stream)
    print( "         For the newer 'grants' format, add:", file=stream)
    print( "", file=stream)
    print( '             { "src": ["autogroup:member"],', file=stream)
    print( '               "dst": ["<this-device-or-tag>"],', file=stream)
    print(f'               "ip":  ["tcp:{port}"] }}', file=stream)
    print( "", file=stream)
    print( "         For the legacy 'acls' format, add:", file=stream)
    print( "", file=stream)
    print( '             { "action": "accept",', file=stream)
    print( '               "src":    ["autogroup:member"],', file=stream)
    print(f'               "dst":    ["<this-device-or-tag>:{port}"] }}', file=stream)
    print( "", file=stream)


def _ensure_ts_serve() -> bool:
    """Configure tailscale serve for PORT. Returns True on success."""
    try:
        out = subprocess.check_output(
            ["tailscale", "serve", "status"],
            stderr=subprocess.DEVNULL, text=True,
        )
        if f":{PORT}" in out:
            print(f"✓ tailscale serve already configured for port {PORT}")
            return True
    except Exception:
        pass

    print(f"Enabling tailscale serve (HTTPS → localhost:{PORT})...")
    try:
        subprocess.run(
            ["tailscale", "serve", "--bg", str(PORT)],
            check=True, stderr=subprocess.DEVNULL,
        )
        print("✓ tailscale serve configured")
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    user = os.environ.get("USER", "")
    print("", file=sys.stderr)
    print("Error: could not configure tailscale serve.", file=sys.stderr)
    print("Your user needs Tailscale operator permission (one-time setup). Run:", file=sys.stderr)
    print(f"  sudo tailscale set --operator={user}", file=sys.stderr)
    print(f"  tailscale serve --bg {PORT}", file=sys.stderr)
    print("Then re-run: trustmux-ctl start", file=sys.stderr)
    return False


def _launch(extra_args: list[str]) -> int | None:
    """Launch daemon as a detached background process. Returns PID or None."""
    _ensure_dir()
    # Ensure the package directory is on the subprocess's Python path so that
    # `python3 -m trustmux` resolves correctly regardless of how Python was
    # invoked (e.g. bare /usr/bin/python3 from a .deb shim).
    pkg_parent = str(Path(__file__).parent.parent)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}:{existing}" if existing else pkg_parent
    with LOGFILE.open("a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "trustmux", "--port", str(PORT)] + extra_args,
            stdout=log, stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    PIDFILE.write_text(str(proc.pid))
    time.sleep(0.5)
    return _pid()


def cmd_setup(quiet: bool = False) -> int:
    print("=== trustmux setup ===\n")

    # Verify package is importable
    try:
        import trustmux._daemon  # noqa: F401
        print("✓ trustmux package available")
    except ImportError:
        print("Error: trustmux package not importable. Install with: pip install trustmux",
              file=sys.stderr)
        return 1

    # Tailscale presence
    try:
        subprocess.run(["tailscale", "--version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("\nError: tailscale not found in PATH.", file=sys.stderr)
        print("Install from https://tailscale.com/download, connect, then re-run.", file=sys.stderr)
        return 1

    ts_host = _ts_host()
    if not ts_host:
        print("\nError: Tailscale installed but not connected.", file=sys.stderr)
        print("Run 'tailscale up', then re-run setup.", file=sys.stderr)
        return 1
    print(f"✓ Tailscale connected as {ts_host}")

    if not _ensure_ts_serve():
        return 1

    warn_if_peer_blocked()

    if not quiet:
        print("\nSetup complete. Next steps:\n")
        print("  1. Start the daemon:      trustmux-ctl start")
        print("  2. Generate pairing code: trustmux-pair")
        print(f"  3. Open on your phone:    https://{ts_host}")
    return 0


def cmd_start(mode: str = "serve") -> int:
    p = _pid()
    if p:
        print(f"trustmux already running (pid {p})")
        return 1

    if mode == "serve":
        try:
            subprocess.run(["tailscale", "--version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("Error: tailscale not found.", file=sys.stderr)
            print("Install: https://tailscale.com/docs/install/linux", file=sys.stderr)
            print("Or use 'start-direct' for plain HTTP without Tailscale.", file=sys.stderr)
            return 1
        ts_host = _ts_host()
        if not ts_host:
            print("Error: cannot determine Tailscale hostname (is tailscale up?)", file=sys.stderr)
            return 1
        if not _ensure_ts_serve():
            return 1
        print("Starting trustmux (HTTPS mode)...")
        pid = _launch(["--host", "127.0.0.1", "--https"])
        ok = pid is not None
        if ok:
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{ts_host}")

    elif mode == "start-local":
        print("Starting trustmux (loopback only — SSH tunnel access)...")
        pid = _launch(["--host", "127.0.0.1"])
        ok = pid is not None
        if ok:
            fqdn = socket.getfqdn()
            print(f"trustmux started (pid {pid})")
            print(f"Access via SSH tunnel: ssh -L {PORT}:localhost:{PORT} user@{fqdn}")
            print(f"Then open: http://localhost:{PORT}")

    elif mode == "start-direct":
        print("Starting trustmux (direct HTTPS — self-signed cert)...")
        pid = _launch(["--host", "0.0.0.0", "--self-signed"])
        ok = pid is not None
        if ok:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except Exception:
                local_ip = "localhost"
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{local_ip}:{PORT}")
            print(f"  (browser will warn about self-signed cert — click through to proceed)")

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        return 1

    if not ok:
        print(f"trustmux failed to start — check {LOGFILE}")
        return 1
    return 0


def cmd_stop() -> int:
    p = _pid()
    if not p:
        print("trustmux not running")
        PIDFILE.unlink(missing_ok=True)
        return 0

    if PIDFILE.exists():
        try:
            file_pid = int(PIDFILE.read_text().strip())
            if file_pid != p:
                print(f"Error: pid {p} owns port {PORT} but PIDFILE contains {file_pid}.",
                      file=sys.stderr)
                print(f"Refusing to kill. Remove {PIDFILE} manually if trustmux is truly stopped.",
                      file=sys.stderr)
                return 1
        except ValueError:
            pass

    os.kill(p, signal.SIGTERM)
    print(f"trustmux stopped (pid {p})")
    PIDFILE.unlink(missing_ok=True)
    return 0


def cmd_status() -> int:
    p = _pid()
    if not p:
        print("trustmux not running")
        return 0

    print(f"trustmux running (pid {p}) — port {PORT}")
    try:
        out = subprocess.check_output(
            ["tailscale", "serve", "status"],
            stderr=subprocess.DEVNULL, text=True,
        )
        if f":{PORT}" in out:
            ts_host = _ts_host()
            if ts_host:
                print(f"Connect: https://{ts_host}")
            return 0
    except Exception:
        pass
    try:
        ts_ip = subprocess.check_output(
            ["tailscale", "ip", "-4"], stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        ts_ip = "localhost"
    print(f"Connect: http://{ts_ip}:{PORT}  (direct HTTP)")
    return 0


def cmd_log() -> int:
    _ensure_dir()
    try:
        subprocess.run(["tail", "-f", str(LOGFILE)])
    except KeyboardInterrupt:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="trustmux-ctl",
        description="Manage the Trustmux daemon",
        epilog="To remove tailscale serve config: tailscale serve reset",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_setup = sub.add_parser("setup", help="One-time setup: verify install, configure tailscale serve")
    p_setup.add_argument("--quiet", action="store_true", help="Suppress next-steps output")

    sub.add_parser("start",        help="Start daemon via tailscale serve (HTTPS — default)")
    sub.add_parser("serve",        help=argparse.SUPPRESS)   # alias
    sub.add_parser("start-local",  help="Start daemon loopback-only for SSH tunnel access")
    sub.add_parser("start-direct", help="Start daemon direct to Tailscale IP (HTTP — dev only)")
    sub.add_parser("stop",         help="Stop daemon (tailscale serve config persists)")
    sub.add_parser("restart",      help="Restart daemon")
    sub.add_parser("status",       help="Show running status and URL")
    sub.add_parser("log",          help="Tail the log file")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    if not shutil.which("tmux"):
        print("Warning: tmux not found in PATH — tmux (or Byobu) must be installed and running.",
              file=sys.stderr)
        print("Install tmux:  https://github.com/tmux/tmux/wiki/Installing", file=sys.stderr)
        print("Install Byobu: https://byobu.org", file=sys.stderr)

    cmd = args.cmd
    if cmd == "setup":
        sys.exit(cmd_setup(quiet=args.quiet))
    elif cmd in ("start", "serve"):
        sys.exit(cmd_start("serve"))
    elif cmd == "start-local":
        sys.exit(cmd_start("start-local"))
    elif cmd == "start-direct":
        sys.exit(cmd_start("start-direct"))
    elif cmd == "stop":
        sys.exit(cmd_stop())
    elif cmd == "restart":
        cmd_stop()
        time.sleep(0.5)
        sys.exit(cmd_start("serve"))
    elif cmd == "status":
        sys.exit(cmd_status())
    elif cmd == "log":
        sys.exit(cmd_log())


if __name__ == "__main__":
    main()
