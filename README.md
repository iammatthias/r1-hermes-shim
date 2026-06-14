# R1 Hermes Shim

A third-party OpenClaw-compatible WebSocket gateway that connects a [Rabbit R1](https://www.rabbit.tech/) to [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a first-class messaging channel.

The R1 talks to the patched gateway exactly like it would talk to OpenClaw, and messages route through the Hermes agent pipeline — same model, same memory, same tools, same sessions as Telegram or any other channel. Tested against Hermes `v0.16.0` (tag `v2026.6.5`).

## How It Works

```
┌───────────┐     WebSocket (OpenClaw protocol)     ┌──────────────────┐
│ Rabbit R1 │ ◄──────────────────────────────────► │  r1_shim adapter  │
│  (device) │    QR bootstrap → pair → chat.send    │  (patched into    │
└───────────┘                                       │   your Hermes)    │
                                                     └─────────┬─────────┘
                                                               │
                                                      _handle_message()
                                                               │
                                                     ┌─────────▼─────────┐
                                                     │   Hermes Gateway   │
                                                     │   (shared agent)   │
                                                     └────────────────────┘
```

## Heads-up: the patches modify your Hermes source tree

The install copies `r1_shim.py` into Hermes and edits two of its source files. Hermes is
installed as a **git checkout** of `NousResearch/hermes-agent` (e.g. `/usr/local/lib/hermes-agent`
or `~/.hermes/hermes-agent`), so these edits show up as **uncommitted working-tree changes**.
That means **`hermes update` / a reinstall will discard them** — you must re-apply the shim after
upgrading Hermes. If you provision via a script, make the patch step idempotent and re-run it
after every Hermes (re)install.

## Install

### 1. Find your Hermes source

```bash
for d in /usr/local/lib/hermes-agent "$HOME/.hermes/hermes-agent"; do
  [ -f "$d/gateway/config.py" ] && echo "HERMES_SRC=$d" && break
done
```

### 2. Copy the adapter

```bash
cp gateway/platforms/r1_shim.py "$HERMES_SRC/gateway/platforms/"
```

### 3–4. Apply the source patch

If your Hermes is exactly tag `v2026.6.5`, apply both source edits in one shot (idempotent):

```bash
P=/tmp/r1-shim.patch
curl -sL https://raw.githubusercontent.com/iammatthias/r1-hermes-shim/main/patches/hermes-v2026.6.5.patch -o "$P"
git -C "$HERMES_SRC" apply --reverse --check "$P" 2>/dev/null && echo "already applied" || git -C "$HERMES_SRC" apply "$P"
```

If it doesn't apply cleanly your Hermes is a different version — do steps 3 and 4 by hand instead.

### 3. Patch `gateway/config.py` (manual)

Add `R1_SHIM = "r1_shim"` to the `Platform` enum (e.g. after `WECOM = "wecom"`), and add this
block before `return config` in `load_gateway_config()`:

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

### 4. Patch `gateway/run.py`

In `_create_adapter`, before the final `return None`:

```python
        elif platform == Platform.R1_SHIM:
            from gateway.platforms.r1_shim import R1ShimAdapter, check_r1_shim_requirements
            if not check_r1_shim_requirements():
                logger.warning("R1 Shim: aiohttp not installed")
                return None
            return R1ShimAdapter(config)
```

And add `Platform.R1_SHIM` to the auth bypass in `_is_user_authorized`:

```python
if source.platform in (Platform.HOMEASSISTANT, Platform.WEBHOOK, Platform.R1_SHIM):
```

### 5. (Optional) Patch `hermes_cli/tools_config.py`

Cosmetic — sets the channel's label/default toolset. The shim works without it. Add to `PLATFORMS`:

```python
    "r1_shim": {"label": "🐰 Rabbit R1", "default_toolset": "hermes-telegram"},
```

### 6. Clear bytecache

```bash
find "$HERMES_SRC" -name '__pycache__' -exec rm -rf {} + 2>/dev/null
```

### 7. Pick a port, then configure `~/.hermes/.env`

The shim defaults to `18789`. If another OpenClaw listener already owns `18789` on the host —
most commonly an Autonomous `intern-server` talking to a local gateway/stub — the shim can't bind
and crash-loops with `[Errno 98] address already in use`. The two OpenClaw dialects are not
interchangeable (the intern stub answers `{id,result}` frames; the R1 shim answers
`{type:"res",ok,payload}` challenge/hello-ok frames), so you run a second listener on its own
port rather than sharing. Use `18790` when `18789` is taken:

```bash
ss -ltnp | grep ':18789' && echo "18789 busy -> use R1_SHIM_PORT=18790"
```

Append to `~/.hermes/.env` (a **fixed** token keeps the pairing QR stable across reboots — pair once):

```bash
cat >> ~/.hermes/.env <<EOF
# Rabbit R1 OpenClaw-compatible shim
R1_SHIM_ENABLED=true
R1_SHIM_TOKEN=$(openssl rand -hex 32)
R1_SHIM_PORT=18790   # or 18789 if nothing else uses it
EOF
```

### 8. Restart the gateway and verify

```bash
hermes gateway restart   # or: systemctl restart hermes-gateway
grep -i r1_shim ~/.hermes/logs/gateway.log | tail -5
# want: [r1_shim] listening on ws://0.0.0.0:18790/   and   ✓ r1_shim connected
# bad:  failed to start: ... address already in use   → change R1_SHIM_PORT
```

### 9. Generate the pairing QR

```bash
R1_SHIM_TOKEN=<your-token> R1_SHIM_PORT=18790 python3 tools/make_qr.py
# writes r1-pairing-qr.png — scan it in R1 → Settings → OpenClaw
```

## Delivering the QR after a reboot

The shim has **no proactive delivery** (`send()` is a no-op), and a fresh Hermes has no messaging
channel wired up, so the gateway can't DM you the QR. With a fixed `R1_SHIM_TOKEN` the QR is
stable, so the clean pattern is to **regenerate it on each gateway start and serve it somewhere
you already reach.** A oneshot systemd unit does the job:

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
leases). For a worked example that renders the QR into a reverse-proxied web root and shows it as
a tile in the Hermes dashboard, see the [Autonomous Intern provisioning kit][intern-kit].

[intern-kit]: https://github.com/iammatthias/intern

## Let an agent do it

This repo ships an [`llms.txt`](llms.txt) — the same steps written for an AI agent (Hermes,
Claude Code, Codex, …). Hand it the raw URL and ask it to install the shim.

```
https://raw.githubusercontent.com/iammatthias/r1-hermes-shim/main/llms.txt
```

## Files

```
├── llms.txt                            # Agent-readable installation guide
├── README.md                           # This file
├── gateway/
│   └── platforms/
│       └── r1_shim.py                  # The adapter (copied into your Hermes)
├── patches/
│   ├── config.py.patch                 # Platform enum + env var config
│   ├── run.py.patch                    # Adapter dispatch + auth bypass
│   └── tools_config.py.patch           # Toolset registration (optional/cosmetic)
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

- **Patches get wiped by `hermes update`** — re-apply the shim after upgrading Hermes (see above)
- **No streaming** — Responses sent as single delta+final pair, not incremental tokens
- **No voice** — R1 mic/speaker not integrated yet
- **No camera** — R1 camera not integrated yet
- **No proactive delivery** — `send()` is a no-op; R1 can't receive cron/cross-platform messages yet (see [Delivering the QR after a reboot](#delivering-the-qr-after-a-reboot) for the pairing-QR workaround)
- **Auto-approve only** — All devices are auto-approved on pairing

## License

MIT
