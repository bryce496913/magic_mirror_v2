"""Current assistant exchange only; no audio, credentials, or transcript history."""

import asyncio
import errno
import fcntl
import json
import os
import socket
import stat
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .events import AudioTranscript, ConversationEvent, PlaybackAction, PlaybackEvent
from .wake import WakeState
from .tools.search import _public_url

SOCKET_PATH = Path(__file__).resolve().parents[2] / ".private" / "assistant-state.sock"
MAX_TEXT = 240
MAX_SNAPSHOT_BYTES = 8192
CLIENT_IO_TIMEOUT = 0.5
CLIENT_DISCONNECT_ERRNOS = {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ENOTCONN}


def _client_disconnected(exc: OSError) -> bool:
    return (isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)) or
            exc.errno in CLIENT_DISCONNECT_ERRNOS)


@dataclass(frozen=True)
class AssistantSnapshot:
    state: str = "disabled"
    user_text: str = ""
    user_final: bool = False
    assistant_text: str = ""
    assistant_final: bool = False
    playback: bool = False
    error: str = ""
    sources: tuple[dict[str, str], ...] = ()


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
        self._audio_played = False
        self._playback_finished = False
        self._final_from_response_done = False
        self.error = ""
        self.sources: tuple[dict[str, str], ...] = ()
        self._retired_user_items = deque(maxlen=16)

    def snapshot(self) -> AssistantSnapshot:
        return AssistantSnapshot(self.state.value, self.user_text, self.user_final,
                                 self.assistant_text if self._audio_played else "",
                                 self.assistant_final if self._audio_played else False,
                                 self.playback_active, self.error,
                                 tuple(dict(source) for source in self.sources))

    def clear_exchange(self) -> None:
        self.user_text = self.assistant_text = ""
        self.user_final = self.assistant_final = False
        self.user_item = self.assistant_item = self.response_id = None
        self.playback_active = False
        self.sources = ()
        self._audio_played = False
        self._playback_finished = False
        self._final_from_response_done = False

    def on_sources(self, sources: tuple[dict[str, str], ...]) -> None:
        """Publish cited links for this exchange only; no raw search text is stored."""
        accepted = []
        for item in sources[:3]:
            if not isinstance(item, dict):
                continue
            url = _public_url(item.get("url"))
            title = item.get("title")
            if url and isinstance(title, str) and title.strip():
                accepted.append({"title": title.strip()[:80], "url": url})
        self.sources = tuple(accepted)

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
            self._audio_played = False
            self._playback_finished = False
            self._final_from_response_done = False
        elif event.kind == "response.done" and event.response_id == self.response_id:
            if event.status != "completed":
                self.assistant_text = ""
                self.assistant_final = False
                self.playback_active = False
                self.sources = ()
                self.response_id = None
                self._audio_played = False
                self._playback_finished = True
            elif not self._playback_finished or self._audio_played:
                # response.done carries the completed audio transcript even if
                # separate streaming transcript events were absent or late.
                for transcript in event.audio_transcripts:
                    if (isinstance(transcript, AudioTranscript) and
                            isinstance(transcript.item_id, str) and transcript.item_id and
                            isinstance(transcript.text, str) and transcript.text and
                            (self.assistant_item is None or self.assistant_item == transcript.item_id)):
                        self.assistant_item = transcript.item_id
                        self.assistant_text = transcript.text[:MAX_TEXT]
                        self.assistant_final = True
                        self._final_from_response_done = True
                        break
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
            if (self._playback_finished and not self._audio_played) or self._final_from_response_done:
                return
            if self.assistant_item and self.assistant_item != event.item_id:
                return
            self.assistant_item = event.item_id
            self.assistant_text = (event.text if event.final else self.assistant_text + event.text)[:MAX_TEXT]
            self.assistant_final = event.final

    def on_playback(self, event: PlaybackEvent) -> None:
        if event.response_id != self.response_id:
            return
        if event.action == PlaybackAction.STARTED:
            self._audio_played = True
            self.playback_active = True
        elif event.action == PlaybackAction.COMPLETED:
            if not self._audio_played:
                # A transcript without a successful local audio write was not spoken.
                self.assistant_text = ""
                self.assistant_final = False
                self.sources = ()
            self.playback_active = False
            self._playback_finished = True
        elif event.action == PlaybackAction.INTERRUPTED:
            self.playback_active = False
            self.assistant_text = ""
            self.assistant_final = False
            self.response_id = None
            self.sources = ()
            self._audio_played = False
            self._playback_finished = True


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
            try:
                raw = (json.dumps(asdict(self.store.snapshot())) + "\n").encode()
                if len(raw) > MAX_SNAPSHOT_BYTES:
                    raw = b'{"state":"error","error":"Assistant display unavailable"}\n'
                writer.write(raw)
                await asyncio.wait_for(writer.drain(), CLIENT_IO_TIMEOUT)
            except TimeoutError:
                writer.transport.abort()  # a stalled local reader must not hold a handler open
            except OSError as exc:
                if not _client_disconnected(exc):
                    raise
        finally:
            try:
                writer.close()
            except OSError as exc:
                if not _client_disconnected(exc):
                    raise
                writer.transport.abort()
            else:
                try:
                    await asyncio.wait_for(writer.wait_closed(), CLIENT_IO_TIMEOUT)
                except TimeoutError:
                    writer.transport.abort()
                except OSError as exc:
                    if not _client_disconnected(exc):
                        raise

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
        while b"\n" not in raw and len(raw) <= MAX_SNAPSHOT_BYTES:
            chunk = client.recv(MAX_SNAPSHOT_BYTES - len(raw) + 1)
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_SNAPSHOT_BYTES or b"\n" not in raw:
            raise ValueError("Invalid assistant snapshot length")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("state") not in {state.value for state in WakeState}:
        raise ValueError("Invalid assistant snapshot")
    return value
