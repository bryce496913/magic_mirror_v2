"""Current assistant exchange only; no audio, credentials, or transcript history."""

import asyncio
import fcntl
import json
import os
import socket
import stat
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .events import ConversationEvent, PlaybackAction, PlaybackEvent
from .wake import WakeState

SOCKET_PATH = Path(__file__).resolve().parents[2] / ".private" / "assistant-state.sock"
MAX_TEXT = 240


@dataclass(frozen=True)
class AssistantSnapshot:
    state: str = "disabled"
    user_text: str = ""
    user_final: bool = False
    assistant_text: str = ""
    assistant_final: bool = False
    playback: bool = False
    error: str = ""


class AssistantStateStore:
    """Single owner of display state; callbacks run on the wake event loop."""

    def __init__(self):
        self.state = WakeState.STOPPED
        self.user_text = ""
        self.user_final = False
        self.user_item = None
        self.assistant_text = ""
        self.assistant_final = False
        self.assistant_item = None
        self.response_id = None
        self.playback_active = False
        self.error = ""
        self._retired_user_items = deque(maxlen=16)

    def snapshot(self) -> AssistantSnapshot:
        return AssistantSnapshot(self.state.value, self.user_text, self.user_final,
                                 self.assistant_text, self.assistant_final,
                                 self.playback_active, self.error)

    def clear_exchange(self) -> None:
        self.user_text = self.assistant_text = ""
        self.user_final = self.assistant_final = False
        self.user_item = self.assistant_item = self.response_id = None
        self.playback_active = False

    def on_state(self, state: WakeState) -> None:
        self.state = state
        if state in {WakeState.IDLE, WakeState.STOPPED, WakeState.WAKE_DETECTED}:
            self.clear_exchange()
            self._retired_user_items.clear()
            self.error = ""
        elif state == WakeState.ERROR:
            self.clear_exchange()
            self.error = "Assistant recovering from an audio or network error"
        elif state == WakeState.RETURNING_IDLE:
            self.playback_active = False

    def on_event(self, event: ConversationEvent) -> None:
        if event.kind == "input_audio_buffer.speech_started":
            if self.user_item:
                self._retired_user_items.append(self.user_item)
            self.clear_exchange()
            self.user_item = event.item_id
        elif event.kind == "response.created" and event.response_id:
            self.response_id = event.response_id
            self.assistant_text = ""
            self.assistant_final = False
            self.assistant_item = None
        elif event.kind == "response.done" and event.response_id == self.response_id:
            if event.status != "completed":
                self.assistant_text = ""
                self.assistant_final = False
                self.playback_active = False
        if event.text is None or not event.item_id or event.role not in {"user", "assistant"}:
            return
        if event.role == "assistant" and not event.kind.startswith("response.output_audio_transcript."):
            return
        if event.role == "user":
            if event.item_id in self._retired_user_items:
                return
            if self.user_item and self.user_item != event.item_id:
                return
            self.user_item = event.item_id
            self.user_text = (event.text if event.final else self.user_text + event.text)[:MAX_TEXT]
            self.user_final = event.final
        elif event.response_id == self.response_id:
            if self.assistant_item and self.assistant_item != event.item_id:
                return
            self.assistant_item = event.item_id
            self.assistant_text = (event.text if event.final else self.assistant_text + event.text)[:MAX_TEXT]
            self.assistant_final = event.final

    def on_playback(self, event: PlaybackEvent) -> None:
        if event.response_id != self.response_id:
            return
        if event.action == PlaybackAction.STARTED:
            self.playback_active = True
        elif event.action == PlaybackAction.COMPLETED:
            if not self.playback_active:
                # A transcript without a successful local audio write was not spoken.
                self.assistant_text = ""
                self.assistant_final = False
            self.playback_active = False
        elif event.action == PlaybackAction.INTERRUPTED:
            self.playback_active = False
            self.assistant_text = ""
            self.assistant_final = False
            self.response_id = None


def acquire_owner_lock(path: Path = SOCKET_PATH) -> int:
    """Reserve the application account's one live assistant process."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise RuntimeError("Assistant state directory must be private (mode 0700)")
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise RuntimeError("Assistant state lock must be a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another assistant process already owns audio and LED") from None
        return fd
    except BaseException:
        os.close(fd)
        raise


class AssistantStateServer:
    """One local Unix socket owner; the OS lock rejects duplicate wake services."""

    def __init__(self, store: AssistantStateStore, path: Path = SOCKET_PATH):
        self.store = store
        self.path = Path(path)
        self.server = None
        self._lock_fd = None

    async def start(self) -> None:
        if self.server is not None:
            raise RuntimeError("Assistant state server is already running")
        fd = acquire_owner_lock(self.path)
        try:
            if self.path.exists():
                if not self.path.is_socket():
                    raise RuntimeError("Assistant state path is not a socket")
                self.path.unlink()  # stale socket only; exclusive owner lock held
            self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
            self.path.chmod(0o600)
            self._lock_fd = fd
        except BaseException:
            os.close(fd)
            raise

    async def _handle(self, _reader, writer) -> None:
        try:
            writer.write((json.dumps(asdict(self.store.snapshot())) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            self.path.unlink(missing_ok=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


def read_snapshot(path: Path = SOCKET_PATH) -> dict:
    """Bounded, read-only IPC for Flask; never start the wake service here."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        client.connect(str(path))
        raw = bytearray()
        while b"\n" not in raw and len(raw) <= 2048:
            chunk = client.recv(2048 - len(raw) + 1)
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 2048 or b"\n" not in raw:
            raise ValueError("Invalid assistant snapshot length")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("state") not in {state.value for state in WakeState}:
        raise ValueError("Invalid assistant snapshot")
    return value
