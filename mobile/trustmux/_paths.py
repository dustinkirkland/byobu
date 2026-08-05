"""Where trustmux keeps its files.

XDG base directories, with one subdirectory per instance so that two daemons
never share a pid file, socket or token store:

    config  $XDG_CONFIG_HOME/trustmux   machines.json — the only thing the user
                                        writes by hand
    state   $XDG_STATE_HOME/trustmux    tokens.json, cert.pem, key.pem, log,
                                        admin socket, pid file

Everything the daemon owns lives together in the state directory, as it always
has -- just no longer mixed in with configuration.

Deliberately *not* $XDG_RUNTIME_DIR for the socket and pid file, despite that
being where the spec puts them: systemd-logind removes /run/user/$UID when a
user's last login session ends unless `loginctl enable-linger` is set, which is
off by default.  Trustmux exists to be started and then reached later from a
phone, so a daemon outliving the directory holding its socket -- alive but
unreachable -- is squarely on the main path.  It also does not exist on macOS
or in most containers.  Byobu has never used it either (usr/lib/byobu/include/
dirs.in).  Staleness is instead detected explicitly, by socket_is_live() below:
a socket whose daemon is gone refuses connections.

Each base honours a TRUSTMUX_*_DIR override, mirroring byobu's own
BYOBU_CONFIG_DIR.
"""
import errno
import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

_PKG = "trustmux"

DEFAULT_INSTANCE = "default"
INSTANCE_ENV = "TRUSTMUX_INSTANCE"
# Instance names are path components and are interpolated into the ~/.profile
# hook line, so keep them to an unambiguous alphabet: no separators, no
# leading dot.
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# Pre-instances layout: everything sat directly in the config directory.
_LEGACY_STATE_FILES = ("tokens.json", "cert.pem", "key.pem", "trustmux.log")


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    """User-authored configuration."""
    return (_env_dir("TRUSTMUX_CONFIG_DIR")
            or (_env_dir("XDG_CONFIG_HOME") or Path.home() / ".config") / _PKG)


def state_dir() -> Path:
    """Data that must survive a reboot: tokens, TLS keypair, logs."""
    return (_env_dir("TRUSTMUX_STATE_DIR")
            or (_env_dir("XDG_STATE_HOME") or Path.home() / ".local" / "state") / _PKG)



def machines_file() -> Path:
    """Sibling-machine list for the in-app selector; shared by all instances."""
    return config_dir() / "machines.json"


@dataclass(frozen=True)
class Instance:
    """One daemon's private state directory.

    Two daemons never share a pid file, socket or token store.
    """
    name: str = DEFAULT_INSTANCE

    @property
    def state(self) -> Path:
        return state_dir() / "instances" / self.name

    @property
    def tokens_file(self) -> Path:
        return self.state / "tokens.json"

    @property
    def cert_file(self) -> Path:
        return self.state / "cert.pem"

    @property
    def key_file(self) -> Path:
        return self.state / "key.pem"

    @property
    def log_file(self) -> Path:
        return self.state / "trustmux.log"

    @property
    def sock(self) -> Path:
        return self.state / "trustmux.sock"

    @property
    def pid_file(self) -> Path:
        return self.state / "trustmux.pid"

    def label(self) -> str:
        """' --name NAME' for non-default instances, for printed hints."""
        return "" if self.name == DEFAULT_INSTANCE else f" --name {self.name}"

    def ensure_dirs(self) -> None:
        self.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.state.chmod(0o700)


def resolve_instance(explicit: str | None = None) -> Instance:
    """--name, then $TRUSTMUX_INSTANCE, then 'default'. Exits 2 if invalid."""
    # An explicitly empty --name is an error, not a silent fall-through;
    # a blank environment variable is treated as unset.
    if explicit is None:
        name = os.environ.get(INSTANCE_ENV, "").strip() or DEFAULT_INSTANCE
    else:
        name = explicit
    if not INSTANCE_RE.match(name):
        print(f"Error: invalid instance name {name!r} — use letters, digits, "
              "'.', '_' or '-' (max 32, not starting with '.').", file=sys.stderr)
        sys.exit(2)
    inst = Instance(name)
    # Belt and braces: the regex already forbids separators, so this only fires
    # if that alphabet is ever widened.  Compares unresolved paths, so a
    # deliberately symlinked instance directory keeps working.
    if inst.state.parent.name != "instances":
        print(f"Error: instance name {name!r} escapes its directory.", file=sys.stderr)
        sys.exit(2)
    return inst


def check_sock_path(inst: Instance, stream=sys.stderr) -> bool:
    """False (with a message) if the admin socket path is too long to bind.

    struct sockaddr_un caps sun_path at 108 bytes on Linux and 104 on macOS;
    without this the failure surfaces as a bare OSError from bind().
    """
    limit = 104 if sys.platform == "darwin" else 108
    encoded = len(str(inst.sock).encode())
    if encoded < limit:
        return True
    print(f"Error: admin socket path is {encoded} bytes, over the {limit}-byte "
          f"limit for unix sockets:\n  {inst.sock}", file=stream)
    print("Use a shorter instance name, or set $TRUSTMUX_STATE_DIR to a shorter path.",
          file=stream)
    return False


def socket_is_live(path: Path) -> bool:
    """True if something is accepting connections on the unix socket at path.

    The state directory outlives the daemon, so a socket file left behind by a
    killed daemon stays on disk; connecting is the only way to tell that apart
    from one a daemon is serving.  This answers at the kernel level -- the
    connection completes off the listen backlog -- so a daemon that is alive
    but too busy to reply still reads as live, which is exactly what `stop`
    needs in order to be able to kill a wedged one.

    Shared by the daemon (deciding whether it may unlink a leftover socket) and
    the CLI (deciding whether this instance has a daemon at all), so that the
    two can never disagree about what "running" means.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def known_instances() -> list[Instance]:
    """Every instance with a state directory, default first."""
    try:
        names = sorted(p.name for p in (state_dir() / "instances").iterdir()
                       if p.is_dir())
    except OSError:
        names = []
    if DEFAULT_INSTANCE in names:
        names.remove(DEFAULT_INSTANCE)
        names.insert(0, DEFAULT_INSTANCE)
    return [Instance(n) for n in names]


def legacy_dir() -> Path:
    """Where the pre-instances layout kept everything: the config directory.

    Derived from config_dir() rather than hardcoding ~/.config/trustmux, so
    that overriding the base directories fully isolates a run — otherwise a
    test or a dev-tree invocation would reach into the real one.
    """
    return config_dir()


def _move_preserving_mode(src: Path, dst: Path) -> None:
    """Move src to dst without ever widening its permissions.

    os.replace is atomic but fails across filesystems, which is easy to hit now
    that state lives under a different base directory than config.  The
    fallback recreates the file with the source's mode from the outset rather
    than copying and chmod-ing after, so a 0600 secret is never briefly world
    readable.

    A symlink is copied through rather than renamed: os.replace would move the
    link itself, leaving the state directory owning a pointer back to the old
    location instead of the secret.  st_mode is the target's, not the link's.
    """
    mode = src.stat().st_mode & 0o777
    if not src.is_symlink():
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
    data = src.read_bytes()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(dst, mode)
    src.unlink()


def migrate_legacy_layout(stream=sys.stderr) -> bool:
    """Move pre-instances files into the default instance's state directory.

    Per-file and idempotent, so a partial failure can be retried.  Returns True
    if anything moved.  Never raises: a migration problem must not stop the
    command the user actually asked for.

    The stale top-level pid file and socket are left alone rather than removed:
    a daemon from before the upgrade may still be serving on that socket.
    """
    legacy = legacy_dir()
    target = Instance().state
    pending = [n for n in _LEGACY_STATE_FILES
               if (legacy / n).exists() and not (target / n).exists()]
    if not pending:
        return False

    moved, failed = [], []
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        print(f"trustmux: cannot create {target}: {e}", file=stream)
        return False

    for name in pending:
        try:
            _move_preserving_mode(legacy / name, target / name)
            moved.append(name)
        except OSError as e:
            failed.append(f"{name} ({e})")

    if moved:
        print(f"trustmux: moved {', '.join(moved)} to {target}", file=stream)
    if failed:
        print(f"trustmux: could not move {', '.join(failed)} out of {legacy} — "
              "move them by hand or they will be ignored.", file=stream)
    return bool(moved)
