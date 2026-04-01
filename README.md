# R1 Hermes Shim

OpenClaw-compatible WebSocket gateway that connects a [Rabbit R1](https://www.rabbit.tech/) to [Hermes Agent](https://github.com/hermes-ai/hermes-agent) as a first-class messaging channel.

The R1 talks to this shim exactly like it would talk to OpenClaw. The shim routes messages through the Hermes gateway's shared agent pipeline — same model, same memory, same tools, same sessions as Telegram, Discord, or any other channel.

## How It Works

```
┌──────────┐     WebSocket (OpenClaw protocol)     ┌─────────────────┐
│ Rabbit R1 │ ◄──────────────────────────────────► │  r1_shim adapter │
│  (device) │    QR bootstrap → pair → chat.send   │  (port 18789)    │
└──────────┘                                        └────────┬────────┘
                                                             │
                                                    _handle_message()
                                                             │
                                                    ┌────────▼────────┐
                                                    │  Hermes Gateway  │
                                                    │  (shared agent)  │
                                                    └──────────────────┘
```

## Installation

### Recommended: Let your agent install it

This repo includes an [`llms.txt`](llms.txt) file — a step-by-step installation guide written for AI agents. The easiest way to set up the shim is to give this file to your Hermes agent (or any coding agent like Claude Code, Codex, etc.) and let it handle the installation.

**From the raw URL:**
```
https://raw.githubusercontent.com/iammatthias/r1-hermes-shim/main/llms.txt
```

**From a local clone:**
```
Give your agent the contents of llms.txt in this repo
```

The `llms.txt` covers everything: downloading the adapter, patching the three Hermes source files, configuring environment variables, restarting the gateway, and generating the pairing QR code. It also includes a troubleshooting table for common issues.

### Manual installation

If you prefer to install by hand, the steps are below. These are the same steps described in `llms.txt`.

<details>
<summary>Expand manual installation steps</summary>

#### 1. Copy the adapter into Hermes

```bash
cp gateway/platforms/r1_shim.py ~/.hermes/hermes-agent/gateway/platforms/
```

#### 2. Apply patches to the Hermes codebase

Three small changes are needed:

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

#### 3. Configure

Add to `~/.hermes/.env`:

```bash
R1_SHIM_ENABLED=true
R1_SHIM_TOKEN=$(openssl rand -hex 32)
R1_SHIM_PORT=18789
```

#### 4. Restart the gateway

```bash
hermes gateway restart
```

You should see:
```
✓ telegram connected
✓ r1_shim connected
Gateway running with 2 platform(s)
```

#### 5. Generate the pairing QR

```bash
python3 tools/make_qr.py
```

This creates a QR code PNG that the R1 can scan to connect. Use the R1's OpenClaw pairing flow to scan it.

</details>

## Files

```
├── llms.txt                            # Agent-readable installation guide (start here)
├── README.md                           # This file
├── gateway/
│   └── platforms/
│       └── r1_shim.py                  # The platform adapter (drop into Hermes)
├── patches/
│   ├── config.py.patch                 # Platform enum + env var config
│   ├── run.py.patch                    # Adapter dispatch + auth bypass
│   └── tools_config.py.patch           # Toolset registration
├── tools/
│   ├── make_qr.py                      # QR code generator for R1 pairing
│   └── make_payload.py                 # Raw JSON payload generator
├── blog/
│   └── rabbit-r1-hermes-shim.md        # Full write-up
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

## Known Limitations

- **No streaming** — Responses sent as single delta+final pair, not incremental tokens
- **No voice** — R1 mic/speaker not integrated yet
- **No camera** — R1 camera not integrated yet
- **No proactive delivery** — `send()` is a no-op; R1 can't receive cron/cross-platform messages yet
- **Auto-approve only** — All devices are auto-approved on pairing

## License

MIT
