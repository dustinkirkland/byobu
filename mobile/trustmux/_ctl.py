"""trustmux — manage the Trustmux daemon."""
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

DEFAULT_PORT = 7432
PORT_ENV   = "TRUSTMUX_PORT"
SERVE_PORT = 443  # tailscale serve terminates TLS on :443
CONFIG_DIR = Path.home() / ".config" / "trustmux"
LOGFILE    = CONFIG_DIR / "trustmux.log"
PIDFILE    = CONFIG_DIR / "trustmux.pid"
ADMIN_SOCK = CONFIG_DIR / "trustmux.sock"
TOKENS_FILE = CONFIG_DIR / "tokens.json"


def _check_tmux() -> bool:
    """Hard-error if tmux is absent; warn if no sessions exist. Returns False on hard error."""
    if not shutil.which("tmux") and not shutil.which("byobu"):
        print("", file=sys.stderr)
        print("Error: tmux is not installed — trustmux requires tmux to attach to sessions.", file=sys.stderr)
        print("  Install tmux:  https://github.com/tmux/tmux/wiki/Installing", file=sys.stderr)
        print("  Install Byobu: https://byobu.org", file=sys.stderr)
        print("", file=sys.stderr)
        return False
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
        sessions = [s for s in out.splitlines() if s]
    except (FileNotFoundError, subprocess.CalledProcessError):
        sessions = []
    if not sessions:
        print("", file=sys.stderr)
        print("Warning: no tmux sessions found — trustmux will start but has nothing to attach to.", file=sys.stderr)
        print("  Start a session first:  tmux new-session", file=sys.stderr)
        print("  Or launch Byobu:        byobu", file=sys.stderr)
        print("", file=sys.stderr)
    return True


def _check_tls() -> bool:
    """Return True if TLS cert generation is available, False (with message) if not."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.generate_private_key(ec.SECP256R1())
        return True
    except Exception as e:
        print("", file=sys.stderr)
        print("Error: TLS support is unavailable — trustmux refuses to start without encryption.", file=sys.stderr)
        print(f"  ({e})", file=sys.stderr)
        print("", file=sys.stderr)
        print("The 'cryptography' package is required. Fix with:", file=sys.stderr)
        print(f"  {sys.executable} -m pip install --upgrade cryptography", file=sys.stderr)
        print("", file=sys.stderr)
        return False


def _valid_port(value: object) -> int | None:
    """Return value as a TCP port number, or None if it is not one."""
    try:
        port = int(value)   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def daemon_info() -> dict | None:
    """Ask the running daemon what it is listening on.

    Returns {"pid", "host", "port", "scheme"} or None if no daemon answers.
    """
    if not ADMIN_SOCK.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(ADMIN_SOCK))
            s.sendall(b'{"action": "info"}\n')
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        info = json.loads(b"".join(chunks))
    except Exception:
        return None
    if not isinstance(info, dict) or "error" in info:
        return None
    return info


def resolve_port(explicit: int | None = None) -> int:
    """Return the port to operate on.

    Precedence: --port, then $TRUSTMUX_PORT, then whatever the running daemon
    reports, then the default.  Consulting the daemon is what lets stop/status/
    pair find a daemon that was started on a non-default port without having to
    repeat the flag.
    """
    if explicit is not None:
        port = _valid_port(explicit)
        if port is None:
            print(f"Error: invalid port {explicit!r} — expected 1-65535.", file=sys.stderr)
            sys.exit(2)
        return port

    env = os.environ.get(PORT_ENV, "").strip()
    if env:
        port = _valid_port(env)
        if port is None:
            print(f"Error: invalid {PORT_ENV}={env!r} — expected 1-65535.", file=sys.stderr)
            sys.exit(2)
        return port

    info = daemon_info()
    if info:
        port = _valid_port(info.get("port"))
        if port is not None:
            return port

    return DEFAULT_PORT


def port_opt(port: int) -> str:
    """' --port N' when port is not the default, else '' — for printed hints."""
    return "" if port == DEFAULT_PORT else f" --port {port}"


def _lan_ip() -> str:
    """Best-effort primary LAN address, or 'localhost' if undeterminable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def direct_url(port: int, info: dict | None = None) -> str:
    """URL for reaching the daemon directly (no tailscale serve in front).

    Uses the daemon's own reported bind address and scheme when it is running,
    so loopback-only and self-signed modes each print something usable.
    """
    if info is None:
        info = daemon_info() or {}
    # A live daemon's own port beats the caller's idea of it.
    port = _valid_port(info.get("port")) or port
    scheme = info.get("scheme") or "https"
    host = info.get("host") or ""
    hostname = "localhost" if host in ("127.0.0.1", "localhost", "::1") else _lan_ip()
    return f"{scheme}://{hostname}:{port}/"


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOGFILE.touch()
    LOGFILE.chmod(0o600)


def _pid(port: int = DEFAULT_PORT) -> int | None:
    """Return PID of process listening on port, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", f"-ti:{port}"], stderr=subprocess.DEVNULL, text=True
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


def _ensure_ts_serve(port: int = DEFAULT_PORT) -> bool:
    """Configure tailscale serve for port. Returns True on success."""
    try:
        out = subprocess.check_output(
            ["tailscale", "serve", "status"],
            stderr=subprocess.DEVNULL, text=True,
        )
        if f":{port}" in out:
            print(f"✓ tailscale serve already configured for port {port}")
            return True
    except Exception:
        pass

    print(f"Enabling tailscale serve (HTTPS → localhost:{port})...")
    try:
        subprocess.run(
            ["tailscale", "serve", "--bg", str(port)],
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
    print(f"  tailscale serve --bg {port}", file=sys.stderr)
    print("Then re-run: trustmux start", file=sys.stderr)
    return False


def _launch(port: int, extra_args: list[str]) -> int | None:
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
            [sys.executable, "-m", "trustmux", "--port", str(port)] + extra_args,
            stdout=log, stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    PIDFILE.write_text(str(proc.pid))
    time.sleep(0.5)
    return _pid(port)


def cmd_setup(quiet: bool = False, port: int | None = None) -> int:
    port = resolve_port(port)
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

    if not _ensure_ts_serve(port):
        return 1

    warn_if_peer_blocked()

    if not quiet:
        print("\nSetup complete. Next steps:\n")
        print(f"  1. Start the daemon:      trustmux start{port_opt(port)}")
        print("  2. Generate pairing code: trustmux pair")
        print(f"  3. Open on your phone:    https://{ts_host}")
    return 0


def cmd_start(mode: str = "serve", port: int | None = None) -> int:
    port = resolve_port(port)
    p = _pid(port)
    if p:
        print(f"trustmux already running (pid {p})")
        return 1

    if mode == "serve":
        if not _check_tmux():
            return 1
        if not _check_tls():
            return 1
        try:
            subprocess.run(["tailscale", "--version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("Error: tailscale not found.", file=sys.stderr)
            print("Install: https://tailscale.com/docs/install/linux", file=sys.stderr)
            print("Or use 'start-direct' for self-signed HTTPS without Tailscale.", file=sys.stderr)
            return 1
        ts_host = _ts_host()
        if not ts_host:
            print("Error: cannot determine Tailscale hostname (is tailscale up?)", file=sys.stderr)
            return 1
        if not _ensure_ts_serve(port):
            return 1
        print("Starting trustmux (HTTPS mode)...")
        pid = _launch(port, ["--host", "127.0.0.1", "--https"])
        ok = pid is not None
        if ok:
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{ts_host}")

    elif mode == "start-local":
        print("Starting trustmux (loopback only — SSH tunnel access)...")
        pid = _launch(port, ["--host", "127.0.0.1"])
        ok = pid is not None
        if ok:
            fqdn = socket.getfqdn()
            print(f"trustmux started (pid {pid})")
            print(f"Access via SSH tunnel: ssh -L {port}:localhost:{port} user@{fqdn}")
            print(f"Then open: http://localhost:{port}")

    elif mode == "start-direct":
        if not _check_tmux():
            return 1
        if not _check_tls():
            return 1
        print("Starting trustmux (direct HTTPS — self-signed cert)...")
        pid = _launch(port, ["--host", "0.0.0.0", "--self-signed"])
        ok = pid is not None
        if ok:
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{_lan_ip()}:{port}")
            print(f"  (browser will warn about self-signed cert — click through to proceed)")

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        return 1

    if not ok:
        print(f"trustmux failed to start — check {LOGFILE}")
        return 1
    return 0


def cmd_stop(port: int | None = None) -> int:
    port = resolve_port(port)
    p = _pid(port)
    if not p:
        print("trustmux not running")
        PIDFILE.unlink(missing_ok=True)
        return 0

    if PIDFILE.exists():
        try:
            file_pid = int(PIDFILE.read_text().strip())
            if file_pid != p:
                print(f"Error: pid {p} owns port {port} but PIDFILE contains {file_pid}.",
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


def cmd_status(port: int | None = None) -> int:
    info = daemon_info()
    port = resolve_port(port)
    p = _pid(port)
    if not p:
        print("trustmux not running")
        return 0

    print(f"trustmux running (pid {p}) — port {port}")
    try:
        out = subprocess.check_output(
            ["tailscale", "serve", "status"],
            stderr=subprocess.DEVNULL, text=True,
        )
        if f":{port}" in out:
            ts_host = _ts_host()
            if ts_host:
                print(f"Connect: https://{ts_host}")
            return 0
    except Exception:
        pass
    print(f"Connect: {direct_url(port, info)}")
    return 0


def cmd_log() -> int:
    _ensure_dir()
    try:
        subprocess.run(["tail", "-f", str(LOGFILE)])
    except KeyboardInterrupt:
        pass
    return 0


def _refuse_root() -> None:
    """Abort if running as root (e.g. via sudo).

    Running as root causes three distinct failure modes:
      1. Path.home() resolves to /root/, so PIDFILE/CONFIG_DIR point to root's
         home; the PID-mismatch safety check in cmd_stop() is silently bypassed
         because the user's PIDFILE is invisible to root.
      2. `tailscale serve` runs as root and creates a conflicting serve config,
         clobbering the user-level config and wedging tailscale.
      3. The daemon relaunches owned by root, creating a privilege-escalation
         risk and making the user-level `trustmux stop/status` inoperative.
    """
    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER", "")
        hint = f"  Run as your normal user: sudo -u {sudo_user} trustmux ..." if sudo_user else ""
        if not hint:
            hint = "  Drop sudo and run as your normal user account."
        print("Error: trustmux must not be run as root (or via sudo).", file=sys.stderr)
        print(hint, file=sys.stderr)
        print("  Running as root breaks tailscale serve config and daemon ownership.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    _refuse_root()
    parser = argparse.ArgumentParser(
        prog="trustmux",
        description="Manage the Trustmux daemon",
        epilog="To remove tailscale serve config: tailscale serve reset",
    )
    sub = parser.add_subparsers(dest="cmd")

    # Shared --port, attached to every subcommand that has to know which
    # listener it is acting on.
    port_opts = argparse.ArgumentParser(add_help=False)
    port_opts.add_argument("--port", type=int, metavar="PORT",
                           help=f"TCP port (default: {DEFAULT_PORT}, or ${PORT_ENV})")

    p_setup = sub.add_parser("setup", parents=[port_opts],
                             help="One-time setup: verify install, configure tailscale serve")
    p_setup.add_argument("--quiet", action="store_true", help="Suppress next-steps output")

    sub.add_parser("start",        parents=[port_opts], help="Start daemon via tailscale serve (HTTPS — default)")
    sub.add_parser("serve",        parents=[port_opts], help=argparse.SUPPRESS)   # alias
    sub.add_parser("start-local",  parents=[port_opts], help="Start daemon loopback-only for SSH tunnel access")
    sub.add_parser("start-direct", parents=[port_opts], help="Start daemon direct HTTPS (self-signed cert, no Tailscale)")
    sub.add_parser("stop",         parents=[port_opts], help="Stop daemon (tailscale serve config persists)")
    sub.add_parser("restart",      parents=[port_opts], help="Restart daemon")
    sub.add_parser("status",       parents=[port_opts], help="Show running status and URL")
    sub.add_parser("log",          help="Tail the log file")
    sub.add_parser("enable",       parents=[port_opts], help="Start daemon and install login hook for automatic start")
    sub.add_parser("disable",      help="Stop daemon and remove login hook")
    sub.add_parser("pair",         help="Generate a one-time pairing code for a new device")
    sub.add_parser("unpair",       help="List paired devices and revoke tokens")
    sub.add_parser("help",         help=argparse.SUPPRESS)  # alias for -h/--help

    args = parser.parse_args()
    if not args.cmd or args.cmd == "help":
        parser.print_help()
        sys.exit(0 if args.cmd == "help" else 1)

    cmd = args.cmd
    # Resolve once: restart must reuse the running daemon's port after stopping
    # it, by which point the daemon can no longer be asked.
    port = resolve_port(args.port) if hasattr(args, "port") else DEFAULT_PORT

    if cmd == "setup":
        sys.exit(cmd_setup(quiet=args.quiet, port=port))
    elif cmd in ("start", "serve"):
        sys.exit(cmd_start("serve", port))
    elif cmd == "start-local":
        sys.exit(cmd_start("start-local", port))
    elif cmd == "start-direct":
        sys.exit(cmd_start("start-direct", port))
    elif cmd == "stop":
        sys.exit(cmd_stop(port))
    elif cmd == "restart":
        cmd_stop(port)
        time.sleep(0.5)
        sys.exit(cmd_start("serve", port))
    elif cmd == "status":
        sys.exit(cmd_status(port))
    elif cmd == "log":
        sys.exit(cmd_log())
    elif cmd == "enable":
        from trustmux._enable import main as _run_enable
        _run_enable(port)
    elif cmd == "disable":
        from trustmux._disable import main as _run_disable
        _run_disable()
    elif cmd == "pair":
        from trustmux._pair import main as _run_pair
        _run_pair()
    elif cmd == "unpair":
        from trustmux._unpair import main as _run_unpair
        _run_unpair()


if __name__ == "__main__":
    main()
