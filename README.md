# R1 Hermes Shim

OpenClaw-compatible WebSocket gateway that connects a [Rabbit R1](https://www.rabbit.tech/) to [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a first-class messaging channel.

The R1 talks to this shim exactly like it would talk to OpenClaw. The shim routes messages through the Hermes gateway's shared agent pipeline — same model, same memory, same tools, same sessions as Telegram, Discord, or any other channel.

> ## ⚠️ Now upstream — you probably don't need to patch anything
>
> **As of Hermes `v0.16.0` (tag `v2026.6.5`), the `r1_shim` adapter ships built into Hermes.**
> The enum member, the adapter factory dispatch, the env-driven config, and the auth bypass
> are all already in the gateway source — so on a current Hermes you do **not** copy `r1_shim.py`
> and you do **not** apply the `patches/`. You just set three env vars and restart the gateway.
> See **[Install on Hermes v0.16.0+](#install-on-hermes-v0160-bundled-adapter)** below.
>
> The `gateway/platforms/r1_shim.py` and `patches/` in this repo are kept only for **older
> Hermes builds** that predate the upstream merge. `llms.txt` auto-detects which path applies.

## How It Works

```
┌───────────┐     WebSocket (OpenClaw protocol)     ┌──────────────────┐
│ Rabbit R1 │ ◄──────────────────────────────────► │  r1_shim adapter  │
│  (device) │    QR bootstrap → pair → chat.send    │  (bundled in      │
└───────────┘                                       │   Hermes gateway) │
                                                     └─────────┬─────────┘
                                                               │
                                                      _handle_message()
                                                               │
                                                     ┌─────────▼─────────┐
                                                     │   Hermes Gateway   │
                                                     │   (shared agent)   │
                                                     └────────────────────┘
```

## Install on Hermes v0.16.0+ (bundled adapter)

**1. Pick a port.** The shim defaults to `18789`. If anything else already speaks the OpenClaw
protocol on `18789` (e.g. an Autonomous `intern-server` talking to a local gateway/stub), the
shim's bind will fail with `address already in use` and it gets stuck in a reconnect loop. In
that case give it its own port — `18790` is a good choice. The two OpenClaw dialects are not
interchangeable, so you genuinely run two listeners, not one shared.

**2. Configure.** Append to `~/.hermes/.env` (a **fixed** token keeps the pairing QR stable
across reboots, so you only pair once):

```bash
R1_SHIM_ENABLED=true
R1_SHIM_TOKEN=$(openssl rand -hex 32)
R1_SHIM_PORT=18790   # or 18789 if nothing else is using it
```

**3. Restart the gateway.**

```bash
hermes gateway restart
```

Confirm it bound the port (not crash-looping):

```bash
grep -i r1_shim ~/.hermes/logs/gateway.log | tail -5
# ✓ want: [r1_shim] listening on ws://0.0.0.0:18790/   and   ✓ r1_shim connected
# ✗ bad:  [r1_shim] failed to start: ... address already in use   → change R1_SHIM_PORT
```

**4. Generate the pairing QR.**

```bash
R1_SHIM_TOKEN=<your-token> R1_SHIM_PORT=18790 python3 tools/make_qr.py
# writes r1-pairing-qr.png — scan it in R1 → Settings → OpenClaw
```

That's it. No file copies, no patches.

### Delivering the QR after a reboot

The shim has **no proactive delivery** (`send()` is a no-op), and a fresh Hermes install has no
messaging channel wired up, so the gateway can't DM you the QR. With a fixed `R1_SHIM_TOKEN` the
QR is stable, so the clean pattern is to **regenerate it on each gateway start and serve it
somewhere you already reach.** A oneshot systemd unit does the job:

```ini
# /etc/systemd/system/r1-qr.service
[Unit]
Description=Render Rabbit R1 pairing QR
After=hermes-gateway.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/r1-qr        # your generator: reads ~/.hermes/.env, writes a PNG to a web root
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
```

Regenerating on start also refreshes the LAN IP baked into the QR (it can change across DHCP
leases). For a worked example that renders the QR into a reverse-proxied web root and injects it
as a tile in the Hermes dashboard, see the [Autonomous Intern provisioning kit][intern-kit].

[intern-kit]: https://github.com/iammatthias/intern

## Install on older Hermes (manual patches — legacy)

<details>
<summary>Only needed on Hermes builds older than v0.16.0, which lack the bundled adapter.</summary>

#### 1. Copy the adapter into Hermes

```bash
cp gateway/platforms/r1_shim.py ~/.hermes/hermes-agent/gateway/platforms/
```

#### 2. Apply patches to the Hermes codebase

**`gateway/config.py`** — Add to the `Platform` enum:
```python
R1_SHIM = "r1_shim"
```

And add the env var config block before `return config` in `load_gateway_config()`:
```python
# R1 Shim (Rabbit R1 OpenClaw-compatible gateway)
r1_shim_enabled = os.getenv("R1_SHIM_ENABLED", "").lower() in ("true", "1", "yes")
r1_shim_token = os.getenv("R1_SHIM_TOKEN", "")
r1_shim_port = os.getenv("R1_SHIM_PORT")
if r1_shim_enabled or r1_shim_token:
    if Platform.R1_SHIM not in config.platforms:
        config.platforms[Platform.R1_SHIM] = PlatformConfig()
    config.platforms[Platform.R1_SHIM].enabled = True
    if r1_shim_token:
        config.platforms[Platform.R1_SHIM].extra["token"] = r1_shim_token
    if r1_shim_port:
        try:
            config.platforms[Platform.R1_SHIM].extra["port"] = int(r1_shim_port)
        except ValueError:
            pass
```

**`gateway/run.py`** — Add to `_create_adapter()` before `return None`:
```python
elif platform.value == "r1_shim":
    from gateway.platforms.r1_shim import R1ShimAdapter, check_r1_shim_requirements
    if not check_r1_shim_requirements():
        logger.warning("R1 Shim: aiohttp not installed")
        return None
    return R1ShimAdapter(config)
```

And add `Platform.R1_SHIM` to the auth bypass in `_is_user_authorized()`:
```python
if source.platform in (Platform.HOMEASSISTANT, Platform.WEBHOOK, Platform.R1_SHIM):
    return True
```

**`hermes_cli/tools_config.py`** — Add to the `PLATFORMS` dict:
```python
"r1_shim": {"label": "🐰 Rabbit R1", "default_toolset": "hermes-telegram"},
```

#### 3. Clear bytecache, then configure + restart as in the v0.16.0+ steps above

```bash
find ~/.hermes/hermes-agent -name '__pycache__' -exec rm -rf {} + 2>/dev/null
```

</details>

## Let an agent do it

This repo ships an [`llms.txt`](llms.txt) — a step-by-step guide written for AI agents (Hermes,
Claude Code, Codex, …). Hand it the raw URL and ask it to install the shim; it detects your
Hermes version and takes the bundled or the legacy path accordingly.

```
https://raw.githubusercontent.com/iammatthias/r1-hermes-shim/main/llms.txt
```

## Files

```
├── llms.txt                            # Agent-readable installation guide (version-aware)
├── README.md                           # This file
├── gateway/
│   └── platforms/
│       └── r1_shim.py                  # The adapter — now bundled in Hermes v0.16.0+;
│                                       #   here for reference / older builds
├── patches/                            # LEGACY: source patches for pre-v0.16.0 Hermes only
│   ├── config.py.patch                 #   Platform enum + env var config
│   ├── run.py.patch                    #   Adapter dispatch + auth bypass
│   └── tools_config.py.patch           #   Toolset registration
├── tools/
│   ├── make_qr.py                      # QR code generator for R1 pairing
│   └── make_payload.py                 # Raw JSON payload generator
└── LICENSE
```

## Protocol Summary

The R1 speaks a subset of the OpenClaw gateway WebSocket protocol:

| Phase | Direction | Frame |
|-------|-----------|-------|
| Challenge | Server → Client | `{type: "event", event: "connect.challenge", payload: {nonce, ts}}` |
| Connect | Client → Server | `{type: "req", method: "connect", params: {auth: {token}, device: {id, publicKey, ...}, role, scopes, client: {displayName, platform, deviceFamily}}}` |
| Hello | Server → Client | `{type: "res", ok: true, payload: {type: "hello-ok", protocol: 3, auth: {deviceToken, role, scopes}}}` |
| Send | Client → Server | `{type: "req", method: "chat.send", params: {sessionKey, message, idempotencyKey}}` |
| Ack | Server → Client | `{type: "res", ok: true, payload: {runId, status: "started"}}` |
| Reply | Server → Client | `{type: "event", event: "chat", payload: {runId, sessionKey, seq, state: "delta"/"final", message: {role: "assistant", content: [{type: "text", text}]}}}` |

The pairing QR payload (what `tools/make_qr.py` encodes) is:

```json
{"type":"clawdbot-gateway","version":1,"ips":["<lan-ip>"],"port":18790,"token":"<token>","protocol":"ws"}
```

## Known Limitations

- **No streaming** — Responses sent as single delta+final pair, not incremental tokens
- **No voice** — R1 mic/speaker not integrated yet
- **No camera** — R1 camera not integrated yet
- **No proactive delivery** — `send()` is a no-op; R1 can't receive cron/cross-platform messages yet (see [Delivering the QR after a reboot](#delivering-the-qr-after-a-reboot) for the pairing-QR workaround)
- **Auto-approve only** — All devices are auto-approved on pairing

## License

MIT
