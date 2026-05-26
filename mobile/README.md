# byobu-mobile

A mobile companion for [Byobu](https://byobu.org) / tmux sessions. Run a lightweight daemon on your workstation; monitor and interact with your terminal sessions from your phone over your Tailscale network.

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

## Install (from .deb)

```bash
sudo dpkg -i byobu-mobile_7.0_all.deb
byobu-mobile-enable    # configure tailscale serve + start daemon
byobu-mobile-pair      # generate pairing code; enter on phone
```

---

## Daily use

```bash
byobu-mobile-ctl start      # start daemon
byobu-mobile-ctl stop       # stop daemon
byobu-mobile-ctl restart    # restart daemon
byobu-mobile-ctl status     # show URL and running status
byobu-mobile-ctl log        # tail the daemon log

byobu-mobile-pair           # generate a pairing code for a new device
byobu-mobile-unpair         # list paired devices and remove them
```

---

## Setup from source

```bash
cd mobile/
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./byobu-mobile-enable
./byobu-mobile-pair
```

---

## Configuration

| Path | Purpose |
|---|---|
| `~/.config/byobu-mobile/tokens.json` | Paired device session tokens (mode 0600) |
| `~/.config/byobu-mobile/byobu-mobile.sock` | Admin Unix socket (mode 0600) |
| `~/.config/byobu-mobile/byobu-mobile.log` | Daemon log (mode 0600) |
| `~/.config/byobu-mobile/machines.json` | Optional: sibling machines for the machine selector |

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

**502 Bad Gateway** — tailscale serve is running but daemon isn't: `byobu-mobile-ctl start`

**"Serve not enabled"** — visit the URL printed by `tailscale serve --bg 7432`

**Phone can't reach URL** — ensure Tailscale is active on the phone

**Need to re-pair** — run `byobu-mobile-pair` and enter the new code on the device
