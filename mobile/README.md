# byobu-mobile

A mobile companion for [Byobu](https://byobu.org) / tmux sessions. Run a lightweight daemon on your workstation; monitor and interact with your terminal sessions from any phone browser over your Tailscale network.

---

## Design philosophy

**byobu-mobile requires Tailscale and uses HTTPS by default.**

This is an opinionated choice. Tailscale provides:
- A WireGuard-encrypted private network — your daemon is never reachable from the public internet
- `tailscale serve` — automatic HTTPS with a valid Let's Encrypt certificate, no cert management required
- Node-level authentication — only devices on your tailnet can reach the daemon at all

Running HTTPS over WireGuard is technically double-encryption. We do it anyway because HTTPS unlocks PWA features (service workers, "Add to Home Screen", push notifications) and avoids an entire class of browser security warnings. A plain-HTTP fallback exists for development, but HTTPS is the supported path.

---

## Requirements

- **Byobu + tmux** installed and working
- **Python 3.10+** (3.12+ recommended)
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager (recommended) or `pip`
- **[Tailscale](https://tailscale.com)** — must be installed, running, and connected to your tailnet

---

## Setup (one-time)

### 1. Install Python dependencies

From the `mobile/` directory:

```bash
uv venv
uv pip install -r requirements.txt
```

Or with pip:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Enable Tailscale Serve on your tailnet

`tailscale serve` provisions a Let's Encrypt cert for your machine's tailnet hostname and proxies HTTPS traffic to the local daemon port. It requires a one-time opt-in from the Tailscale admin console.

```bash
tailscale serve --bg 7432
```

If Serve isn't yet enabled on your tailnet, Tailscale prints an authorization URL:

```
Serve is not enabled on your tailnet.
To enable, visit:
    https://login.tailscale.com/f/serve?node=...
```

Visit that URL, approve it in the Tailscale admin console, then re-run the command. You should see:

```
Available within your tailnet:
https://your-machine.tail1234.ts.net/
|-- proxy http://127.0.0.1:7432
```

This step only needs to be done once per tailnet. The serve configuration persists across reboots.

### 3. Start the daemon

```bash
./byobu-mobile-ctl start
```

Expected output:

```
Configuring tailscale serve: https://your-machine.tail1234.ts.net → localhost:7432
Starting byobu-mobile (HTTPS mode)...
byobu-mobile started (pid 12345)
Connect from phone: https://your-machine.tail1234.ts.net
```

### 4. Pair your phone

On the workstation, generate a one-time pairing code:

```bash
./byobu-mobile-pair
```

```
══════════════════════════════════════════════════
  Byobu Mobile pairing code:  123-456  (valid 5 min)
══════════════════════════════════════════════════
```

On your phone, open `https://your-machine.tail1234.ts.net` in a browser. You'll be prompted for the 6-digit code. Enter it and tap **Pair device**. The session token is stored permanently — you won't need to re-pair unless you explicitly unpair or clear tokens.

> **Note:** Your phone must be connected to the same Tailscale network as your workstation. Install the [Tailscale app](https://tailscale.com/download) on your phone if you haven't already.

---

## Daily use

```bash
./byobu-mobile-ctl start      # start daemon (HTTPS via tailscale serve)
./byobu-mobile-ctl stop       # stop daemon
./byobu-mobile-ctl restart    # restart daemon
./byobu-mobile-ctl status     # show running status and HTTPS URL
./byobu-mobile-ctl log        # tail the daemon log

./byobu-mobile-pair           # generate a new pairing code for a device
./byobu-mobile-unpair         # list paired devices and remove them
```

---

## Automated setup

The `setup` subcommand handles steps 1–2 above in one shot:

```bash
./byobu-mobile-ctl setup
```

This creates the Python virtual environment, installs dependencies, and enables Tailscale Serve. After setup, run `./byobu-mobile-ctl start` to launch the daemon.

---

## Troubleshooting

### 502 Bad Gateway after accepting the certificate
The Tailscale Serve proxy is running but the daemon isn't. Start it:
```bash
./byobu-mobile-ctl start
```

### `Serve is not enabled on your tailnet`
Visit the authorization URL printed by `tailscale serve --bg 7432`. If you've lost it, just re-run that command — it will print the URL again.

### `byobu-mobile-pair` connection refused / reset
Check that the daemon is running: `./byobu-mobile-ctl status`

### Phone can't reach the URL
Make sure Tailscale is active on your phone. The daemon is only reachable within your tailnet — it is not accessible from the public internet.

### Re-pairing after changing HTTPS mode or URL
Changing the URL (e.g., switching from direct HTTP to HTTPS) changes the cookie origin, so existing browser sessions won't carry over. Run `./byobu-mobile-pair` and enter the new code on the new URL. Existing tokens in `~/.config/byobu-mobile/tokens.json` remain valid.

---

## Configuration

| Path | Purpose |
|---|---|
| `~/.config/byobu-mobile/tokens.json` | Paired device session tokens (mode 0600) |
| `~/.config/byobu-mobile/byobu-mobile.sock` | Admin Unix socket — pair/unpair tools only (mode 0600) |
| `~/.config/byobu-mobile/byobu-mobile.log` | Daemon log (mode 0600) |

---

## Security and network exposure

### How exposed is the daemon?

Less than you might think. In the default HTTPS mode:

- `byobu_mobile.py` binds to **`127.0.0.1:7432` only** — loopback, not reachable from the network
- `tailscaled` (the Tailscale daemon) handles all external traffic over WireGuard
- From a firewall or IT perspective: one already-present process (`tailscaled`) has an outbound WireGuard connection; the byobu-mobile daemon is invisible to the network — just another localhost service, no different from a local dev server

No new inbound firewall holes. No new externally-reachable ports. The only meaningful policy question is whether Tailscale itself is permitted on your machine.

### Can I run this on a locked-down work machine?

If Tailscale is allowed (and it often is — many corporate IT teams approve it), the answer is yes. The daemon's network footprint is entirely contained within the already-approved Tailscale connection.

If Tailscale is not available, see [start-local mode](#start-local-loopback-only--ssh-tunnel) below.

### Why Tailscale is the recommended transport

byobu-mobile is opinionated about Tailscale for good reasons:

- **WireGuard encryption** — all traffic is encrypted end-to-end between your devices
- **Node authentication** — only devices on your tailnet can reach the daemon; there is no public attack surface
- **Automatic HTTPS** — `tailscale serve` provisions a valid Let's Encrypt cert with no cert management
- **No port forwarding** — Tailscale's NAT traversal works through firewalls and CGNAT without opening router ports
- **PWA support** — HTTPS is required for service workers, "Add to Home Screen", and push notifications

---

## Alternative access modes

### `start-local` — loopback-only + SSH tunnel

For machines where Tailscale is unavailable. The daemon binds to `127.0.0.1` only and is accessed via SSH local port forwarding from the phone.

```bash
./byobu-mobile-ctl start-local
```

Then from your phone's SSH app (Termius or Blink Shell both support port forwarding):

```bash
ssh -L 7432:localhost:7432 user@workstation
```

Open `http://localhost:7432` in the phone browser while the SSH session is active.

**Security profile:** only SSH port 22 is involved. The daemon never touches the network. IT sees a normal SSH session.

**Limitations:** the web UI is served over HTTP (no HTTPS on loopback), so PWA features won't work. The tunnel drops if the SSH session is interrupted.

### `start-direct` — plain HTTP over Tailscale IP

Binds directly to your Tailscale IP without using `tailscale serve`. WireGuard still encrypts the transport, but there is no TLS cert and PWA features won't work. For development and debugging only.

```bash
./byobu-mobile-ctl start-direct
```

### What about SSH directly to tmux?

SSH + `byobu attach` works and requires no daemon at all:

```bash
ssh user@workstation -t "byobu attach"
```

The limitation is the mobile keyboard. SSH terminal apps (Termius, Blink Shell, JuiceSSH) are functional but none of them solve the fundamental problem of typing shell commands — Ctrl, Esc, function keys, and arrow keys — on a touchscreen without a purpose-built toolbar. This is fine for monitoring; painful for doing real work. byobu-mobile's web UI was built specifically to address this. Native SSH support may be added in a future release.

### Summary

| Mode | Command | Transport | HTTPS | Requires |
|---|---|---|---|---|
| **Default (recommended)** | `start` | Tailscale WireGuard | ✓ via tailscale serve | Tailscale |
| Local + SSH tunnel | `start-local` | SSH port forward | — | SSH app with forwarding |
| Direct HTTP | `start-direct` | Tailscale WireGuard | — | Tailscale |
| Raw terminal | SSH + byobu attach | SSH | — | SSH app |

---

## Development / plain HTTP fallback

To remove the Tailscale Serve configuration entirely:
```bash
tailscale serve reset
```
