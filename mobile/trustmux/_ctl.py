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

from trustmux._paths import (DEFAULT_INSTANCE, INSTANCE_ENV, Instance,
                             check_sock_path, known_instances,
                             migrate_legacy_layout, resolve_instance,
                             socket_is_live, state_dir)

DEFAULT_PORT = 7432
PORT_ENV   = "TRUSTMUX_PORT"
SERVE_PORT = 443  # tailscale serve terminates TLS on :443


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


def daemon_info(inst: Instance | None = None) -> dict | None:
    """Ask this instance's running daemon what it is listening on.

    Returns {"pid", "host", "port", "scheme"} or None if no daemon answers.
    """
    inst = inst or Instance()
    if not inst.sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            # This is a local IPC round trip to a process on the same
            # machine, not a network call -- a healthy daemon answers in low
            # milliseconds. Several call paths now consult this (resolve_port,
            # direct_url, and _pid(), for which it is the primary source), so a
            # generous timeout here means an unresponsive-but-alive daemon
            # turns ordinary commands into a multi-second hang instead of an
            # instant response.
            s.settimeout(1.5)
            s.connect(str(inst.sock))
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


def resolve_port(explicit: int | None = None, inst: Instance | None = None) -> int:
    """Return the port to operate on.

    Precedence: --port, then $TRUSTMUX_PORT, then whatever this instance's
    running daemon reports, then the port it was recorded as starting on, then
    the default.  Consulting the daemon is what lets stop/status/pair find a
    daemon that was started on a non-default port without having to repeat the
    flag; the recorded port keeps that working when the daemon is alive but no
    longer answering, which is exactly when `stop` matters most.
    """
    inst = inst or Instance()
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

    info = daemon_info(inst)
    if info:
        port = _valid_port(info.get("port"))
        if port is not None:
            return port

    # Not answering.  Only believe the recorded port if the socket shows a
    # daemon still there: a pid file left by a killed daemon must not send the
    # next `start` to some port the user did not ask for.
    recorded = _recorded(inst)
    if recorded and recorded[1] is not None and socket_is_live(inst.sock):
        return recorded[1]

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


def _ensure_dir(inst: Instance | None = None) -> None:
    inst = inst or Instance()
    inst.ensure_dirs()
    inst.log_file.touch()
    inst.log_file.chmod(0o600)


def write_pid_file(inst: Instance, pid: int, port: int) -> None:
    """Record which pid this instance started, and on which port.

    The port is written alongside the pid because the pid alone cannot answer
    the question `stop`/`status` actually asks -- "is *our* daemon on this
    port?" -- and every way of asking the operating system that question
    afterwards is either unportable or system-wide (see _recorded()).
    """
    inst.pid_file.write_text(f"{pid} {port}\n")


def _recorded(inst: Instance) -> tuple[int, int | None] | None:
    """(pid, port) from this instance's pid file, if that pid is still alive.

    port is None for a file written before the port was recorded; such a file
    can still identify the daemon but can no longer place it on a port.

    Nothing here proves the pid is a trustmux daemon rather than a recycled
    number -- callers must first establish that this instance has a live daemon
    at all, which is what socket_is_live() is for.
    """
    if not inst.pid_file.exists():
        return None
    try:
        fields = inst.pid_file.read_text().split()
        pid = int(fields[0])
        os.kill(pid, 0)
    except (IndexError, ValueError, ProcessLookupError, PermissionError, OSError):
        return None
    return pid, _valid_port(fields[1]) if len(fields) > 1 else None


def _pid(port: int = DEFAULT_PORT, inst: Instance | None = None) -> int | None:
    """Return the PID of *this instance's* daemon listening on `port`, or None.

    Everything consulted here lives inside this instance's own state directory:
    the admin socket and the pid file.  Nothing system-wide is asked, which is
    the whole point -- see the second invariant below.

    The admin socket answers both halves of the question at once.  Whatever
    replies on it is by construction the daemon this instance started (the
    socket is 0600, inside the instance's state directory, and a starting
    daemon refuses to steal a live one), and its reply names the port it
    actually bound.  Merely *connecting* is a weaker but still useful signal:
    it completes off the kernel's listen backlog, so it succeeds even for a
    daemon too wedged to reply, and fails for a socket whose daemon was killed.
    That is what makes `stop` able to kill a hung daemon: the live socket
    proves this instance has one, and the pid file names it and its port.

    Two invariants, each from a bug that shipped:

    * Never return a pid without confirming it owns the *requested* port.
      Violating it let `trustmux stop --port <port nothing is on>` kill an
      unrelated daemon (11bb4ca7).
    * Never return a pid this instance does not claim.  Discovering the daemon
      by asking the system who holds the port let a throwaway instance report
      -- and stop -- a real daemon that merely happened to share it.
    """
    inst = inst or Instance()

    info = daemon_info(inst)
    if info is not None:
        pid = info.get("pid")
        if (isinstance(pid, int) and pid > 0
                and _valid_port(info.get("port")) == port):
            return pid
        # Our daemon answered, about some other port: it is not on this one.
        return None

    # No reply.  If the socket still accepts connections the daemon is alive
    # but not answering; fall back to what was recorded when it was started.
    # If it does not, this instance has no daemon, whoever else holds the port.
    if not socket_is_live(inst.sock):
        return None
    recorded = _recorded(inst)
    if recorded is None:
        return None
    pid, recorded_port = recorded
    return pid if recorded_port == port else None


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


def _launch(port: int, extra_args: list[str], inst: Instance | None = None) -> int | None:
    """Launch daemon as a detached background process. Returns PID or None."""
    inst = inst or Instance()
    _ensure_dir(inst)
    # Ensure the package directory is on the subprocess's Python path so that
    # `python3 -m trustmux` resolves correctly regardless of how Python was
    # invoked (e.g. bare /usr/bin/python3 from a .deb shim).
    pkg_parent = str(Path(__file__).parent.parent)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}:{existing}" if existing else pkg_parent
    with inst.log_file.open("a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "trustmux",
             "--port", str(port), "--instance", inst.name] + extra_args,
            stdout=log, stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    write_pid_file(inst, proc.pid, port)
    time.sleep(0.5)
    if proc.poll() is not None:
        # Died on startup — usually the port is taken, which is easy to hit
        # once more than one instance is in play.  Without this the pid file
        # still names the (zombie) child and the caller reports success.
        inst.pid_file.unlink(missing_ok=True)
        return None
    return _pid(port, inst)


def can_use_serve(inst: Instance, stream=sys.stderr) -> bool:
    """False (with a message) if this instance must not use serve mode.

    `tailscale serve` publishes on the tailnet's port 443, which only one
    daemon can own: a second `tailscale serve --bg` silently replaces the
    first instance's mapping, leaving it running but unreachable.  Per-instance
    --port does not help, since that only moves the loopback backend the proxy
    forwards to, not the tailnet-facing port.
    """
    if inst.name == DEFAULT_INSTANCE:
        return True
    print(f"Error: instance {inst.name!r} cannot use tailscale serve mode.", file=stream)
    print("  `tailscale serve` publishes on the tailnet's port 443, which only one",
          file=stream)
    print("  daemon can own — a second one would silently take over the mapping.",
          file=stream)
    print("  Use a mode that binds its own port instead:", file=stream)
    print(f"    trustmux start-direct{inst.label()} --port <port>", file=stream)
    print(f"    trustmux start-local{inst.label()} --port <port>", file=stream)
    return False


def cmd_setup(quiet: bool = False, port: int | None = None,
              inst: Instance | None = None) -> int:
    inst = inst or Instance()
    # setup exists to configure tailscale serve, so there is nothing it can
    # usefully do for an instance that is not allowed to use it.
    if not can_use_serve(inst):
        return 1
    port = resolve_port(port, inst)
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
        opts = f"{inst.label()}{port_opt(port)}"
        print("\nSetup complete. Next steps:\n")
        print(f"  1. Start the daemon:      trustmux start{opts}")
        print(f"  2. Generate pairing code: trustmux pair{inst.label()}")
        print(f"  3. Open on your phone:    https://{ts_host}")
    return 0


def cmd_start(mode: str = "serve", port: int | None = None,
              inst: Instance | None = None) -> int:
    inst = inst or Instance()
    if mode == "serve" and not can_use_serve(inst):
        return 1
    if not check_sock_path(inst):
        return 1
    port = resolve_port(port, inst)
    p = _pid(port, inst)
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
        pid = _launch(port, ["--host", "127.0.0.1", "--https"], inst)
        ok = pid is not None
        if ok:
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{ts_host}")

    elif mode == "start-local":
        print("Starting trustmux (loopback only — SSH tunnel access)...")
        pid = _launch(port, ["--host", "127.0.0.1"], inst)
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
        pid = _launch(port, ["--host", "0.0.0.0", "--self-signed"], inst)
        ok = pid is not None
        if ok:
            print(f"trustmux started (pid {pid})")
            print(f"Connect: https://{_lan_ip()}:{port}")
            print(f"  (browser will warn about self-signed cert — click through to proceed)")

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        return 1

    if not ok:
        # _pid() only names daemons this instance claims, so a port held by
        # someone else no longer surfaces as "already running" -- it lands here
        # instead, as a failure to bind.  Name that as the likely cause: the
        # daemon logged the real errno, but nobody reads a log on a hunch.
        print(f"trustmux failed to start — check {inst.log_file}")
        print(f"  (most often port {port} is already in use — try another "
              f"--port)", file=sys.stderr)
        return 1
    return 0


def cmd_stop(port: int | None = None, inst: Instance | None = None) -> int:
    inst = inst or Instance()
    port = resolve_port(port, inst)
    p = _pid(port, inst)
    if not p:
        print("trustmux not running")
        # "Nothing found on *this* port" does not mean the pid file is stale
        # -- it may correctly be tracking this instance's daemon on another
        # port. Only clean it up when its own recorded pid is actually dead.
        if inst.pid_file.exists() and _recorded(inst) is None:
            inst.pid_file.unlink(missing_ok=True)
        return 0

    if inst.pid_file.exists():
        try:
            file_pid = int(inst.pid_file.read_text().split()[0])
            if file_pid != p:
                print(f"Error: pid {p} owns port {port} but {inst.pid_file} contains {file_pid}.",
                      file=sys.stderr)
                print(f"Refusing to kill. Remove {inst.pid_file} manually if trustmux is truly stopped.",
                      file=sys.stderr)
                return 1
        except (IndexError, ValueError):
            pass

    os.kill(p, signal.SIGTERM)
    print(f"trustmux stopped (pid {p})")
    inst.pid_file.unlink(missing_ok=True)
    return 0


def cmd_status(port: int | None = None, inst: Instance | None = None) -> int:
    inst = inst or Instance()
    info = daemon_info(inst)
    port = resolve_port(port, inst)
    p = _pid(port, inst)
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


def cmd_list() -> int:
    instances = known_instances()
    if not instances:
        print("no trustmux instances yet")
        return 0
    print(f"{'INSTANCE':<16} {'PORT':>5}  {'SCHEME':<6} STATUS")
    for inst in instances:
        info = daemon_info(inst) or {}
        port = _valid_port(info.get("port"))
        if info:
            p = _pid(port or DEFAULT_PORT, inst)
        else:
            # No reply, but a socket still accepting connections means the
            # daemon is there and merely not answering; the pid file then
            # supplies both the pid and the port it was started on.
            recorded = _recorded(inst) if socket_is_live(inst.sock) else None
            p, port = recorded if recorded else (None, port)
        status = f"running (pid {p})" if p else "stopped"
        print(f"{inst.name:<16} {port or '-':>5}  {info.get('scheme', '-'):<6} {status}")
    return 0


def cmd_log(inst: Instance | None = None) -> int:
    inst = inst or Instance()
    _ensure_dir(inst)
    try:
        subprocess.run(["tail", "-f", str(inst.log_file)])
    except KeyboardInterrupt:
        pass
    return 0


def _refuse_root() -> None:
    """Abort if running as root (e.g. via sudo).

    Running as root causes three distinct failure modes:
      1. Path.home() resolves to /root/, so the state and runtime directories
         point at root's home; the PID-mismatch safety check in cmd_stop() is
         silently bypassed because the user's pid file is invisible to root.
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

    # --instance selects which daemon's state directory to act on and so is
    # accepted everywhere; --port only matters to commands that address a
    # listener.
    inst_opts = argparse.ArgumentParser(add_help=False)
    inst_opts.add_argument("--instance", metavar="NAME",
                           help=f"Instance name (default: {DEFAULT_INSTANCE}, or ${INSTANCE_ENV})")

    port_opts = argparse.ArgumentParser(add_help=False)
    port_opts.add_argument("--port", type=int, metavar="PORT",
                           help=f"TCP port (default: {DEFAULT_PORT}, or ${PORT_ENV})")

    both = [inst_opts, port_opts]

    p_setup = sub.add_parser("setup", parents=both,
                             help="One-time setup: verify install, configure tailscale serve")
    p_setup.add_argument("--quiet", action="store_true", help="Suppress next-steps output")

    sub.add_parser("start",        parents=both, help="Start daemon via tailscale serve (HTTPS — default)")
    sub.add_parser("serve",        parents=both, help=argparse.SUPPRESS)   # alias
    sub.add_parser("start-local",  parents=both, help="Start daemon loopback-only for SSH tunnel access")
    sub.add_parser("start-direct", parents=both, help="Start daemon direct HTTPS (self-signed cert, no Tailscale)")
    sub.add_parser("stop",         parents=both, help="Stop daemon (tailscale serve config persists)")
    sub.add_parser("restart",      parents=both, help="Restart daemon")
    sub.add_parser("status",       parents=both, help="Show running status and URL")
    sub.add_parser("enable",       parents=both, help="Start daemon and install login hook for automatic start")
    sub.add_parser("log",          parents=[inst_opts], help="Tail the log file")
    sub.add_parser("disable",      parents=[inst_opts], help="Stop daemon and remove login hook")
    sub.add_parser("pair",         parents=[inst_opts], help="Generate a one-time pairing code for a new device")
    sub.add_parser("unpair",       parents=[inst_opts], help="List paired devices and revoke tokens")
    sub.add_parser("list",         help="List instances and their listeners")
    sub.add_parser("help",         help=argparse.SUPPRESS)  # alias for -h/--help

    args = parser.parse_args()
    if not args.cmd or args.cmd == "help":
        parser.print_help()
        sys.exit(0 if args.cmd == "help" else 1)

    migrate_legacy_layout()

    cmd = args.cmd
    inst = resolve_instance(getattr(args, "instance", None))
    # Resolve once: restart must reuse the running daemon's port after stopping
    # it, by which point the daemon can no longer be asked.
    port = resolve_port(args.port, inst) if hasattr(args, "port") else DEFAULT_PORT

    if cmd == "setup":
        sys.exit(cmd_setup(quiet=args.quiet, port=port, inst=inst))
    elif cmd in ("start", "serve"):
        sys.exit(cmd_start("serve", port, inst))
    elif cmd == "start-local":
        sys.exit(cmd_start("start-local", port, inst))
    elif cmd == "start-direct":
        sys.exit(cmd_start("start-direct", port, inst))
    elif cmd == "stop":
        sys.exit(cmd_stop(port, inst))
    elif cmd == "restart":
        # restart brings the daemon back in serve mode, so check the serve
        # restriction before stopping anything -- otherwise a named instance
        # gets stopped and then refused, leaving it down.
        if not can_use_serve(inst):
            sys.exit(1)
        cmd_stop(port, inst)
        time.sleep(0.5)
        sys.exit(cmd_start("serve", port, inst))
    elif cmd == "status":
        sys.exit(cmd_status(port, inst))
    elif cmd == "list":
        sys.exit(cmd_list())
    elif cmd == "log":
        sys.exit(cmd_log(inst))
    elif cmd == "enable":
        from trustmux._enable import main as _run_enable
        _run_enable(port, inst)
    elif cmd == "disable":
        from trustmux._disable import main as _run_disable
        _run_disable(inst)
    elif cmd == "pair":
        from trustmux._pair import main as _run_pair
        _run_pair(inst)
    elif cmd == "unpair":
        from trustmux._unpair import main as _run_unpair
        _run_unpair(inst)


if __name__ == "__main__":
    main()
