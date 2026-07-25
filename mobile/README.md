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

---

## Files

Trustmux follows the XDG base directory spec.

| Path | Purpose |
|---|---|
| `$XDG_CONFIG_HOME/trustmux/machines.json` | Optional: sibling machines for the machine selector — the only user-authored file |
| `$XDG_STATE_HOME/trustmux/instances/default/tokens.json` | Paired device session tokens (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/default/cert.pem`, `key.pem` | Self-signed TLS keypair for `start-direct` |
| `$XDG_STATE_HOME/trustmux/instances/default/trustmux.log` | Daemon log (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/default/trustmux.sock` | Admin Unix socket (mode 0600) |
| `$XDG_STATE_HOME/trustmux/instances/default/trustmux.pid` | PID file |

Defaults are `~/.config` and `~/.local/state`. Config holds only the file you
write by hand; everything the daemon owns lives together under state, as it
always has — just no longer mixed in with configuration.

The socket and pid file stay here rather than in `$XDG_RUNTIME_DIR`, where the
spec would put them. systemd-logind deletes `/run/user/$UID` when your last
login session ends unless `loginctl enable-linger` is set, which would strand a
still-running daemon with no socket to reach it by — and being started and then
reached later is the whole point of trustmux. That directory also doesn't exist
on macOS or in most containers. Leftovers are detected instead of swept away: a
socket refuses connections once its daemon is gone, and a recorded pid is only
believed when it's confirmed to hold the port in question.

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
`trustmux.log` are moved into the state directory above with their modes
preserved. A stale `trustmux.pid`/`trustmux.sock` is left alone, in
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
