"""Advertised addresses — what a phone connects to, and what the cert covers.

The address a daemon discovers for itself is not always one a phone can reach.
Behind cloud NAT it only ever sees an internal address, so both the URL
`trustmux pair` prints and the names in the self-signed certificate describe a
host nothing outside can route to.  An advertise *source* overrides that:

    34.1.2.3            a literal address
    tmux.example.com    a literal name
    host:8443           a literal with a port
    https://name/       a full URL, for a proxy terminating TLS on 443
    cmd:/path/to/prog   run prog; one value per line of its stdout

Sources are resolved afresh on every daemon start, which is what makes them
safe to keep in a config file: on a cloud instance with an ephemeral public
address a literal would quietly go stale across a stop/start cycle, while a
cmd: source re-asks each time.

Resolution is strict, and a failure stops the daemon rather than falling back
to the address it would have guessed.  Advertising is not decoration -- it
decides what the certificate attests -- and a browser rejects a certificate
that omits the name in the URL outright rather than offering the click-through
a self-signed one gets.  There is no useful "warning" outcome here.
"""
import ipaddress
import json
import os
import re
import shlex
import subprocess
import time
from typing import NamedTuple
from urllib.parse import urlsplit

from trustmux._paths import Instance

ADVERTISE_ENV = "TRUSTMUX_ADVERTISE"
CMD_PREFIX = "cmd:"

# Every cmd: source shares one budget, because this runs on the daemon's
# startup path ahead of the admin socket being bound and _ctl._launch() waits
# for that socket to decide whether the daemon came up.  One bounded total
# keeps that wait bounded however many sources there are.
CMD_BUDGET = 5.0
MAX_VALUES = 16
MAX_OUTPUT = 64 * 1024

# cmd: is split into argv and never handed to a shell, so these characters
# would reach the program as literal arguments rather than doing what they
# look like.  Rejecting them beats letting curl receive a '|' as a URL.
_SHELL_CHARS = frozenset("|&;<>$()`\n\r")

# Per RFC 1035 plus the usual leading-digit allowance.
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class AdvertiseError(Exception):
    """A source could not be resolved, or resolved to something unusable."""


class Advertised(NamedTuple):
    """One address to advertise."""
    url: str    # what pair and status print, always ending in '/'
    host: str   # what goes in the certificate, without IPv6 brackets


def _is_hostname(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    return all(_LABEL_RE.match(label) for label in value.split("."))


def _as_port(text: str, whole: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise AdvertiseError(f"invalid port {text!r} in {whole!r}") from None
    if not 1 <= port <= 65535:
        raise AdvertiseError(f"invalid port {port} in {whole!r} — expected 1-65535")
    return port


def _split_host_port(value: str) -> tuple[str, int | None]:
    """(host, port) from host, host:port, [v6] or [v6]:port."""
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep:
            raise AdvertiseError(f"unbalanced '[' in {value!r}")
        if rest.startswith(":"):
            return host, _as_port(rest[1:], value)
        if rest:
            raise AdvertiseError(f"trailing {rest!r} after ']' in {value!r}")
        return host, None
    # A bare IPv6 literal has several colons, so only a single one can be a
    # port separator; anything else is a host in its own right.
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, _as_port(port, value)
    return value, None


def _build(host: str, port: int | None, scheme: str, whole: str) -> Advertised:
    if host.endswith(".") and len(host) > 1:
        host = host[:-1]     # legal in DNS, not in a certificate SAN
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if not _is_hostname(host):
            raise AdvertiseError(f"invalid host {host!r} in {whole!r}") from None
    if ip is not None and ip.is_unspecified:
        raise AdvertiseError(f"{whole!r}: {host} is a wildcard, not an address "
                             "anything can connect to")
    default = 443 if scheme == "https" else 80
    port = default if port is None else port
    shown = "" if port == default else f":{port}"
    literal = f"[{host}]" if ip is not None and ip.version == 6 else host
    return Advertised(url=f"{scheme}://{literal}{shown}/", host=host)


def normalize(value: str, scheme: str = "https", port: int | None = None) -> Advertised:
    """One advertise value as (url, host), or AdvertiseError if it is not one.

    scheme and port supply what a bare host omits -- the daemon's own, so that
    `--advertise 34.1.2.3` on a daemon serving https on 7432 advertises
    https://34.1.2.3:7432/.  A full URL overrides both, which is how a proxy
    terminating TLS on 443 in front of the daemon is expressed.
    """
    if not value:
        raise AdvertiseError("empty value — a blank line is not an address")
    if value.startswith(CMD_PREFIX):
        # Only reachable for a value a cmd: source printed: resolve() routes
        # sources themselves to _run().  One level, so nothing recurses.
        raise AdvertiseError(f"{value!r}: a cmd: source cannot produce another one")
    if "://" in value:
        return _from_url(value)
    host, explicit = _split_host_port(value)
    return _build(host, port if explicit is None else explicit, scheme, value)


def _from_url(value: str) -> Advertised:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise AdvertiseError(f"unsupported scheme {parts.scheme!r} in {value!r} — "
                             "expected http or https")
    if parts.path not in ("", "/"):
        raise AdvertiseError(f"{value!r} has a path — trustmux serves from the "
                             "root only, so a subpath could not work")
    if parts.query or parts.fragment:
        raise AdvertiseError(f"{value!r} has a query or fragment — the pairing "
                             "code is carried in the fragment")
    if parts.username or parts.password:
        raise AdvertiseError(f"{value!r} carries credentials")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise AdvertiseError(f"invalid port in {value!r}") from None
    if not host:
        raise AdvertiseError(f"no host in {value!r}")
    return _build(host, port, parts.scheme, value)


def _argv(source: str) -> list[str]:
    """argv for a cmd: source, or AdvertiseError if it is not one we would run.

    No shell.  The command is split with shlex, which honours quoting but
    expands nothing, so a pipeline or a $(...) would reach the program as text
    rather than being evaluated -- saying so is friendlier than letting curl
    receive a '|' as a URL.
    """
    spec = source[len(CMD_PREFIX):]
    bad = sorted(_SHELL_CHARS & set(spec))
    if bad:
        chars = " ".join(repr(c) for c in bad)
        raise AdvertiseError(
            f"{source!r} contains {chars}: cmd: runs a program directly and does "
            "not use a shell, so this would be passed through as an argument. "
            "Put the pipeline in a script and name the script instead.")
    try:
        argv = shlex.split(spec)
    except ValueError as e:
        raise AdvertiseError(f"cannot parse {source!r}: {e}") from None
    if not argv:
        raise AdvertiseError(f"{source!r} names no command")
    return argv


def _run(source: str, deadline: float) -> list[str]:
    """Run a cmd: source, returning one value per line of its stdout.

    Any failure is fatal, and that is the point: a shell would turn a failed
    curl into an empty line and still exit 0, which is precisely the silent
    wrong answer advertising exists to prevent.
    """
    argv = _argv(source)
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise AdvertiseError(f"out of time before running {argv[0]} — advertise "
                             f"sources share a {CMD_BUDGET:g}s budget")
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=budget)
    except FileNotFoundError:
        raise AdvertiseError(f"{argv[0]}: not found (an absolute path is safest "
                             "here, since a daemon's PATH is whatever started "
                             "it)") from None
    except PermissionError:
        raise AdvertiseError(f"{argv[0]}: not executable") from None
    except subprocess.TimeoutExpired:
        raise AdvertiseError(f"{argv[0]}: still running after {budget:.1f}s") from None
    except OSError as e:
        raise AdvertiseError(f"{argv[0]}: {e}") from None

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise AdvertiseError(f"{argv[0]} exited {proc.returncode}"
                             + (f": {detail[-1]}" if detail else ""))
    if len(proc.stdout) > MAX_OUTPUT:
        raise AdvertiseError(f"{argv[0]} printed more than {MAX_OUTPUT} bytes")

    lines = proc.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()          # one trailing newline is how a program ends output
    if not lines:
        raise AdvertiseError(f"{argv[0]} printed nothing")
    return lines


def resolve(sources: list[str], scheme: str = "https",
            port: int | None = None) -> list[Advertised]:
    """Resolve sources to the addresses to advertise, in order.

    The first value decides the URL that pair prints; every value becomes a
    certificate SAN.  Repeats of a host are dropped rather than duplicated in
    the certificate, keeping the first.
    """
    deadline = time.monotonic() + CMD_BUDGET
    out: list[Advertised] = []
    seen: set[str] = set()
    for source in sources:
        for value in (_run(source, deadline) if source.startswith(CMD_PREFIX)
                      else [source]):
            try:
                adv = normalize(value, scheme, port)
            except AdvertiseError as e:
                # Name the source: which line of which program was unusable is
                # the only thing that makes this fixable.
                raise AdvertiseError(f"{source}: {e}" if source != value
                                     else str(e)) from None
            if adv.host in seen:
                continue
            seen.add(adv.host)
            out.append(adv)
            if len(out) > MAX_VALUES:
                raise AdvertiseError(f"more than {MAX_VALUES} addresses to advertise")
    return out


def check_sources(sources: list[str], scheme: str = "https",
                  port: int | None = None) -> None:
    """Validate sources as far as is possible without running anything.

    Lets the CLI reject a mistyped source itself, as the usage error it is,
    rather than launching a daemon that exits during startup.  Whether a program
    will actually succeed cannot be known here -- that is the daemon's business,
    and fatal there.
    """
    for source in sources:
        if source.startswith(CMD_PREFIX):
            _argv(source)
        else:
            normalize(source, scheme, port)


def _from_config(inst: Instance) -> list[str]:
    """Advertise sources from this instance's config file, or [] if it has none."""
    path = inst.config_file
    try:
        st = path.stat()
    except OSError:
        return []
    # A source can name a program to run, so a file anyone else can write is a
    # way to have this daemon run their code.
    if st.st_mode & 0o022:
        raise AdvertiseError(
            f"{path} is writable by group or other (mode {st.st_mode & 0o777:04o}) "
            "and can name a program to run — refusing to read it. chmod 600 it.")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise AdvertiseError(f"cannot read {path}: {e}") from None
    if not isinstance(data, dict):
        raise AdvertiseError(f"{path}: expected a JSON object")
    raw = data.get("advertise")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise AdvertiseError(f'{path}: "advertise" must be a string or a list '
                             "of strings")
    return raw


def resolve_sources(explicit: list[str] | None = None, no_advertise: bool = False,
                    inst: Instance | None = None) -> list[str]:
    """The sources to use: --advertise, then $TRUSTMUX_ADVERTISE, then the
    instance's config file.

    Whichever is set replaces the others outright rather than adding to them.
    With a repeatable flag an appending --advertise could never drop a name the
    config file still lists, and a certificate attesting a host you were trying
    to remove is the failure this exists to avoid.  --no-advertise is how to
    resolve to nothing despite a configured source.
    """
    if no_advertise:
        if explicit:
            raise AdvertiseError("--advertise and --no-advertise are mutually exclusive")
        return []
    if explicit:
        if not all(explicit):
            raise AdvertiseError("--advertise needs a value — use --no-advertise "
                                 "to advertise nothing")
        return list(explicit)
    # A single source, not a list: no delimiter is safe here, since ':' collides
    # with both URL schemes and the cmd: prefix while ',' can appear inside a
    # command's arguments.  A cmd: source already yields many values by itself,
    # so nothing is out of reach.
    env = os.environ.get(ADVERTISE_ENV, "").strip()
    if env:
        return [env]
    return _from_config(inst or Instance())


def advertised_urls(info: dict | None) -> list[str]:
    """The URLs a running daemon reports advertising, from an admin "info" reply.

    Only the daemon knows what its certificate was actually built for, so this
    -- not a fresh local resolution -- is what the CLI should print.  Shaped
    defensively because it crosses the socket.
    """
    urls = (info or {}).get("advertise")
    if not isinstance(urls, list):
        return []
    return [u for u in urls if isinstance(u, str) and u]
