"""
Rabbit R1 OpenClaw-compatible shim platform adapter.

Runs a WebSocket server that speaks enough of the OpenClaw gateway protocol
for a Rabbit R1 to connect, pair, and send messages. Messages are routed
through the standard Hermes gateway message pipeline (_handle_message),
so the R1 gets the same agent, same sessions, same tools as Telegram.

Configuration in config.yaml:
  platforms:
    r1_shim:
      enabled: true
      extra:
        port: 18789
        token: "<your-token>"  # or set R1_SHIM_TOKEN env var
        auto_approve: true       # false => new devices wait for operator approval

Proactive delivery: send() pushes a message to a connected R1 (used by cron and
cross-platform routing via the gateway's DeliveryRouter). Messages for an offline
R1 are queued and flushed on its next connect.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from aiohttp import web, WSMsgType
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None
    WSMsgType = None

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    EDGE_TTS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18789
STATE_DIR_NAME = "r1_shim"
# Cap the offline queue per device so a long-offline R1 can't grow it without bound.
MAX_PENDING_PER_DEVICE = 50


def check_r1_shim_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def _now_ms() -> int:
    return int(time.time() * 1000)


class R1ShimAdapter(BasePlatformAdapter):
    """OpenClaw-compatible WebSocket gateway for Rabbit R1."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.R1_SHIM)
        extra = config.extra or {}
        self._port = int(extra.get("port", os.getenv("R1_SHIM_PORT", str(DEFAULT_PORT))))
        self._token = extra.get("token", os.getenv("R1_SHIM_TOKEN", secrets.token_hex(32)))
        self._auto_approve = str(extra.get("auto_approve", os.getenv("R1_SHIM_AUTO_APPROVE", "true"))).lower() not in ("0", "false", "no")
        # Talk-back (TTS): the R1 calls the talk.speak RPC when its on-device "speak replies"
        # toggle is on; we synthesize and return base64 audio. Voice = an edge-tts voice name
        # (free, no key). Format: "mp3" (MediaPlayer, robust) or "pcm" (raw pcm_24000 ->
        # AudioTrack, lower latency; transcoded via ffmpeg).
        self._tts_voice = extra.get("tts_voice", os.getenv("R1_SHIM_TTS_VOICE", "en-US-AriaNeural"))
        self._tts_format = str(extra.get("tts_format", os.getenv("R1_SHIM_TTS_FORMAT", "mp3"))).lower()
        self._runner = None
        self._site = None
        self._app = None
        self._paired: Dict[str, Dict[str, Any]] = {}
        self._device_tokens: set = set()
        # conn_id -> ws (every live socket) and device_id -> ws (newest socket per device,
        # for proactive send()). Both cleared as sockets close.
        self._active_ws: Dict[str, web.WebSocketResponse] = {}
        self._device_ws: Dict[str, web.WebSocketResponse] = {}
        self._pending_responses: Dict[str, asyncio.Future] = {}
        # device_id -> [text, ...] queued while the R1 was offline; flushed on connect.
        self._pending: Dict[str, List[str]] = {}

        # State persistence
        from hermes_constants import get_hermes_home
        self._state_dir = get_hermes_home() / STATE_DIR_NAME
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self._load_pending()

    def _load_state(self):
        state_file = self._state_dir / "paired.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                self._paired = data.get("paired", {})
                # Only approved devices contribute a valid reconnect token. Devices
                # predating the approval flow have no "status" — treat as approved.
                self._device_tokens = set(
                    p.get("deviceToken")
                    for p in self._paired.values()
                    if p.get("deviceToken") and self._is_approved(p)
                )
            except Exception:
                pass

    def _save_state(self):
        state_file = self._state_dir / "paired.json"
        state_file.write_text(json.dumps({"paired": self._paired}, indent=2))

    def _load_pending(self):
        pending_file = self._state_dir / "pending.json"
        if pending_file.exists():
            try:
                self._pending = json.loads(pending_file.read_text()) or {}
            except Exception:
                self._pending = {}

    def _save_pending(self):
        (self._state_dir / "pending.json").write_text(json.dumps(self._pending))

    def _log_event(self, event: Dict[str, Any]):
        log_file = self._state_dir / "events.jsonl"
        row = {"ts": _now_ms(), **event}
        with log_file.open("a") as f:
            f.write(json.dumps(row) + "\n")

    @staticmethod
    def _is_approved(paired: Dict[str, Any]) -> bool:
        # Missing status (legacy devices) counts as approved for backward compat.
        return paired.get("status", "approved") == "approved"

    def _ensure_device(self, device_id: str, connect_params: Dict[str, Any]) -> Dict[str, Any]:
        if device_id in self._paired:
            return self._paired[device_id]
        approved = self._auto_approve
        token = secrets.token_hex(32)
        client = connect_params.get("client", {})
        paired = {
            "deviceId": device_id,
            "displayName": client.get("displayName", "Unknown"),
            "platform": client.get("platform"),
            "deviceFamily": client.get("deviceFamily"),
            "deviceToken": token,
            "status": "approved" if approved else "pending",
            "createdAtMs": _now_ms(),
            "approvedAtMs": _now_ms() if approved else None,
        }
        self._paired[device_id] = paired
        if approved:
            self._device_tokens.add(token)
        self._save_state()
        self._log_event({"type": "approved" if approved else "pending", "deviceId": device_id})
        if not approved:
            logger.info("[r1_shim] device %s is pending approval (auto_approve=false). "
                        "Approve at http://<host>:%d/approve?token=<gateway-token>&deviceId=%s",
                        device_id, self._port, device_id)
        return paired

    def approve_device(self, device_id: str) -> bool:
        """Mark a pending device approved (operator action). Returns True if it changed."""
        p = self._paired.get(device_id)
        if not p:
            return False
        if self._is_approved(p):
            return False
        p["status"] = "approved"
        p["approvedAtMs"] = _now_ms()
        if p.get("deviceToken"):
            self._device_tokens.add(p["deviceToken"])
        self._save_state()
        self._log_event({"type": "approved_by_operator", "deviceId": device_id})
        return True

    async def _send_chat_event(self, ws, content: str, run_id: Optional[str] = None,
                               session_key: str = "main") -> None:
        """Push a single final assistant chat event to a connected R1."""
        await ws.send_json({
            "type": "event", "event": "chat",
            "payload": {
                "runId": run_id or secrets.token_hex(8),
                "sessionKey": session_key,
                "seq": 0,
                "state": "final",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": content}],
                    "timestamp": _now_ms(),
                },
            },
        })

    def _queue_pending(self, device_id: str, content: str) -> None:
        q = self._pending.setdefault(device_id, [])
        q.append(content)
        if len(q) > MAX_PENDING_PER_DEVICE:
            del q[: len(q) - MAX_PENDING_PER_DEVICE]  # keep newest
        self._save_pending()

    async def _flush_pending(self, device_id: str, ws) -> None:
        msgs = self._pending.pop(device_id, [])
        if not msgs:
            return
        self._save_pending()
        for i, m in enumerate(msgs):
            try:
                await self._send_chat_event(ws, m)
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning("[r1_shim] failed to flush queued message to %s: %s", device_id, e)
                # re-queue this and the rest, preserving order
                for leftover in msgs[i:]:
                    self._queue_pending(device_id, leftover)
                break

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            # Serve admin page for browser visits
            token_ok = request.query.get("token", "") == self._token
            return web.Response(text=self._admin_html(token_ok), content_type="text/html")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        conn_id = secrets.token_hex(6)
        nonce = secrets.token_hex(16)
        device_id = None
        self._log_event({"type": "ws_open", "connId": conn_id, "remote": request.remote})

        # Send challenge
        await ws.send_json({
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": nonce, "ts": _now_ms()},
        })

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except Exception:
                continue

            # Frame capture for protocol discovery: record each inbound frame's param
            # keys + a truncated snapshot so new frame shapes (voice, camera, attachments)
            # can be reverse-engineered from events.jsonl. The connect frame is captured
            # too, but with its auth token redacted — its device/client/scopes reveal what
            # the R1 advertises (e.g. an audio/talk capability we could target for TTS).
            _method = frame.get("method", "")
            _params = frame.get("params", {}) if isinstance(frame.get("params"), dict) else {}
            _log_row = {"type": "frame_in", "connId": conn_id, "method": _method,
                        "paramKeys": sorted(_params.keys())}
            if _method == "connect":
                _red = dict(_params)
                if isinstance(_red.get("auth"), dict):
                    _red["auth"] = {k: ("<redacted>" if k == "token" else v)
                                    for k, v in _red["auth"].items()}
                _log_row["paramsSnippet"] = json.dumps(_red)[:1000]
            else:
                _log_row["paramsSnippet"] = json.dumps(_params)[:600]
            self._log_event(_log_row)

            if frame.get("type") == "req" and frame.get("method") == "connect":
                params = frame.get("params", {})
                auth = params.get("auth", {})
                supplied = auth.get("token", "")

                if supplied != self._token and supplied not in self._device_tokens:
                    await ws.send_json({
                        "type": "res", "id": frame.get("id"), "ok": False,
                        "error": {"code": "UNAUTHORIZED", "message": "auth token mismatch"},
                    })
                    await ws.close(code=1008, message=b"auth mismatch")
                    break

                device = params.get("device", {})
                device_id = device.get("id") or secrets.token_hex(12)
                paired = self._ensure_device(device_id, params)

                # Device approval gate (no-op when auto_approve, the default).
                if not self._is_approved(paired):
                    await ws.send_json({
                        "type": "res", "id": frame.get("id"), "ok": False,
                        "error": {"code": "PENDING_APPROVAL",
                                  "message": "device awaiting operator approval"},
                    })
                    self._log_event({"type": "connect_rejected_pending", "deviceId": device_id})
                    await ws.close(code=1008, message=b"pending approval")
                    break

                self._active_ws[conn_id] = ws
                self._device_ws[device_id] = ws

                await ws.send_json({
                    "type": "res", "id": frame.get("id"), "ok": True,
                    "payload": {
                        "type": "hello-ok",
                        "protocol": 3,
                        "policy": {"tickIntervalMs": 15000},
                        "auth": {
                            "deviceToken": paired["deviceToken"],
                            "role": params.get("role", "operator"),
                            "scopes": params.get("scopes") or ["operator.read", "operator.write"],
                        },
                        "presence": [],
                        "health": {"ok": True, "status": "ok"},
                        "stateVersion": 1,
                        "uptimeMs": 0,
                    },
                })
                # Deliver anything queued while this device was offline.
                await self._flush_pending(device_id, ws)
                continue

            if frame.get("type") == "req":
                rid = frame.get("id")
                method = frame.get("method", "")
                params = frame.get("params", {})

                if method in ("health", "gateway.health"):
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"ok": True, "status": "ok"}})

                elif method == "system-presence":
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"entries": []}})

                elif method in ("tools.catalog", "tools.effective"):
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"tools": []}})

                elif method == "chat.history":
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"sessionKey": params.get("sessionKey", "main"), "messages": []}})

                elif method == "chat.send":
                    message_text = params.get("message", "")
                    attachments = params.get("attachments") or []
                    run_id = params.get("idempotencyKey") or rid or secrets.token_hex(8)
                    session_key = params.get("sessionKey", "main")

                    # Ack immediately
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"runId": run_id, "status": "started"}})

                    # Fire agent in background so the WS read loop stays alive
                    asyncio.create_task(self._run_chat(
                        ws=ws,
                        device_id=device_id,
                        message_text=message_text,
                        run_id=run_id,
                        session_key=session_key,
                        attachments=attachments,
                    ))

                elif method in ("talk.speak", "tts.convert"):
                    # Talk-back: the R1 asks us to synthesize an assistant reply (or any text)
                    # and returns the audio in the RPC response. Run it off the read loop so
                    # synthesis latency doesn't stall other frames.
                    asyncio.create_task(self._handle_talk_speak(ws, rid, params))

                else:
                    # Echo unknown methods
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"method": method}})

        self._active_ws.pop(conn_id, None)
        # Drop the device->ws mapping only if it still points at this socket.
        if device_id and self._device_ws.get(device_id) is ws:
            self._device_ws.pop(device_id, None)
        self._log_event({"type": "ws_close", "connId": conn_id})
        return ws

    async def _handle_talk_speak(self, ws, rid, params) -> None:
        """Answer the R1's talk.speak RPC with synthesized audio (talk-back).

        The R1 fires talk.speak (when its on-device "speak replies" toggle is on) carrying
        the assistant text; we synthesize via edge-tts and return base64 audio in the RPC
        response. On any failure we return a fallback-eligible error so the R1 falls back to
        its own on-device Android TTS instead of going silent.
        """
        text = (params.get("text") or "").strip() if isinstance(params, dict) else ""
        if not text:
            await ws.send_json({"type": "res", "id": rid, "ok": False,
                                "error": {"code": "INVALID_ARGUMENT", "message": "empty text"}})
            return
        # The R1 may pass an ElevenLabs-style voiceId edge-tts won't know; only honor it if it
        # looks like a Microsoft voice (e.g. en-US-AriaNeural), else use the configured default.
        voice = params.get("voiceId") or self._tts_voice
        if not isinstance(voice, str) or "Neural" not in voice:
            voice = self._tts_voice
        try:
            audio = await self._tts_synthesize(text, voice)
            if not audio:
                raise RuntimeError("empty audio from synthesizer")
            if self._tts_format == "pcm":
                audio = await self._tts_to_pcm24k(audio)
                out_format, mime, ext = "pcm_24000", None, None
            else:
                out_format, mime, ext = "mp3_24000_48", "audio/mpeg", ".mp3"
            await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {
                "audioBase64": base64.b64encode(audio).decode("ascii"),
                "provider": "edge",
                "outputFormat": out_format,
                "mimeType": mime,
                "fileExtension": ext,
            }})
            self._log_event({"type": "talk_speak", "chars": len(text),
                             "bytes": len(audio), "format": out_format})
        except Exception as e:
            logger.warning("[r1_shim] talk.speak synth failed (%s) — R1 will use on-device TTS", e)
            await ws.send_json({"type": "res", "id": rid, "ok": False,
                "error": {"code": "UNAVAILABLE", "message": f"tts failed: {str(e)[:160]}",
                          "details": {"reason": "talk_unconfigured", "fallbackEligible": True}}})
            self._log_event({"type": "talk_speak_fallback", "error": str(e)[:200]})

    async def _tts_synthesize(self, text: str, voice: str) -> bytes:
        """Synthesize speech to MP3 bytes via edge-tts (free, no API key)."""
        if not EDGE_TTS_AVAILABLE:
            raise RuntimeError("edge-tts not installed")
        communicate = edge_tts.Communicate(text, voice)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.extend(chunk["data"])
        return bytes(buf)

    async def _tts_to_pcm24k(self, mp3_bytes: bytes) -> bytes:
        """Transcode MP3 -> raw signed-16-bit LE mono PCM @ 24kHz (the R1's AudioTrack format)."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "24000", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(mp3_bytes)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err[:200].decode('utf-8', 'replace')}")
        return out

    def _save_attachments(self, attachments) -> Tuple[List[str], List[str]]:
        """Decode inbound R1 attachments (base64) to files Hermes vision can read.

        The R1 sends a photo inside chat.send as
        ``attachments: [{type:"image", mimeType, fileName, content:<base64>}]``.
        Hermes carries inbound images as MessageEvent.media_urls (local file paths)
        + media_types, so we write each image to ~/.hermes/r1_shim/media/ and return
        the paths. Returns (media_urls, media_types).
        """
        paths: List[str] = []
        types: List[str] = []
        if not attachments:
            return paths, types
        media_dir = self._state_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        for att in attachments:
            if not isinstance(att, dict):
                continue
            content = att.get("content")
            mime = att.get("mimeType", "") or ""
            atype = att.get("type", "") or ""
            if not content:
                continue
            if atype == "image" or mime.startswith("image/"):
                try:
                    raw = base64.b64decode(content)
                except Exception as e:
                    logger.warning("[r1_shim] could not decode image attachment: %s", e)
                    continue
                ext = (".png" if "png" in mime else ".webp" if "webp" in mime
                       else ".gif" if "gif" in mime else ".jpg")
                path = media_dir / (secrets.token_hex(12) + ext)
                try:
                    path.write_bytes(raw)
                except Exception as e:
                    logger.warning("[r1_shim] could not write image attachment: %s", e)
                    continue
                paths.append(str(path))
                types.append(mime or "image/jpeg")
                self._log_event({"type": "attachment_saved", "kind": "image",
                                 "bytes": len(raw), "path": str(path)})
            else:
                # Unknown attachment kind (audio? video?) — log its shape for protocol work.
                logger.info("[r1_shim] unsupported attachment type=%s mime=%s (skipped)", atype, mime)
                self._log_event({"type": "attachment_skipped", "attType": atype, "mime": mime})
        self._prune_media(media_dir)
        return paths, types

    @staticmethod
    def _prune_media(media_dir, keep: int = 100) -> None:
        """Keep the media cache bounded — drop all but the newest `keep` files."""
        try:
            files = sorted(media_dir.glob("*"), key=lambda p: p.stat().st_mtime)
            for p in files[:-keep]:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    async def _run_chat(self, ws, device_id, message_text, run_id, session_key, attachments=None):
        """Run the agent in a background task and send the reply when done."""
        # Use the unified session source so R1 shares the same session as
        # the primary Telegram DM.  This means messages from R1 and Telegram
        # land in the same conversation with the same history.
        unified_chat_id = os.getenv("R1_SHIM_UNIFIED_CHAT_ID", "")
        unified_platform_str = os.getenv("R1_SHIM_UNIFIED_PLATFORM", "")
        if unified_chat_id and unified_platform_str:
            try:
                unified_platform = Platform(unified_platform_str)
            except ValueError:
                unified_platform = Platform.R1_SHIM
            source = SessionSource(
                platform=unified_platform,
                chat_id=unified_chat_id,
                chat_type="dm",
                user_id=unified_chat_id,
                user_name=self._paired.get(device_id, {}).get("displayName", "Rabbit R1"),
            )
        else:
            source = SessionSource(
                platform=Platform.R1_SHIM,
                chat_id=f"r1:{device_id}" if device_id else "r1:unknown",
                chat_type="dm",
                user_id=device_id,
                user_name=self._paired.get(device_id, {}).get("displayName", "Rabbit R1"),
            )
        media_urls, media_types = self._save_attachments(attachments)
        event = MessageEvent(
            text=message_text,
            message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
            source=source,
            media_urls=media_urls,
            media_types=media_types,
        )

        try:
            response = await self._message_handler(event) if self._message_handler else None
        except Exception as e:
            logger.error("[r1_shim] message handler error: %s", e, exc_info=True)
            response = f"Error: {str(e)[:300]}"

        reply_text = response or "(no response)"

        try:
            await ws.send_json({
                "type": "event", "event": "chat",
                "payload": {
                    "runId": run_id,
                    "sessionKey": session_key,
                    "seq": 0,
                    "state": "delta",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": reply_text}],
                        "timestamp": _now_ms(),
                    },
                },
            })
            await asyncio.sleep(0.05)
            await ws.send_json({
                "type": "event", "event": "chat",
                "payload": {
                    "runId": run_id,
                    "sessionKey": session_key,
                    "seq": 1,
                    "state": "final",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": reply_text}],
                        "timestamp": _now_ms(),
                    },
                },
            })
        except Exception as e:
            logger.warning("[r1_shim] failed to send reply (client may have disconnected): %s", e)

    def _admin_html(self, token_ok: bool = False) -> str:
        rows = []
        for d in self._paired.values():
            did = d.get("deviceId", "")
            status = d.get("status", "approved")
            approve = ""
            if status != "approved" and token_ok:
                approve = f" &nbsp; <a href='/approve?token={self._token}&deviceId={did}' style='color:#7af'>approve</a>"
            elif status != "approved":
                approve = " &nbsp; <span style='color:#888'>(append ?token=&lt;gateway-token&gt; to approve)</span>"
            rows.append(f"{did} — {d.get('displayName','?')} — <b>{status}</b>{approve}")
        body = "<br>".join(rows) or "(none)"
        return f"""<!doctype html>
<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>
<h1>R1 Shim (gateway adapter)</h1>
<p>Port: {self._port} | Paired: {len(self._paired)} | Auto-approve: {self._auto_approve} | Token: {self._token[:8]}...</p>
<h2>Devices</h2>
<div style='background:#222;padding:12px;border-radius:8px;line-height:1.8'>{body}</div>
</body></html>"""

    async def _approve(self, request: web.Request) -> web.Response:
        if request.query.get("token", "") != self._token:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=403)
        device_id = request.query.get("deviceId", "")
        if device_id not in self._paired:
            return web.json_response({"ok": False, "error": "unknown device"}, status=404)
        changed = self.approve_device(device_id)
        return web.json_response({"ok": True, "deviceId": device_id, "status": "approved", "changed": changed})

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "paired": len(self._paired), "online": len(self._device_ws)})

    # BasePlatformAdapter interface

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[r1_shim] aiohttp not available")
            return False
        try:
            self._app = web.Application()
            self._app.router.add_get("/", self._ws_handler)
            self._app.router.add_get("/approve", self._approve)
            self._app.router.add_get("/healthz", self._healthz)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await self._site.start()
            self._mark_connected()
            logger.info("[r1_shim] listening on ws://0.0.0.0:%d/", self._port)
            logger.info("[r1_shim] gateway token: %s", self._token)
            return True
        except Exception as e:
            logger.error("[r1_shim] failed to start: %s", e)
            return False

    async def disconnect(self) -> None:
        self._mark_disconnected()
        for ws in list(self._active_ws.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._active_ws.clear()
        self._device_ws.clear()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._app = None
        logger.info("[r1_shim] stopped")

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        """Proactive delivery: push `content` to the R1(s) for this chat_id.

        Used by the gateway's DeliveryRouter (cron jobs, cross-platform routing).
        A reply to a live chat.send is still sent inline in _run_chat; this path is
        for messages the R1 didn't ask for. Offline devices get the message queued
        and flushed on their next connect.
        """
        targets = self._resolve_targets(chat_id)
        if not targets:
            return SendResult(success=False, error="no R1 device for chat_id")
        delivered = 0
        for device_id in targets:
            ws = self._device_ws.get(device_id)
            if ws is not None and not ws.closed:
                try:
                    await self._send_chat_event(ws, content)
                    delivered += 1
                    continue
                except Exception as e:
                    logger.warning("[r1_shim] proactive send to %s failed, queuing: %s", device_id, e)
            self._queue_pending(device_id, content)
        # Success if delivered live to at least one device, or queued for later.
        return SendResult(success=True)

    def _resolve_targets(self, chat_id: str) -> List[str]:
        """Map a chat_id to the device ids it addresses.

        `r1:<device_id>` targets that one device; the unified-session chat id (or an
        unknown id) fans out to every approved paired device.
        """
        if chat_id and chat_id.startswith("r1:") and chat_id[3:] not in ("", "unknown"):
            return [chat_id[3:]]
        return [d for d, p in self._paired.items() if self._is_approved(p)]

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Rabbit R1", "type": "r1_shim"}
