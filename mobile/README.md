# Trustmux

A mobile companion for [tmux](https://github.com/tmux/tmux) / [Byobu](https://byobu.org) sessions. Run a lightweight daemon on your workstation; monitor and interact with your terminal sessions from your phone over your Tailscale network. No relay server — terminal data stays between your devices.

Works with **plain tmux** and with **byobu**. Byobu users get the live status bar chips; plain tmux users get everything else.

Two tiers:
- **Free** — PWA. Install one icon per machine directly from the browser.
- **Paid** — Native Flutter app with full ANSI colors, multi-machine management, and biometric auth.

---

## Requirements

- tmux (byobu optional but recommended)
- Python 3.10+
- [Tailscale](https://tailscale.com) — only for the default `start` mode, which
  serves over your tailnet. `start-direct` (self-signed HTTPS, binds **all**
  interfaces — reachable from anywhere the host is) and `start-local` (loopback
  only, reached through an SSH tunnel) need no Tailscale at all

---

## Install

### Homebrew (macOS / Linux)

```bash
brew tap dustinkirkland/trustmux
brew install trustmux
trustmux enable    # configure tailscale serve + start daemon
trustmux pair      # generate pairing code; enter on phone
```

### pip (PyPI)

```bash
pip install trustmux
trustmux enable
trustmux pair
```

### Debian / Ubuntu (.deb)

Trustmux is bundled with byobu — installing byobu brings trustmux along:

```bash
sudo apt install byobu
trustmux enable
trustmux pair
```

Or with the PPA for the latest release:

```bash
sudo add-apt-repository ppa:dustinkirkland/byobu
sudo apt install byobu
trustmux enable
trustmux pair
```

---

## Daily use

```bash
trustmux start      # start daemon
trustmux stop       # stop daemon
trustmux restart    # restart daemon
trustmux status     # show URL and running status
trustmux log        # tail the daemon log

trustmux pair           # generate a pairing code for a new device
trustmux unpair         # list paired devices and remove them
```

### A different port

The daemon listens on 7432 by default. `--port` (or `$TRUSTMUX_PORT`) changes
it for `setup`, `start`, `start-local`, `start-direct`, `stop`, `restart`,
`status` and `enable`:

```bash
trustmux start --port 3389
trustmux status              # finds it — no need to repeat --port
```

`stop`, `status` and `pair` ask the running daemon which port it is on, so only
the start command needs the flag. `enable --port` records it in the login hook.

### An address this host cannot see

Behind NAT — a cloud instance, most often — the daemon only ever sees an
internal address, so both the URL `pair` prints and the names in the
`start-direct` certificate describe a host nothing outside can route to.
`--advertise` says what a phone should use instead:

```bash
trustmux start-direct --advertise 203.0.113.45
trustmux start-direct --advertise https://tmux.example.com/   # proxy on :443
```

A bare host takes the daemon's own scheme and port; a full URL overrides both,
which is how a reverse proxy terminating TLS on 443 is expressed. Repeat the
flag for more than one, and the first supplies the printed URL.

Every advertised address also goes into the self-signed certificate. It has to:
a browser rejects a certificate that omits the name in the URL bar outright,
rather than offering the click-through a self-signed one gets.

`--advertise` changes only what is published, not what the daemon binds — you
still need a firewall rule admitting the port, which does put the daemon on the
public internet behind just the pairing code and session token.

#### An address that changes

A cloud instance with an ephemeral public address gets a different one across a
stop/start cycle, so a literal written down once goes quietly stale. `cmd:`
names a program to run instead, re-run on every daemon start, one address per
line of its output:

```bash
cat > /usr/local/bin/external-ip <<'EOF'
#!/bin/sh
exec curl --fail --silent -H Metadata-Flavor:Google \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip
EOF
chmod +x /usr/local/bin/external-ip

trustmux start-direct --advertise cmd:/usr/local/bin/external-ip
```

The program is split into arguments with shell-style quoting but is **not** run
by a shell — no pipelines, no `$(...)`; those characters are rejected rather
than passed through, so put a pipeline in a script and name the script. Note
`--fail` above: without it `curl` prints an error page and exits 0.

Resolution is strict and any failure stops the daemon: a non-zero exit, no
output, a blank line, a value that is not an address, or more than 5 seconds
across all sources. A shell wrapper whose `curl` fails typically prints an
empty line and still exits 0, and reading that as "nothing to advertise" would
hand out an unreachable URL and a certificate for the wrong host — neither of
which a warning would fix.

To keep a source across reboots put it in the instance's config file, which is
also the only place a login hook will find it:

```bash
mkdir -p ~/.config/trustmux/instances
echo '{"advertise": ["cmd:/usr/local/bin/external-ip"]}' \
  > ~/.config/trustmux/instances/default.json
chmod 600 ~/.config/trustmux/instances/default.json
```

`--advertise` then `$TRUSTMUX_ADVERTISE` then that file, and whichever is set
replaces the others outright rather than adding to them — otherwise a
repeatable flag could never drop a name the file still lists.
`--no-advertise` advertises nothing despite a configured source.

### Several daemons at once

`--name NAME` (or `$TRUSTMUX_INSTANCE`) gives a daemon its own pid file,
admin socket, log, session tokens and TLS certificate, so more than one can run
side by side — on different ports, or on the same port at different addresses:

```bash
trustmux start-direct --name work --port 3389
trustmux pair  --name work
trustmux list
trustmux stop  --name work
trustmux rm    --name work    # delete its state directory for good
```

An instance is created by its first `start` and otherwise lasts forever, so
`rm` is how you get rid of one — it deletes the state directory (session
tokens and TLS keypair included) and removes that instance's login hook, so it
does not come back at the next login. It refuses while the daemon is running,
and refuses on `default`; `--force` overrides both, stopping the daemon first.

The unnamed instance is called `default`; it is not special-cased, and lives
under `instances/default/` like any other.

Only `default` can use `tailscale serve` mode, because `serve` publishes on the
tailnet's port 443 and only one daemon can own it — a second would silently
take over the mapping. Named instances use `start-direct` or `start-local`;
`setup`, `start`, `restart` and `enable` refuse them with a message saying so.
Note `--port` does not lift this: it moves the loopback backend that
`tailscale serve` proxies to, not the tailnet-facing port.

---

## Setup from source

```bash
cd mobile/
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python trustmux.in --help
```

`trustmux.in` and `trustmuxd.in` find the package in the sibling `trustmux/`
directory, so no install step is needed. To run a throwaway daemon that leaves
your real one untouched, point both base directories at a scratch tree:

```bash
export TRUSTMUX_CONFIG_DIR=$(mktemp -d)
export TRUSTMUX_STATE_DIR=$TRUSTMUX_CONFIG_DIR/state
.venv/bin/python trustmux.in start-local --port 3389
```

`start`, `stop`, `status` and `list` only ever act on a daemon the instance
itself started, which they establish from that instance's own admin socket and
pid file — never by asking the system who holds a port. So a scratch tree is
isolated even if it shares a port with your real daemon; `--port` above just
avoids the two fighting over the bind.

---

## Files

Trustmux follows the XDG base directory spec, with one subdirectory per
instance. `<I>` below is the `--name` name, or `default`.

| Path | Purpose |
|---|---|
| `$XDG_CONFIG_HOME/trustmux/machines.json` | Optional: sibling machines for the machine selector. Shared by all instances |
| `$XDG_CONFIG_HOME/trustmux/instances/<I>.json` | Optional: per-instance settings, hand-written. `{"advertise": [...]}`. Refused if group- or world-writable, since a source can name a program to run |
| `$XDG_STATE_HOME/trustmux/instances/<I>/tokens.json` | Paired device session tokens (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/<I>/cert.pem`, `key.pem` | Self-signed TLS keypair for `start-direct` |
| `$XDG_STATE_HOME/trustmux/instances/<I>/trustmux.log` | Daemon log (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/<I>/trustmux.sock` | Admin Unix socket (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/<I>/trustmux.pid` | PID file — `<pid> <port>` |

Defaults are `~/.config` and `~/.local/state`. Config holds only the files you
write by hand; everything the daemon owns lives together under state, as it
always has — just no longer mixed in with configuration. An advertise source is
config rather than state because it records intent, not a result: it is
re-resolved on every start, which is what makes writing one down safe even when
the address behind it changes.

The socket and pid file stay here rather than in `$XDG_RUNTIME_DIR`, where the
spec would put them. systemd-logind deletes `/run/user/$UID` when your last
login session ends unless `loginctl enable-linger` is set, which would strand a
still-running daemon with no socket to reach it by — and being started and then
reached later is the whole point of trustmux. That directory also doesn't exist
on macOS or in most containers. Leftovers are detected instead of swept away:
once a daemon is gone its socket refuses connections, which is how a stale one
is told from a live one. A daemon that still accepts connections but has
stopped replying is still running, so the pid file records `<pid> <port>` —
enough to stop a hung daemon without having to ask the system who holds a port.
Asking that question is what an earlier version did, via `lsof`; it answered
for the whole machine, so it could not tell one instance's daemon from another,
went blind across network namespaces, and needed a binary that isn't always
installed and doesn't always support `-ti:<port>`.

`TRUSTMUX_CONFIG_DIR` and `TRUSTMUX_STATE_DIR` override each base, taking
precedence over the XDG variables.

### Multiple machines

```json
[
  { "name": "work",     "url": "https://work-machine.tail1234.ts.net" },
  { "name": "personal", "url": "https://personal.tail1234.ts.net" }
]
```

**Upgrading:** earlier versions kept everything directly in
`~/.config/trustmux`. On first run `tokens.json`, `cert.pem`, `key.pem` and
`trustmux.log` are moved into the `default` instance's state directory with
their modes preserved. A stale `trustmux.pid`/`trustmux.sock` is left alone, in
case a daemon predating the upgrade is still serving on it.

---

## Security

- In the default mode the daemon binds to `127.0.0.1` only — not reachable from the network
- All traffic encrypted by Tailscale WireGuard; HTTPS via `tailscale serve`
- No relay server — terminal data never leaves your Tailscale mesh
- Pairing codes: 6-digit, 60-second TTL, single-use, max 3 attempts
- Session tokens: 256-bit random, stored at mode 0600

---

## Tests

```bash
cd mobile/
python3 -m unittest discover -s tests -t .
```

Needs `tornado` and `cryptography` (`pip install -r requirements.txt`). The
suite points `TRUSTMUX_CONFIG_DIR`/`TRUSTMUX_STATE_DIR` at a temporary tree, so it never
reads or writes your real trustmux state.

---

## Troubleshooting

**502 Bad Gateway** — tailscale serve is running but daemon isn't: `trustmux start`

**"Serve not enabled"** — visit the URL printed by `tailscale serve --bg 7432`

**Phone can't reach URL** — ensure Tailscale is active on the phone

**Need to re-pair** — run `trustmux pair` and enter the new code on the device
