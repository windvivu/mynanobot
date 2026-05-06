"""Zalo personal channel — unofficial, uses zca-js (Node.js) bridge."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

# Source files bundled with nanobot
_BRIDGE_DIR = Path(__file__).parent
_BRIDGE_JS = _BRIDGE_DIR / "zalo_bridge.js"
_BRIDGE_PKG = _BRIDGE_DIR / "zalo_bridge_package.json"


def _is_mentioned(content: str, bot_user_id: str, bot_name: str,
                  mentioned_ids: list[str], quoted_sender_id: str | None = None) -> bool:
    """Return True if the bot appears to be @mentioned or replied-to in a group message."""
    # 1. Explicit mention via userId (from zca-js mentions map)
    if bot_user_id and bot_user_id in mentioned_ids:
        return True
    # 2. Name mention in content text (e.g. "@Nanobot" or "@nanobot")
    if bot_name:
        lower = content.lower()
        if f"@{bot_name.lower()}" in lower or bot_name.lower() in lower[:40]:
            return True
    # 3. Reply/quote to bot's own message
    if bot_user_id and quoted_sender_id and quoted_sender_id == bot_user_id:
        return True
    return False


def _strip_mention(content: str, bot_name: str) -> str:
    """Remove leading @mention of the bot from message content."""
    if not bot_name:
        return content
    import re
    # Remove @BotName at start, case-insensitive
    cleaned = re.sub(rf"^@?{re.escape(bot_name)}\s*[,:]?\s*", "", content, flags=re.IGNORECASE)
    return cleaned.strip() or content


class ZaloPersonalConfig(Base):
    """Zalo personal channel configuration (unofficial, uses zca-js Node.js bridge)."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Zalo user IDs (* = all, behind QR auth)
    node_path: str = "node"  # Path to Node.js binary


class ZaloPersonalChannel(BaseChannel):
    """
    Zalo personal channel using zca-js Node.js bridge.

    Spawns a Node.js subprocess that communicates via stdin/stdout JSON lines.
    QR code is generated on-demand via the Web Dashboard /zalo/setup page.

    ⚠️ This uses an unofficial API — account may be locked/banned.
    """

    name = "zalo"
    display_name = "Zalo"

    # Connection states
    STATUS_DISCONNECTED = "disconnected"
    STATUS_CONNECTING = "connecting"      # Bridge spawned, waiting for QR scan
    STATUS_CONNECTED = "connected"         # Logged in, listening for messages
    STATUS_AUTH_REQUIRED = "auth_required" # Session expired — dashboard must trigger QR re-scan

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        # Parse dict → typed config so BaseChannel.is_allowed() works correctly
        if isinstance(config, dict):
            config = ZaloPersonalConfig.model_validate(config)
            self.config = config  # override self.config set by super().__init__
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._bridge_dir: Path | None = None
        self._node_path: str = getattr(config, "node_path", "node")
        self._ready_event = asyncio.Event()
        self._status: str = self.STATUS_DISCONNECTED
        self._user_id: str = ""
        self._user_name: str = ""
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 5
        self._intentional_disconnect: bool = False  # set True when user explicitly disconnects
        self._pending_commands: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Rate limit: track send timestamps per chat_id to prevent runaway loops
        self._send_timestamps: dict[str, list[float]] = {}
        self._max_sends_per_window: int = 5     # max messages per chat per window (raised from 3 to support multi-message splits)
        self._send_window_seconds: float = 10.0 # sliding window size (reduced from 30s to reset faster between turns)
        # Registry: remember thread_type per chat_id from inbound messages.
        # Used as fallback when outbound metadata lacks thread_type (e.g. cross-chat message tool).
        self._known_thread_types: dict[str, str] = {}
        # Tracks which chat_ids have already received the one-per-turn processing hint.
        # Cleared when a real (non-progress) message is sent to that chat.
        self._hint_sent: set[str] = set()

    # ── BaseChannel interface ─────────────────────────────

    async def start(self) -> None:
        """Start the channel — just setup bridge files, don't connect yet.

        Connection is triggered on-demand from the Web Dashboard /zalo/setup page.
        """
        self._running = True

        # Setup bridge (copy files, npm install) so it's ready when user clicks Connect
        try:
            await self._setup_bridge()
            logger.info("Zalo channel ready — use Web Dashboard /zalo/setup to connect")
        except Exception as e:
            logger.error("Failed to setup Zalo bridge: {}", e)
            self._running = False

    async def stop(self) -> None:
        """Stop the channel and disconnect if connected."""
        self._running = False
        await self.disconnect()
        logger.info("Zalo channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Zalo."""
        if self._status != self.STATUS_CONNECTED:
            logger.warning("Zalo not connected, cannot send")
            return

        if not self._process or self._process.returncode is not None:
            logger.warning("Zalo bridge not running, cannot send")
            return

        # Progress messages: send one "processing" hint per chat per turn, drop the rest
        if msg.metadata.get("_progress", False):
            if msg.metadata.get("_tool_hint") and msg.chat_id not in self._hint_sent:
                self._hint_sent.add(msg.chat_id)
                hint_thread_type = (
                    msg.metadata.get("thread_type")
                    or self._known_thread_types.get(msg.chat_id, "User")
                )
                try:
                    await self._send_command({
                        "action": "send",
                        "threadId": msg.chat_id,
                        "threadType": hint_thread_type,
                        "content": "⏳ Đang xử lý, xin đợi...",
                    }, wait_for_ack=True)
                    logger.debug("[Zalo] Sent processing hint to {}", msg.chat_id)
                except Exception as e:
                    logger.debug("[Zalo] Failed to send processing hint: {}", e)
            return

        if not msg.content or msg.content == "[empty message]":
            if not msg.media:
                return
            # Has media but no text — skip text send, continue to send media only

        # LLM chose to stay silent in group ambient mode
        if msg.content and msg.content.strip() == "__SILENT__":
            logger.debug("[Zalo] Group AMBIENT mode — LLM chose not to respond")
            return

        # ── Rate limiting: prevent runaway send loops ──────────────────
        now = time.monotonic()
        chat_id = msg.chat_id
        timestamps = self._send_timestamps.setdefault(chat_id, [])
        # Purge entries outside the window
        cutoff = now - self._send_window_seconds
        self._send_timestamps[chat_id] = [t for t in timestamps if t > cutoff]
        timestamps = self._send_timestamps[chat_id]

        if len(timestamps) >= self._max_sends_per_window:
            logger.warning(
                "Zalo rate limit: {} msgs to {} in {}s — dropping message",
                len(timestamps), chat_id, self._send_window_seconds,
            )
            raise RuntimeError(
                f"Zalo rate limit: {len(timestamps)} messages in {self._send_window_seconds}s for {chat_id}"
            )
        timestamps.append(now)

        thread_type = msg.metadata.get("thread_type") or self._known_thread_types.get(chat_id, "User")
        if not msg.metadata.get("thread_type") and thread_type != "User":
            logger.debug("[Zalo] thread_type resolved from registry for {}: {}", chat_id, thread_type)

        # Send text message (if any)
        if msg.content and msg.content not in ("", "[empty message]"):
            await self._send_command({
                "action": "send",
                "threadId": msg.chat_id,
                "threadType": thread_type,
                "content": msg.content,
            }, wait_for_ack=True)

        # ── Send media files if any ──
        if msg.media:
            for media_path in msg.media:
                media_type = self._detect_media_type(media_path)
                try:
                    await self._send_command({
                        "action":     "send_media",
                        "threadId":   msg.chat_id,
                        "threadType": thread_type,
                        "content":    "",
                        "filePath":   str(Path(media_path).resolve()),
                        "mediaType":  media_type,
                    }, wait_for_ack=True)
                    logger.info("[Zalo] Queued media send: {} ({})", Path(media_path).name, media_type)
                except Exception as e:
                    logger.warning("[Zalo] Failed to queue media {}: {}", media_path, e)
                    raise

        # Real message sent — reset hint tracker so next turn can send a new hint
        self._hint_sent.discard(chat_id)

    @staticmethod
    def _detect_media_type(path: str) -> str:
        """Detect media type from file extension for Zalo bridge."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "webp"):
            return "photo"
        if ext == "gif":
            return "gif"
        if ext in ("mp4", "mov", "avi", "mkv"):
            return "video"
        if ext in ("ogg", "mp3", "m4a", "aac", "opus"):
            return "voice"
        return "file"

    # ── Public methods for Web Dashboard ──────────────────

    async def connect(self) -> dict:
        """Start the bridge and initiate QR login.

        Returns:
            dict with {"status": "connecting"} or {"status": "error", "message": "..."}
        """
        if self._status == self.STATUS_CONNECTED:
            return {"status": "already_connected", "userId": self._user_id}

        if self._status == self.STATUS_CONNECTING:
            return {"status": "already_connecting"}

        self._intentional_disconnect = False
        self._status = self.STATUS_CONNECTING
        self._ready_event.clear()

        # Ensure bridge is set up
        if not self._bridge_dir:
            try:
                await self._setup_bridge()
            except Exception as e:
                self._status = self.STATUS_DISCONNECTED
                return {"status": "error", "message": str(e)}

        # Spawn Node.js bridge process
        try:
            bridge_js = self._bridge_dir / "zalo_bridge.js"
            self._process = await asyncio.create_subprocess_exec(
                self._node_path, str(bridge_js),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._bridge_dir),
            )
            logger.info("Zalo bridge process started (PID: {})", self._process.pid)
        except FileNotFoundError:
            self._status = self.STATUS_DISCONNECTED
            msg = (
                f"Node.js not found at '{self._node_path}'. "
                "Install Node.js >= 18 or set channels.zalo.nodePath in config."
            )
            logger.error(msg)
            return {"status": "error", "message": msg}
        except Exception as e:
            self._status = self.STATUS_DISCONNECTED
            return {"status": "error", "message": str(e)}

        # Start reading bridge output
        self._reader_task = asyncio.create_task(self._read_bridge_output())
        self._stderr_task = asyncio.create_task(self._read_bridge_stderr())

        return {"status": "connecting"}

    async def disconnect(self) -> dict:
        """Disconnect from Zalo and kill bridge process."""
        if self._status == self.STATUS_DISCONNECTED:
            return {"status": "already_disconnected"}
        self._intentional_disconnect = True

        # Send stop command to bridge
        if self._process and self._process.stdin and self._process.returncode is None:
            try:
                await self._send_command({"action": "stop"})
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("Zalo bridge did not exit gracefully, killing...")
                    self._process.kill()
            except Exception as e:
                logger.debug("Error stopping Zalo bridge: {}", e)
                if self._process and self._process.returncode is None:
                    self._process.kill()

        # Cancel reader tasks
        for task in [self._reader_task, self._stderr_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._status = self.STATUS_DISCONNECTED
        self._ready_event.clear()
        self._user_id = ""
        self._user_name = ""
        for future in self._pending_commands.values():
            if not future.done():
                future.set_exception(RuntimeError("Zalo bridge disconnected before delivery ack"))
        self._pending_commands.clear()

        logger.info("Zalo disconnected")
        return {"status": "disconnected"}

    async def _auto_reconnect(self) -> None:
        """Attempt to automatically reconnect after an unexpected disconnection.

        Uses exponential backoff (5s, 10s, 20s, ..., capped at 5 min).
        Gives up after _max_reconnect_attempts and sets STATUS_AUTH_REQUIRED.
        """
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.error(
                "Zalo auto-reconnect: max attempts ({}) reached — manual re-auth required",
                self._max_reconnect_attempts,
            )
            self._status = self.STATUS_AUTH_REQUIRED
            return

        self._reconnect_attempts += 1
        delay = min(5 * (2 ** (self._reconnect_attempts - 1)), 300)  # 5s, 10s, 20s… cap 5 min
        logger.info(
            "Zalo auto-reconnect in {}s (attempt {}/{})",
            delay, self._reconnect_attempts, self._max_reconnect_attempts,
        )
        await asyncio.sleep(delay)

        if not self._running:
            return  # Channel was stopped while waiting — abort

        # Clean up the dead process before spawning a new one
        await self.disconnect()
        result = await self.connect()
        if result.get("status") in ("connecting", "already_connected", "already_connecting"):
            logger.info("Zalo auto-reconnect: bridge restarted ({})", result.get("status"))
        else:
            logger.warning("Zalo auto-reconnect: connect() returned {}", result)
            asyncio.create_task(self._auto_reconnect())

    def get_status(self) -> dict:
        """Get current connection status."""
        result = {
            "status": self._status,
            "userId": self._user_id,
            "userName": self._user_name,
        }

        # Check if QR image exists
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            result["hasQr"] = qr_path.exists()
            # Check if saved credentials exist
            creds_path = self._bridge_dir / "credentials.json"
            result["hasSavedSession"] = creds_path.exists()
        else:
            result["hasQr"] = False
            result["hasSavedSession"] = False

        return result

    def get_qr_path(self) -> Path | None:
        """Get path to QR code image file, or None if not available."""
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            if qr_path.exists():
                return qr_path
        return None

    # ── Internal methods ──────────────────────────────────

    async def _setup_bridge(self) -> None:
        """Setup the bridge directory with Node.js files and install dependencies."""
        from nanobot.config.paths import get_data_dir

        data_dir = get_data_dir()
        self._bridge_dir = data_dir / "zalo_bridge"
        self._bridge_dir.mkdir(parents=True, exist_ok=True)

        # Copy bridge files
        dest_js = self._bridge_dir / "zalo_bridge.js"
        dest_pkg = self._bridge_dir / "package.json"

        shutil.copy2(_BRIDGE_JS, dest_js)
        shutil.copy2(_BRIDGE_PKG, dest_pkg)
        logger.debug("Copied Zalo bridge files to {}", self._bridge_dir)

        # Install npm dependencies if needed
        node_modules = self._bridge_dir / "node_modules"
        if not node_modules.exists():
            logger.info("Installing Zalo bridge dependencies (first time)...")
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            proc = await asyncio.create_subprocess_exec(
                npm_cmd, "install", "--production",
                cwd=str(self._bridge_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace")
                raise RuntimeError(f"npm install failed: {err_msg}")
            logger.info("Zalo bridge dependencies installed successfully")

    async def _send_command(self, cmd: dict, *, wait_for_ack: bool = False, timeout_s: float = 20.0) -> dict[str, Any] | None:
        """Send a JSON command to the bridge via stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Zalo bridge is not running")
        future: asyncio.Future[dict[str, Any]] | None = None
        cmd_id: str | None = None
        try:
            if wait_for_ack:
                cmd_id = uuid.uuid4().hex
                cmd = {**cmd, "cmdId": cmd_id}
                future = asyncio.get_running_loop().create_future()
                self._pending_commands[cmd_id] = future
            line = json.dumps(cmd, ensure_ascii=False) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()
            if future is not None:
                return await asyncio.wait_for(future, timeout=timeout_s)
            return None
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Failed to send command to Zalo bridge: {}", e)
            raise RuntimeError(f"Failed to send command to Zalo bridge: {e}") from e
        finally:
            if cmd_id and future is not None:
                self._pending_commands.pop(cmd_id, None)

    async def _read_bridge_output(self) -> None:
        """Read JSON lines from bridge stdout and dispatch events."""
        if not self._process or not self._process.stdout:
            return

        try:
            while self._running and self._process.returncode is None:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    break  # EOF — process exited

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON from Zalo bridge: {}", line[:200])
                    continue

                await self._handle_bridge_event(event)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error reading Zalo bridge output: {}", e)
        finally:
            if self._running and self._status == self.STATUS_CONNECTED and not self._intentional_disconnect:
                logger.warning("Zalo bridge output stream ended unexpectedly — scheduling reconnect")
                self._status = self.STATUS_DISCONNECTED
                asyncio.create_task(self._auto_reconnect())

    async def _read_bridge_stderr(self) -> None:
        """Read bridge stderr and forward to loguru (debug logs)."""
        if not self._process or not self._process.stderr:
            return

        try:
            while self._process.returncode is None:
                line_bytes = await self._process.stderr.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if line:
                    logger.debug("[ZaloBridge] {}", line)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_bridge_event(self, event: dict) -> None:
        """Handle an event from the Zalo bridge."""
        event_type = event.get("event", "")

        if event_type == "ready":
            self._user_id = event.get("userId", "unknown")
            self._status = self.STATUS_CONNECTED
            self._reconnect_attempts = 0  # reset backoff counter on successful connect
            self._ready_event.set()
            logger.info("Zalo logged in as user {}", self._user_id)

            # Delete QR image after successful login
            if self._bridge_dir:
                qr_path = self._bridge_dir / "qr.png"
                if qr_path.exists():
                    qr_path.unlink(missing_ok=True)

        elif event_type == "qr":
            logger.info("Zalo QR code received — scan with Zalo app")

        elif event_type == "message":
            thread_id = event.get("threadId", "")
            sender_id = event.get("senderId", thread_id)
            content = event.get("content", "")
            thread_type = event.get("threadType", "User")
            sender_name = event.get("senderName", "")
            mentioned_ids = event.get("mentionedIds", [])
            quoted_sender_id = event.get("quotedSenderId")  # None if not a reply
            quoted_content = event.get("quotedContent")     # None if no quoted text
            # ── Media fields ──
            media_url   = event.get("mediaUrl")    # None for text-only messages
            media_thumb = event.get("mediaThumb")  # reserved, not used yet
            media_type  = event.get("mediaType")   # "photo"|"video"|"voice"|"gif"|"file"|None

            if not content and not media_url:
                return

            # ── Download media if present ──
            media_paths: list[str] = []
            if media_url:
                try:
                    import time as _time
                    import httpx
                    from nanobot.config.paths import get_media_dir
                    ext_map = {
                        "photo": ".jpg", "gif": ".gif", "video": ".mp4",
                        "voice": ".ogg", "sticker": ".png", "file": ".bin",
                    }
                    ext = ext_map.get(media_type or "", ".bin")
                    media_dir = get_media_dir("zalo")
                    media_dir.mkdir(parents=True, exist_ok=True)
                    unique_id = f"{thread_id}_{int(_time.time() * 1000)}"
                    file_path = media_dir / f"{unique_id}{ext}"
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(media_url)
                        resp.raise_for_status()
                        await asyncio.to_thread(file_path.write_bytes, resp.content)
                    media_paths.append(str(file_path))
                    logger.info("[Zalo] Downloaded {} → {}", media_type, file_path.name)
                except Exception as e:
                    logger.warning("[Zalo] Failed to download media: {}", e)
                    # Continue — still forward any text content if present


            # ── Group chat filter (Option C) ───────────────────────────
            group_ambient = False
            if thread_type == "Group":
                mentioned = _is_mentioned(
                    content, self._user_id, self._user_name,
                    mentioned_ids, quoted_sender_id,
                )
                if mentioned:
                    # Strip @mention from content before forwarding to agent
                    content = _strip_mention(content, self._user_name)
                    logger.debug("[Zalo] Group message — bot mentioned/replied-to, responding")
                else:
                    # Not mentioned: pass to LLM in AMBIENT mode
                    group_ambient = True
                    logger.debug(
                        "[Zalo] Group message from {} — AMBIENT mode (LLM decides)",
                        sender_name or sender_id,
                    )

            session_key = f"zalo:{thread_id}"

            logger.info(
                "[Zalo] Message from {} ({}{}) : {}",
                sender_name or sender_id,
                thread_type,
                " · AMBIENT" if group_ambient else "",
                content[:80] if content else "[" + (media_type or "media") + " media]",
            )

            # Build ambient note: prepended to content so LLM sees the instruction
            # without modifying the session history in a confusing way.
            deliver_content = content

            # Prepend quoted context so LLM always knows what was replied to
            if quoted_content:
                deliver_content = f"[Quoted: \"{quoted_content}\"]\n\n{deliver_content}"

            if group_ambient:
                ambient_note = (
                    f"[GROUP AMBIENT — you were NOT @mentioned. "
                    f"The following is a group message from {sender_name or sender_id}. "
                    "Only respond if it is clearly directed at you or you can genuinely help. "
                    "If the message is just general group chat, output exactly: __SILENT__]\n\n"
                    f"{sender_name or sender_id}: {content if content else '[sent a media]'}"
                )
                deliver_content = ambient_note

            # Record thread_type so outbound can resolve it for cross-chat sends
            self._known_thread_types[thread_id] = thread_type

            await self._handle_message(
                sender_id=sender_id,
                chat_id=thread_id,
                content=deliver_content,
                media=media_paths if media_paths else None,
                metadata={
                    "thread_type": thread_type,
                    "sender_name": sender_name,
                    "group_ambient": group_ambient,
                },
                session_key=session_key,
            )

        elif event_type == "disconnected":
            reason = event.get("reason", "unknown")
            logger.warning("Zalo disconnected: {}", reason)
            if reason in ("auth_expired", "max_reconnect_reached"):
                # JS bridge exhausted all retries or session is invalid.
                # User must manually re-scan QR via dashboard.
                self._status = self.STATUS_AUTH_REQUIRED
                logger.error(
                    "Zalo requires re-authentication — please scan QR again in dashboard"
                )
            else:
                # Unexpected drop — Python side triggers its own reconnect.
                self._status = self.STATUS_DISCONNECTED
                if self._running and not self._intentional_disconnect:
                    asyncio.create_task(self._auto_reconnect())

        elif event_type == "error":
            message = event.get("message", "unknown error")
            logger.error("Zalo bridge error: {}", message)

        elif event_type in {"sent", "send_error"}:
            cmd_id = event.get("cmdId")
            if not cmd_id:
                return
            future = self._pending_commands.get(cmd_id)
            if not future or future.done():
                return
            if event_type == "sent":
                future.set_result(event)
            else:
                future.set_exception(RuntimeError(event.get("message", "Unknown Zalo send error")))

        else:
            logger.debug("Unknown Zalo bridge event: {}", event_type)
