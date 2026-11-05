"""Explicit, bounded server-side OpenAI Realtime WebSocket session."""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus, WebSocketException

from ..config import Settings
from .events import ConversationEvent, ConversationState, EventTracker

LOGGER = logging.getLogger(__name__)


class RealtimeError(RuntimeError):
    """A safe-to-display Realtime transport or protocol error."""

    def __init__(self, message: str, *, category: str | None = None,
                 close_code: int | None = None):
        super().__init__(message)
        self.category = category
        self.close_code = close_code


def _close_diagnostics(exc: ConnectionClosed) -> tuple[str, int | None]:
    """Use only the close-frame category and numeric code, never its reason text."""
    frame = exc.rcvd
    if frame is None:
        # websockets 15 emits this exact local close when its Ping receives no
        # Pong before ping_timeout. Do not expose the close reason in logs.
        if (exc.sent is not None and exc.sent.code == 1011 and
                exc.sent.reason == "keepalive ping timeout"):
            return "keepalive_timeout", None
        return "abnormal_no_frame", None
    code = frame.code if isinstance(frame.code, int) and 1000 <= frame.code <= 4999 else None
    return ("remote_clean" if isinstance(exc, ConnectionClosedOK) else "remote_error"), code


class RealtimeClient:
    def __init__(self, settings: Settings, *, connector: Callable[..., Any] = websocket_connect,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[..., Any] = asyncio.sleep):
        self.settings = settings
        self.connector = connector
        self.clock = clock
        self.sleep = sleep
        self.tracker = EventTracker()
        self.socket = None
        self.started_at: float | None = None
        self.last_activity: float | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._activity = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._expiry_reason: str | None = None

    @property
    def state(self) -> ConversationState:
        return self.tracker.state

    @property
    def session_id(self) -> str | None:
        return self.tracker.session_id

    async def connect(self) -> ConversationEvent:
        if self.socket is not None:
            raise RealtimeError("Realtime session is already connected")
        if not self.settings.openai_api_key:
            raise RealtimeError("OPENAI_API_KEY is not configured")
        self.tracker = EventTracker(state=ConversationState.CONNECTING)
        self._expiry_reason = None
        self._activity.clear()
        url = "wss://api.openai.com/v1/realtime?model=" + quote(self.settings.realtime_model, safe="")
        for attempt in range(self.settings.realtime_retry_limit + 1):
            try:
                self.socket = await asyncio.wait_for(self.connector(
                    url, additional_headers={"Authorization": "Bearer " + self.settings.openai_api_key},
                    open_timeout=self.settings.realtime_connect_timeout,
                    close_timeout=3, ping_interval=20, ping_timeout=10, max_size=4 * 1024 * 1024,
                ), timeout=self.settings.realtime_connect_timeout)
                self.started_at = self.last_activity = self.clock()
                created = await self._receive_ack("session.created")
                await self._send_raw({"type": "session.update", "session": {
                    "type": "realtime", "model": self.settings.realtime_model,
                    "output_modalities": ["text"],
                    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                        "turn_detection": None},
                              "output": {"format": {"type": "audio/pcm", "rate": 24000}}},
                }})
                updated = await self._receive_ack("session.updated")
                if updated.session_id != created.session_id:
                    raise RealtimeError("Realtime session acknowledgment changed session ID")
                self._watchdog = asyncio.create_task(self._watch_limits())
                return updated
            except InvalidStatus as exc:
                status = exc.response.status_code
                await self.close()
                if status in (401, 403):
                    raise RealtimeError("OpenAI authentication failed; check the configured API key") from None
                if status not in (429, 500, 502, 503, 504) or attempt == self.settings.realtime_retry_limit:
                    raise RealtimeError(f"OpenAI connection rejected (HTTP {status})") from None
            except (OSError, TimeoutError, ConnectionClosed):
                await self.close()
                if attempt == self.settings.realtime_retry_limit:
                    raise RealtimeError("OpenAI connection failed or timed out") from None
            except WebSocketException:
                await self.close()
                raise RealtimeError("OpenAI WebSocket protocol error") from None
            except RealtimeError:
                await self.close()
                raise
            self.tracker = EventTracker(state=ConversationState.CONNECTING)
            await self.sleep(self.settings.realtime_backoff_seconds * 2 ** attempt)
        raise RealtimeError("OpenAI connection retry limit reached")

    async def _receive_ack(self, expected: str) -> ConversationEvent:
        if self.socket is None:
            raise RealtimeError("Realtime session is disconnected")
        try:
            raw = await asyncio.wait_for(self.socket.recv(), self.settings.realtime_connect_timeout)
        except (TimeoutError, ConnectionClosed, OSError):
            raise RealtimeError(f"Timed out waiting for {expected}") from None
        event = self.tracker.parse(raw, self.clock())
        if event.kind == "error":
            raise RealtimeError(f"OpenAI rejected session configuration ({event.error_code or 'server error'})")
        if event.kind != expected:
            raise RealtimeError(f"Expected {expected} acknowledgment")
        return event

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        try:
            async with self._send_lock:
                socket = self.socket
                if socket is None:
                    raise RealtimeError("Realtime session is disconnected")
                await asyncio.wait_for(socket.send(json.dumps(payload)), self.settings.realtime_connect_timeout)
        except TimeoutError:
            await self.close()
            raise RealtimeError("Realtime send failed", category="local_timeout") from None
        except ConnectionClosed as exc:
            category, code = _close_diagnostics(exc)
            if self.socket is None:
                category = "local_timeout" if self._expiry_reason else "local_shutdown"
                code = None
            await self.close()
            raise RealtimeError("Realtime send failed", category=category, close_code=code) from None
        except OSError:
            category = ("local_timeout" if self._expiry_reason else "local_shutdown") if self.socket is None else "socket_error"
            await self.close()
            raise RealtimeError("Realtime send failed", category=category) from None
        except WebSocketException:
            await self.close()
            raise RealtimeError("Realtime WebSocket protocol error") from None
        self.last_activity = self.clock()
        self._activity.set()

    def _deadline(self) -> tuple[float, str]:
        assert self.started_at is not None and self.last_activity is not None
        deadlines = [(self.started_at + self.settings.realtime_max_session_seconds, "Session duration limit reached"),
                     (self.last_activity + self.settings.realtime_idle_seconds, "Session idle timeout reached")]
        deadlines.extend((started + self.settings.realtime_max_response_seconds, "Response duration limit reached")
                         for started in self.tracker.response_started.values())
        return min(deadlines)

    async def _watch_limits(self) -> None:
        while self.socket is not None and self.started_at is not None:
            # Clear before reading the deadline so activity during this window
            # cannot be lost and mistaken for an expired idle period.
            self._activity.clear()
            deadline, reason = self._deadline()
            remaining = deadline - self.clock()
            if remaining <= 0:
                current_deadline, reason = self._deadline()
                if self.clock() < current_deadline:
                    continue
                self._expiry_reason = reason
                LOGGER.warning("Realtime watchdog timeout (category=%s)", self._timeout_category(reason))
                await self.close()
                return
            try:
                # asyncio.wait_for(Event.wait()) creates a child task. On
                # Python 3.11, cancelling this watchdog while that child is
                # pending can leave close() awaiting the watchdog forever.
                # asyncio.timeout() bounds the wait on this task directly.
                async with asyncio.timeout(remaining):
                    await self._activity.wait()
            except TimeoutError:
                if self.socket is None or self.started_at is None:
                    return
                current_deadline, reason = self._deadline()
                if self.clock() < current_deadline:
                    continue
                self._expiry_reason = reason
                LOGGER.warning("Realtime watchdog timeout (category=%s)", self._timeout_category(reason))
                await self.close()
                return

    @staticmethod
    def _timeout_category(reason: str) -> str:
        return {"Session duration limit reached": "session_limit",
                "Session idle timeout reached": "idle_timeout",
                "Response duration limit reached": "response_limit"}.get(reason, "unknown")

    async def send(self, payload: dict[str, Any]) -> None:
        if self.socket is not None and self.started_at is not None:
            deadline, reason = self._deadline()
            if self.clock() >= deadline:
                self._expiry_reason = reason
                await self.close()
                raise RealtimeError(reason, category="local_timeout")
        if self.state not in {ConversationState.READY, ConversationState.LISTENING,
                              ConversationState.PROCESSING, ConversationState.RESPONDING}:
            raise RealtimeError("Realtime session is not ready")
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise RealtimeError("Realtime event must have a type")
        try:
            size = len(json.dumps(payload))
        except (TypeError, ValueError):
            raise RealtimeError("Realtime event must be JSON serializable") from None
        if size > 4 * 1024 * 1024:
            raise RealtimeError("Realtime event exceeds size limit")
        await self._send_raw(payload)

    async def receive(self) -> ConversationEvent:
        if self.socket is None or self.started_at is None or self.last_activity is None:
            raise RealtimeError(self._expiry_reason or "Realtime session is disconnected",
                                category="local_timeout" if self._expiry_reason else "local_shutdown")
        while True:
            now = self.clock()
            deadline, reason = self._deadline()
            if now >= deadline:
                self._expiry_reason = reason
                await self.close()
                raise RealtimeError(reason, category="local_timeout")
            socket = self.socket
            if socket is None:
                raise RealtimeError(self._expiry_reason or "Realtime session is disconnected",
                                    category="local_timeout" if self._expiry_reason else "local_shutdown")
            try:
                raw = await asyncio.wait_for(socket.recv(), deadline - now)
                break
            except TimeoutError:
                # A tool output or response.create may have extended activity
                # after this receive began. Recalculate rather than close on
                # the stale deadline.
                if self.socket is socket and self.clock() < self._deadline()[0]:
                    await asyncio.sleep(0)
                    continue
                if self.socket is not socket:
                    raise RealtimeError(self._expiry_reason or "Realtime session is disconnected",
                                        category="local_timeout" if self._expiry_reason else "local_shutdown") from None
                self._expiry_reason = reason
                await self.close()
                raise RealtimeError(reason, category="local_timeout") from None
            except ConnectionClosed as exc:
                category, code = _close_diagnostics(exc)
                if self.socket is None:
                    category = "local_timeout" if self._expiry_reason else "local_shutdown"
                    code = None
                message = ("Realtime session is disconnected" if category == "local_shutdown" else
                           self._expiry_reason or "Realtime connection closed unexpectedly")
                await self.close()
                self.tracker.state = ConversationState.ERROR
                raise RealtimeError(message, category=category, close_code=code) from None
            except OSError:
                category = ("local_timeout" if self._expiry_reason else "local_shutdown") if self.socket is None else "socket_error"
                message = ("Realtime session is disconnected" if category == "local_shutdown" else
                           self._expiry_reason or "Realtime connection closed unexpectedly")
                await self.close()
                self.tracker.state = ConversationState.ERROR
                raise RealtimeError(message, category=category) from None
            except WebSocketException:
                await self.close()
                self.tracker.state = ConversationState.ERROR
                raise RealtimeError("Realtime WebSocket protocol error", category="protocol_error") from None
        self.last_activity = self.clock()
        event = self.tracker.parse(raw, self.last_activity)
        self._activity.set()
        return event

    async def cancel_response(self, response_id: str | None = None) -> None:
        if response_id is not None and response_id not in self.tracker.response_started:
            raise RealtimeError("Response is not active")
        if response_id is None and not self.tracker.response_started:
            return
        payload = {"type": "response.cancel"}
        if response_id:
            payload["response_id"] = response_id
        await self.send(payload)

    async def close(self) -> None:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        socket, self.socket = self.socket, None
        self.tracker.state = ConversationState.STOPPING if socket else ConversationState.DISCONNECTED
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), timeout=3)
            except (TimeoutError, OSError):
                pass
        self.tracker.response_started.clear()
        self.tracker.state = ConversationState.DISCONNECTED
        self.started_at = self.last_activity = None

    async def __aenter__(self) -> "RealtimeClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
