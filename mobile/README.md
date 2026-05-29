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
- [Tailscale](https://tailscale.com) installed, running, and connected

---

## Install

### Homebrew (macOS / Linux)

```bash
brew tap dustinkirkland/trustmux
brew install trustmux
trustmux-enable    # configure tailscale serve + start daemon
trustmux-pair      # generate pairing code; enter on phone
```

### pip (PyPI)

```bash
pip install trustmux
trustmux-enable
trustmux-pair
```

### Debian / Ubuntu (.deb)

Trustmux is bundled with byobu — installing byobu brings trustmux along:

```bash
sudo apt install byobu
trustmux-enable
trustmux-pair
```

Or with the PPA for the latest release:

```bash
sudo add-apt-repository ppa:dustinkirkland/byobu
sudo apt install byobu
trustmux-enable
trustmux-pair
```

---

## Daily use

```bash
trustmux-ctl start      # start daemon
trustmux-ctl stop       # stop daemon
trustmux-ctl restart    # restart daemon
trustmux-ctl status     # show URL and running status
trustmux-ctl log        # tail the daemon log

trustmux-pair           # generate a pairing code for a new device
trustmux-unpair         # list paired devices and remove them
```

---

## Setup from source

```bash
cd mobile/
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./trustmux-enable
./trustmux-pair
```

---

## Configuration

| Path | Purpose |
|---|---|
| `~/.config/trustmux/tokens.json` | Paired device session tokens (mode 0600) |
| `~/.config/trustmux/trustmux.sock` | Admin Unix socket (mode 0600) |
| `~/.config/trustmux/trustmux.log` | Daemon log (mode 0600) |
| `~/.config/trustmux/machines.json` | Optional: sibling machines for the machine selector |

### Multiple machines

```json
[
  { "name": "work",     "url": "https://work-machine.tail1234.ts.net" },
  { "name": "personal", "url": "https://personal.tail1234.ts.net" }
]
```

---

## Security

- Daemon binds to `127.0.0.1:7432` only — not reachable from the network
- All traffic encrypted by Tailscale WireGuard; HTTPS via `tailscale serve`
- No relay server — terminal data never leaves your Tailscale mesh
- Pairing codes: 6-digit, 5-minute TTL, single-use, max 10 attempts
- Session tokens: 256-bit random, stored at mode 0600

---

## Tests

```bash
cd mobile/
python3 -m unittest tests.test_daemon -v
```

---

## Troubleshooting

**502 Bad Gateway** — tailscale serve is running but daemon isn't: `trustmux-ctl start`

**"Serve not enabled"** — visit the URL printed by `tailscale serve --bg 7432`

**Phone can't reach URL** — ensure Tailscale is active on the phone

**Need to re-pair** — run `trustmux-pair` and enter the new code on the device
