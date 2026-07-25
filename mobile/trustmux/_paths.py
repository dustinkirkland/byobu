"""Where trustmux keeps its files.

XDG base directories, under a per-instance subdirectory:

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
dirs.in).  Staleness is instead detected explicitly: see _daemon's admin server
and _ctl's _pid().

Each base honours a TRUSTMUX_*_DIR override, mirroring byobu's own
BYOBU_CONFIG_DIR.
"""
import errno
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_PKG = "trustmux"

DEFAULT_INSTANCE = "default"

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
    """One daemon's state directory.

    Only the default instance exists for now; the layout is already keyed by
    name so that selecting another is purely a matter of plumbing.
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

    def ensure_dirs(self) -> None:
        self.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.state.chmod(0o700)





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
    """
    mode = src.stat().st_mode & 0o777
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
