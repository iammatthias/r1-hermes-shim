# R1 Hermes Shim

A third-party OpenClaw-compatible WebSocket gateway that connects a [Rabbit R1](https://www.rabbit.tech/) to [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a first-class messaging channel.

The R1 talks to the gateway exactly like it would talk to OpenClaw, and messages route through the Hermes agent pipeline with the same model, memory, tools, and sessions as Telegram or any other channel.

This ships as a **Hermes platform plugin**. It installs into `~/.hermes/plugins/` and registers itself through Hermes' plugin system, so it needs **zero edits to the Hermes source tree** and survives `hermes update`. (Earlier versions patched the Hermes source directly; that broke on every upgrade. See [Migrating from the patch-based install](#migrating-from-the-patch-based-install).)

## How It Works

```
┌───────────┐     WebSocket (OpenClaw protocol)     ┌───────────────────────┐
│ Rabbit R1 │ ◄──────────────────────────────────► │  r1_shim plugin        │
│  (device) │    QR bootstrap → pair → chat.send    │  (~/.hermes/plugins/)  │
└───────────┘                                       └───────────┬───────────┘
                                                                │
                                                       _handle_message()
                                                                │
                                                      ┌─────────▼─────────┐
                                                      │   Hermes Gateway   │
                                                      │   (shared agent)   │
                                                      └────────────────────┘
```

The plugin registers a `r1_shim` platform via `ctx.register_platform()`. Hermes mints the
`Platform("r1_shim")` enum member on demand (its `_missing_()` hook), reads `allow_all_env`
straight off the registry entry for authorization, and auto-enables the channel when
`R1_SHIM_TOKEN` is set. No core code is touched.

## Install

### 1. Install the plugin

```bash
hermes plugins install iammatthias/r1-hermes-shim
hermes plugins enable r1-shim
```

`install` clones this repo into `~/.hermes/plugins/r1-hermes-shim/`. Platform plugins are
gated by an allow-list (untrusted code), so `enable` opts it in. Later, `hermes plugins update`
pulls new versions.

### 2. Configure `~/.hermes/.env`

A **fixed** token keeps the pairing QR stable across reboots, so you pair once. Its presence
auto-enables the channel.

```bash
cat >> ~/.hermes/.env <<EOF
# Rabbit R1 OpenClaw-compatible shim
R1_SHIM_TOKEN=$(openssl rand -hex 32)
R1_SHIM_PORT=18790
R1_SHIM_ALLOW_ALL_USERS=true
EOF
```

Pick the port carefully. The plugin defaults to `18790`. If another OpenClaw listener already
owns a port on the host (most commonly an Autonomous `intern-server` talking to a local
gateway/stub on `18789`), do not share it. The two OpenClaw dialects are not interchangeable
(the intern stub answers `{id,result}` frames; the R1 shim answers `{type:"res",ok,payload}`
challenge/hello-ok frames), so run the R1 shim on its own port:

```bash
ss -ltnp | grep ':18789' && echo "18789 busy -> keep R1_SHIM_PORT=18790"
```

### 3. Restart the gateway and verify

```bash
hermes gateway restart   # or: systemctl restart hermes-gateway
journalctl -u hermes-gateway | grep -i r1_shim | tail -5
# want: [r1_shim] listening on ws://0.0.0.0:18790/
# bad:  failed to start: ... address already in use   → change R1_SHIM_PORT
ss -ltnp | grep ':18790' && echo "r1_shim listening"
```

### 4. Generate the pairing QR

```bash
R1_SHIM_TOKEN=<your-token> R1_SHIM_PORT=18790 python3 scripts/make_qr.py
# writes r1-pairing-qr.png — scan it in R1 → Settings → OpenClaw
```

## Configuration

All knobs are env vars in `~/.hermes/.env` (or `platforms.r1_shim.extra` in `config.yaml`):

| Env var | Default | Purpose |
|---------|---------|---------|
| `R1_SHIM_TOKEN` | (auto, unstable) | Gateway pairing token. Set it; its presence auto-enables the channel and a fixed value keeps the QR stable. |
| `R1_SHIM_PORT` | `18790` | WebSocket listen port. |
| `R1_SHIM_ALLOW_ALL_USERS` | unset | Authorize all R1 devices. Expected: the R1 authenticates at the WS layer with the gateway token. |
| `R1_SHIM_AUTO_APPROVE` | `true` | Auto-approve new devices. `false` holds them pending operator approval. |
| `R1_SHIM_TTS_VOICE` | `en-US-AriaNeural` | edge-tts voice for talk-back. |
| `R1_SHIM_TTS_FORMAT` | `mp3` | `mp3` or `pcm` (raw pcm_24000 via ffmpeg). |
| `R1_SHIM_UNIFIED_CHAT_ID` / `R1_SHIM_UNIFIED_PLATFORM` | unset | Share one session with another channel (e.g. the primary Telegram DM). |

## Migrating from the patch-based install

Versions before 2.0 copied `r1_shim.py` into `gateway/platforms/` and patched `config.py`,
`run.py`, and `authz_mixin.py`. Those are working-tree edits to the Hermes git checkout, so
`hermes update` stashed/discarded them and the channel broke on every upgrade. To migrate:

```bash
# 1. revert the patched source files and drop the copied adapter
HERMES_SRC=/usr/local/lib/hermes-agent   # or ~/.hermes/hermes-agent
git -C "$HERMES_SRC" checkout -- gateway/config.py gateway/run.py gateway/authz_mixin.py
rm -f "$HERMES_SRC/gateway/platforms/r1_shim.py"
# 2. install as a plugin (steps above)
hermes plugins install iammatthias/r1-hermes-shim && hermes plugins enable r1-shim
# 3. `hermes update` now runs clean
```

State in `~/.hermes/r1_shim/` (paired devices, queue, event log) is untouched by the migration,
so paired R1s stay paired.

## Delivering the QR after a reboot

With a fixed `R1_SHIM_TOKEN` the QR is stable, so the clean pattern is to regenerate it on each
gateway start and serve it somewhere you already reach. Regenerating on start also refreshes the
LAN IP baked into the QR (it can change across DHCP leases). For a worked example that renders
the QR into a reverse-proxied web root and shows it as a tile in the Hermes dashboard, see the
[Autonomous Intern provisioning kit][intern-kit].

[intern-kit]: https://github.com/iammatthias/intern

## Files

```
├── plugin.yaml                          # Plugin manifest (kind: platform)
├── __init__.py                          # Plugin entry point (exports register)
├── adapter.py                           # The R1ShimAdapter + register(ctx)
├── scripts/
│   ├── make_qr.py                       # QR code generator for R1 pairing
│   └── make_payload.py                  # Raw JSON payload generator
├── README.md
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

The pairing QR payload (what `scripts/make_qr.py` encodes) is:

```json
{"type":"clawdbot-gateway","version":1,"ips":["<lan-ip>"],"port":18790,"token":"<token>","protocol":"ws"}
```

## Capabilities & limitations

- **Survives `hermes update`** ✅ — the plugin lives in `~/.hermes/plugins/`, outside the Hermes
  git checkout, so native updates no longer wipe it.
- **Proactive delivery** ✅ — `send()` pushes a message to a connected R1 (cron jobs, cross-platform
  routing via the gateway's `DeliveryRouter`). Messages for an offline R1 are queued and flushed on
  its next connect (capped per device).
- **Device approval** ✅ — `auto_approve: true` (default) keeps pair-on-connect. Set
  `R1_SHIM_AUTO_APPROVE=false` to hold new devices as `pending`; approve at
  `http://<host>:<port>/approve?token=<gateway-token>&deviceId=<id>` (the admin page at
  `/?token=<gateway-token>` lists pending devices).
- **Voice — inbound** ✅ — the R1 transcribes speech on-device and sends it as `chat.send` text, so
  talking to it already works.
- **Voice — talk-back (TTS)** ⚠️ **implemented, but not reachable on current R1 firmware.** The
  shim answers the `talk.speak` RPC with synthesized audio (edge-tts, free, no key). Any OpenClaw
  _node_ client that calls `talk.speak` will speak, but a Rabbit R1 connects only as a single
  `role:"operator"` session and never opens the `role:"node"`, `talk`-capable session OpenClaw's
  spoken-reply path requires. Confirmed from the
  [openclaw/openclaw](https://github.com/openclaw/openclaw) source: TTS is produced device-side by
  the node client, and there is no `hello-ok` field, `talk.config` value, or chat-reply flag the
  gateway can use to enable it. It is an R1-firmware limitation, not a shim gap. (Inbound voice is
  unaffected.)
- **Camera** ✅ — photos arrive inside `chat.send` as base64 `attachments` and are decoded to files
  Hermes vision reads (routed as `MessageEvent.media_urls`).
- **No streaming** — replies are a single delta+final pair, not incremental tokens (true streaming
  would couple to Hermes' per-platform streaming internals).

## License

MIT
