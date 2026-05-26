# byobu-mobile

A mobile companion for [Byobu](https://byobu.org) / tmux sessions. Run a lightweight daemon on your workstation; monitor and interact with your terminal sessions from your phone over your Tailscale network.

Two tiers:
- **Free** — PWA (Progressive Web App). One installable icon per machine, with the machine hostname in the app name. Full session/window/pane control included now; window/pane switching will move to the paid tier in a future release.
- **Paid** — Native Flutter app (`github.com/dustinkirkland/byobu-mobile`, private). Multi-machine management, session/window/pane switching, biometric auth.

---

## Architecture

```
Phone (browser / Flutter app)
  │  HTTPS / WSS over Tailscale WireGuard
  ▼
tailscale serve  (Let's Encrypt cert, reverse proxy)
  │  HTTP / WS on 127.0.0.1:7432
  ▼
byobu_mobile.py  (Tornado daemon)
  │  subprocess
  ▼
tmux CLI  (capture-pane, send-keys, list-sessions…)
```

- **Transport:** Tailscale WireGuard (encrypted) + `tailscale serve` (HTTPS/TLS)
- **Auth:** one-time 6-digit pairing code → permanent session token stored in `~/.config/byobu-mobile/tokens.json` (mode 0600); token sent as `byobu_mobile_session` cookie (browser) or `?token=` query param (native app)
- **Terminal output:** `tmux capture-pane -p [-e]`; `-e` flag used when client sends `"ansi": true` in subscribe message (native app with xterm renderer)
- **Admin channel:** Unix socket `~/.config/byobu-mobile/byobu-mobile.sock` (mode 0600); pair/unpair tools talk to the daemon here — no TCP exposure

---

## Packaging

byobu-mobile ships as a separate Debian binary package alongside `byobu`:

```
byobu_7.0_all.deb          — core byobu package
byobu-mobile_7.0_all.deb   — mobile daemon + web UI
```

Built from the `byobu` source tree (`mobile/` directory) via `debian/` packaging rules. The daemon installs to `/usr/bin/byobu-mobile-*` and the static web assets to `/usr/share/byobu-mobile/static/`.

---

## Install (from .deb)

```bash
sudo dpkg -i byobu-mobile_7.0_all.deb
byobu-mobile-enable    # configure tailscale serve + start daemon
byobu-mobile-pair      # generate pairing code; enter on phone
```

### Enable / disable

```bash
byobu-mobile-enable    # set up tailscale serve, start daemon on login
byobu-mobile-disable   # stop daemon, remove tailscale serve config
```

`byobu-mobile-enable` auto-runs `sudo tailscale set --operator=$USER` if needed, then `tailscale serve --bg 7432`.

### Daily control

```bash
byobu-mobile-ctl start      # start daemon
byobu-mobile-ctl stop       # stop daemon
byobu-mobile-ctl restart    # restart daemon
byobu-mobile-ctl status     # show running status and URL
byobu-mobile-ctl log        # tail daemon log

byobu-mobile-pair           # generate a new pairing code
byobu-mobile-unpair         # list paired devices and remove them
```

---

## Setup (from source / dev tree)

```bash
cd mobile/
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./byobu-mobile-enable
./byobu-mobile-pair
```

The `byobu-mobile-ctl` script auto-detects whether it's running from a dev tree or an installed package and sets paths accordingly.

---

## PWA features

- **Per-machine install:** `manifest.json` is generated dynamically with `socket.gethostname()` in `name`/`short_name`, so each machine installs as a distinct PWA icon (e.g. "byobu · claude").
- **Fixed layout:** header (logo, hostname, clock, S/W/P pickers) and bottom bar (byobu status chips + input) are fixed; only the terminal output area scrolls. Uses `position:fixed; inset:0` on the app container and `overscroll-behavior:contain` on the output div.
- **Byobu status line:** reads `~/.config/byobu/status` for `tmux_left`/`tmux_right` config; reads chip data from `/dev/shm/byobu-{user}-*/status.tmux/`; renders colored chips matching the terminal statusline appearance.
- **Password masking:** `input_mode` WebSocket message with `echo: false` masks the input field.
- **Nav pickers:** abbreviated `S:` / `W:` / `P:` prefixes to save space; placeholders remain "Session…" / "Window…" / "Pane…".

---

## Native app (Flutter) — paid tier

Repo: `github.com/dustinkirkland/byobu-mobile` (private, proprietary license)

- xterm Flutter package for full VT100/ANSI rendering
- Token passed as `?token=` query param on WebSocket (Android Cookie headers unreliable)
- Daemon sends `tmux capture-pane -e` when client requests `"ansi": true` in subscribe message
- FlutterSecureStorage (Android encryptedSharedPreferences) for session tokens
- Session/window/pane picker as a bottom sheet (paid feature)
- Sticky Ctrl key + long-press Ctrl menu for common shortcuts

### Monetization boundary

| Feature | Free PWA | Paid Flutter |
|---|---|---|
| View terminal output | ✓ | ✓ |
| Send keys / commands | ✓ | ✓ |
| Password input masking | ✓ | ✓ |
| Per-machine PWA install | ✓ | — |
| Session/window/pane switching | ✓ now → paid later | ✓ |
| ANSI colors in terminal | — | ✓ |
| Biometric auth | — | ✓ |
| Multi-machine in one app | — | ✓ |

---

## Security

- Daemon binds to `127.0.0.1:7432` only; all external traffic goes through `tailscaled` over WireGuard
- No new inbound firewall holes; Tailscale is the only externally-reachable surface
- Pairing codes: 6-digit, 5-minute TTL, max 10 attempts, single-use (invalidated on first success)
- Session tokens: `secrets.token_urlsafe(32)`, stored at mode 0600, validated on every WebSocket message
- WebSocket: `del raw` after JSON parse, `del keys` after `tmux send-keys` (sensitive content released early)
- Rate limiting: 20 messages/second per WebSocket connection
- Admin socket: mode 0600 Unix socket; pair/unpair never touch TCP
- CSP header: `default-src 'self'`; no CDN dependencies; all assets served from daemon

---

## Configuration files

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

The machine the browser is currently connected to is always the selected option. The selector is hidden when only one machine is configured.

---

## Access modes

| Mode | Command | Transport | HTTPS | PWA |
|---|---|---|---|---|
| **Default (recommended)** | `start` | Tailscale WireGuard | ✓ tailscale serve | ✓ |
| Local + SSH tunnel | `start-local` | SSH port forward | — | — |
| Direct HTTP | `start-direct` | Tailscale WireGuard | — | — |

---

## Tests

```bash
cd mobile/
python3 -m unittest tests.test_daemon -v
```

50 tests covering: ANSI stripping, tmux ID validation, tmux output parsing (panes/windows), byobu status config parsing, pair code generation, HTTP handlers (ping, pair, manifest, status), and `tmux capture-pane` ANSI flag behavior. Uses stdlib `unittest` + `tornado.testing` — no extra dependencies.

---

## Troubleshooting

**502 Bad Gateway** — tailscale serve is running but daemon isn't: `byobu-mobile-ctl start`

**Serve not enabled** — visit the URL printed by `tailscale serve --bg 7432`

**Phone can't reach URL** — ensure Tailscale is active on the phone

**Re-pairing after URL change** — changing the URL changes the cookie origin; run `byobu-mobile-pair` again on the new URL
