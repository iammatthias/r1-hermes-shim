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
        auto_approve: true
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web, WSMsgType
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None
    WSMsgType = None

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
        self._runner = None
        self._site = None
        self._app = None
        self._paired: Dict[str, Dict[str, Any]] = {}
        self._device_tokens: set = set()
        self._active_ws: Dict[str, web.WebSocketResponse] = {}
        self._pending_responses: Dict[str, asyncio.Future] = {}

        # State persistence
        from hermes_constants import get_hermes_home
        self._state_dir = get_hermes_home() / STATE_DIR_NAME
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _load_state(self):
        state_file = self._state_dir / "paired.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                self._paired = data.get("paired", {})
                self._device_tokens = set(
                    p.get("deviceToken") for p in self._paired.values() if p.get("deviceToken")
                )
            except Exception:
                pass

    def _save_state(self):
        state_file = self._state_dir / "paired.json"
        state_file.write_text(json.dumps({"paired": self._paired}, indent=2))

    def _log_event(self, event: Dict[str, Any]):
        log_file = self._state_dir / "events.jsonl"
        row = {"ts": _now_ms(), **event}
        with log_file.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def _ensure_device(self, device_id: str, connect_params: Dict[str, Any]) -> Dict[str, Any]:
        if device_id in self._paired:
            return self._paired[device_id]
        token = secrets.token_hex(32)
        paired = {
            "deviceId": device_id,
            "displayName": connect_params.get("client", {}).get("displayName", "Unknown"),
            "platform": connect_params.get("client", {}).get("platform"),
            "deviceFamily": connect_params.get("client", {}).get("deviceFamily"),
            "deviceToken": token,
            "approvedAtMs": _now_ms(),
        }
        self._paired[device_id] = paired
        self._device_tokens.add(token)
        self._save_state()
        self._log_event({"type": "paired", "deviceId": device_id})
        return paired

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            # Serve admin page for browser visits
            return web.Response(text=self._admin_html(), content_type="text/html")

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

            self._log_event({"type": "frame_in", "connId": conn_id, "method": frame.get("method", "")})

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
                self._active_ws[conn_id] = ws

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
                    ))

                else:
                    # Echo unknown methods
                    await ws.send_json({"type": "res", "id": rid, "ok": True, "payload": {"method": method}})

        self._active_ws.pop(conn_id, None)
        self._log_event({"type": "ws_close", "connId": conn_id})
        return ws

    async def _run_chat(self, ws, device_id, message_text, run_id, session_key):
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
        event = MessageEvent(
            text=message_text,
            message_type=MessageType.TEXT,
            source=source,
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

    def _admin_html(self) -> str:
        paired_info = json.dumps(list(self._paired.values()), indent=2)
        return f"""<!doctype html>
<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>
<h1>R1 Shim (gateway adapter)</h1>
<p>Port: {self._port} | Paired: {len(self._paired)} | Token: {self._token[:8]}...</p>
<h2>Paired Devices</h2>
<pre style='background:#222;padding:12px;border-radius:8px'>{paired_info}</pre>
</body></html>"""

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "paired": len(self._paired)})

    # BasePlatformAdapter interface

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[r1_shim] aiohttp not available")
            return False
        try:
            self._app = web.Application()
            self._app.router.add_get("/", self._ws_handler)
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
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._app = None
        logger.info("[r1_shim] stopped")

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        # The response is sent inline in the WS handler, not through send()
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Rabbit R1", "type": "r1_shim"}
