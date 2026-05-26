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

## Development / plain HTTP fallback

If Tailscale Serve isn't available (older Tailscale version, air-gapped machine), you can run the daemon bound directly to your Tailscale IP over plain HTTP. WireGuard still encrypts the transport, but PWA features won't work.

```bash
./byobu-mobile-ctl start-direct
```

To remove the Tailscale Serve configuration entirely:
```bash
tailscale serve reset
```
