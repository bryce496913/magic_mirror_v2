"""Pass 6D state, IPC, local access, and LED integration without Pi hardware."""

import asyncio
import errno
import signal
import socket
import pytest

from magic_mirror import create_app
from magic_mirror.assistant.display import (AssistantStateServer, AssistantStateStore,
                                            acquire_owner_lock, read_snapshot)
from magic_mirror.assistant.events import (ConversationEvent, ConversationState, EventTracker,
                                           PlaybackAction, PlaybackEvent)
from magic_mirror.assistant.wake import WakeState
from magic_mirror.assistant.wake_cli import led_state_for
from magic_mirror.config import Settings


def event(kind, *, response=None, item=None, role=None, text=None, final=False, status=None):
    return ConversationEvent(kind, ConversationState.READY, "session", response, item,
                             role=role, text=text, final=final, status=status)


def test_current_exchange_correlates_transcripts_and_actual_playback():
    store = AssistantStateStore()
    store.on_state(WakeState.IDLE)
    assert store.snapshot().state == "idle"
    store.on_state(WakeState.WAKE_DETECTED)
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="user-1"))
    store.on_event(event("conversation.item.input_audio_transcription.delta", item="user-1",
                         role="user", text="What "))
    assert store.snapshot().user_text == "What " and not store.snapshot().user_final
    store.on_event(event("input_audio_buffer.speech_stopped", item="user-1"))
    store.on_state(WakeState.PROCESSING)
    store.on_event(event("conversation.item.input_audio_transcription.completed", item="user-1",
                         role="user", text="What time?", final=True))
    store.on_event(event("response.created", response="response-1"))
    store.on_state(WakeState.RESPONDING)
    store.on_event(event("response.output_audio_transcript.delta", response="other",
                         item="wrong", role="assistant", text="Unrelated"))
    store.on_event(event("response.output_text.done", response="response-1",
                         item="unspoken", role="assistant", text="Not spoken", final=True))
    assert not store.snapshot().assistant_text
    store.on_event(event("response.output_audio_transcript.delta", response="response-1",
                         item="answer-1", role="assistant", text="It is "))
    store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "response-1"))
    store.on_event(event("response.output_audio_transcript.done", response="response-1",
                         item="answer-1", role="assistant", text="It is noon.", final=True))
    store.on_event(event("response.done", response="response-1", status="completed"))
    snap = store.snapshot()
    assert (snap.user_text, snap.user_final, snap.assistant_text, snap.assistant_final,
            snap.playback) == ("What time?", True, "It is noon.", True, True)
    store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "response-1"))
    store.on_state(WakeState.LISTENING)
    assert not store.snapshot().playback
    store.on_state(WakeState.RETURNING_IDLE)
    store.on_state(WakeState.IDLE)
    assert not store.snapshot().user_text and not store.snapshot().assistant_text


@pytest.mark.parametrize("completion_order", ["before_playback", "during_playback", "after_playback"])
def test_tool_continuation_can_display_completed_audio_transcript_from_response_done(completion_order):
    store = AssistantStateStore()
    tracker = EventTracker(session_id="session", state=ConversationState.READY)
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="user"))
    store.on_event(event("conversation.item.input_audio_transcription.completed", item="user",
                         role="user", text="What can you do?", final=True))
    store.on_event(event("response.created", response="tool-response"))
    store.on_event(event("response.done", response="tool-response", status="completed"))
    store.on_event(event("response.created", response="spoken-response"))
    if completion_order != "before_playback":
        store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken-response"))
    if completion_order == "after_playback":
        store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "spoken-response"))
    completed = tracker.parse({"type": "response.done", "response": {
        "id": "spoken-response", "status": "completed", "output": [{
            "id": "spoken-item", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_audio", "transcript": "I can talk with you."}]}]}}, 1)
    store.on_event(completed)
    if completion_order == "before_playback":
        assert store.snapshot().assistant_text == ""
        store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken-response"))
    if completion_order != "after_playback":
        store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "spoken-response"))
    store.on_event(event("response.output_audio_transcript.delta", response="spoken-response",
                         item="spoken-item", role="assistant", text="Late duplicate"))
    store.on_state(WakeState.LISTENING)
    snap = store.snapshot()
    assert (snap.user_text, snap.assistant_text, snap.assistant_final, snap.playback) == (
        "What can you do?", "I can talk with you.", True, False)


@pytest.mark.parametrize("transcript_after_playback", [False, True])
def test_streaming_audio_transcript_before_or_after_playback_completion(transcript_after_playback):
    store = AssistantStateStore()
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="u"))
    store.on_event(event("response.created", response="spoken"))
    if transcript_after_playback:
        store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken"))
        store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "spoken"))
    store.on_event(event("response.output_audio_transcript.delta", response="spoken",
                         item="a", role="assistant", text="I can "))
    store.on_event(event("response.output_audio_transcript.done", response="spoken",
                         item="a", role="assistant", text="I can help.", final=True))
    if not transcript_after_playback:
        assert store.snapshot().assistant_text == ""  # no audio was heard yet
        store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken"))
        store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "spoken"))
    store.on_state(WakeState.LISTENING)
    assert store.snapshot().assistant_text == "I can help."
    assert store.snapshot().assistant_final and not store.snapshot().playback


def test_late_transcript_cannot_resurrect_unplayed_cancelled_or_sleeping_answer():
    store = AssistantStateStore()
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="u"))
    store.on_event(event("response.created", response="r1"))
    store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "r1"))
    store.on_event(event("response.output_audio_transcript.done", response="r1",
                         item="a1", role="assistant", text="Unheard", final=True))
    assert not store.snapshot().assistant_text
    store.on_event(event("response.created", response="r2"))
    store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "r2"))
    store.on_event(event("response.output_audio_transcript.done", response="r2",
                         item="a2", role="assistant", text="Interrupted", final=True))
    store.on_playback(PlaybackEvent(PlaybackAction.INTERRUPTED, "r2"))
    store.on_event(event("response.output_audio_transcript.done", response="r2",
                         item="a2", role="assistant", text="Stale", final=True))
    assert not store.snapshot().assistant_text
    store.on_state(WakeState.IDLE)
    assert not store.snapshot().user_text and not store.snapshot().assistant_text


def test_cancelled_tool_continuation_cannot_restore_response_done_audio_text():
    store = AssistantStateStore()
    tracker = EventTracker(session_id="session", state=ConversationState.READY)
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="u1"))
    store.on_event(event("response.created", response="tool"))
    store.on_event(event("response.done", response="tool", status="completed"))
    store.on_event(event("response.created", response="spoken"))
    store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken"))
    store.on_playback(PlaybackEvent(PlaybackAction.INTERRUPTED, "spoken"))
    late = tracker.parse({"type": "response.done", "response": {
        "id": "spoken", "status": "completed", "output": [{"id": "a", "type": "message",
            "role": "assistant", "content": [{"type": "output_audio", "transcript": "Old answer"}]}]}}, 1)
    store.on_event(late)
    assert not store.snapshot().assistant_text
    store.on_event(event("input_audio_buffer.speech_started", item="u2"))
    store.on_event(event("response.created", response="next"))
    store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "next"))
    store.on_event(late)
    assert not store.snapshot().assistant_text
    store.on_state(WakeState.IDLE)
    assert not store.snapshot().assistant_text


def test_tool_response_and_unspoken_output_text_never_become_dashboard_answer():
    store = AssistantStateStore()
    tracker = EventTracker(session_id="session", state=ConversationState.READY)
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="u"))
    store.on_event(event("response.created", response="tool"))
    tool = tracker.parse({"type": "response.done", "response": {
        "id": "tool", "status": "completed", "output": [{"type": "function_call", "id": "i",
            "call_id": "c", "name": "get_mirror_capabilities", "arguments": '{"hidden":"private"}'}]}}, 1)
    store.on_event(tool)
    assert not store.snapshot().assistant_text
    store.on_event(event("response.created", response="spoken"))
    store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken"))
    unspoken = tracker.parse({"type": "response.done", "response": {
        "id": "spoken", "status": "completed", "output": [{"type": "message", "role": "assistant",
            "id": "text", "content": [{"type": "output_text", "text": "Internal only"}]}]}}, 2)
    store.on_event(unspoken)
    assert not store.snapshot().assistant_text


def test_interrupt_failure_late_events_and_restart_cannot_leave_stale_answer():
    store = AssistantStateStore()
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="u1"))
    store.on_event(event("conversation.item.input_audio_transcription.completed", item="u1",
                         role="user", text="First?", final=True))
    store.on_event(event("response.created", response="r1"))
    store.on_event(event("response.output_audio_transcript.done", response="r1", item="a1",
                         role="assistant", text="First answer", final=True))
    store.on_playback(PlaybackEvent(PlaybackAction.INTERRUPTED, "r1"))
    store.on_event(event("input_audio_buffer.speech_started", item="u2"))
    store.on_event(event("conversation.item.input_audio_transcription.completed", item="u1",
                         role="user", text="Late first", final=True))
    store.on_event(event("response.output_audio_transcript.done", response="r1", item="a1",
                         role="assistant", text="Late answer", final=True))
    assert not store.snapshot().user_text and not store.snapshot().assistant_text
    store.on_event(event("response.created", response="r2"))
    store.on_event(event("response.output_audio_transcript.delta", response="r2", item="a2",
                         role="assistant", text="Partial"))
    store.on_event(event("response.done", response="r2", status="failed"))
    assert not store.snapshot().assistant_text
    store.on_state(WakeState.ERROR)
    assert store.snapshot().error and not store.snapshot().playback
    store.on_state(WakeState.IDLE)
    assert not store.snapshot().error
    store.on_state(WakeState.STOPPED)
    assert store.snapshot().state == "stopped"


def test_transcript_without_audio_is_not_presented_as_spoken():
    store = AssistantStateStore()
    store.on_event(event("response.created", response="r"))
    store.on_event(event("response.output_audio_transcript.done", response="r", item="a",
                         role="assistant", text="Never heard", final=True))
    store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "r"))
    assert not store.snapshot().assistant_text


def test_private_socket_snapshot_refresh_single_owner_and_restart(tmp_path):
    async def scenario():
        path = tmp_path / "private" / "state.sock"
        store = AssistantStateStore()
        server = AssistantStateServer(store, path)
        await server.start()
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert (await asyncio.to_thread(read_snapshot, path))["state"] == "stopped"
        other = AssistantStateServer(AssistantStateStore(), path)
        with pytest.raises(RuntimeError, match="Another assistant process"):
            await other.start()
        store.on_state(WakeState.IDLE)
        assert (await asyncio.to_thread(read_snapshot, path))["state"] == "idle"
        await server.close()
        assert not path.exists()
        with pytest.raises(OSError):
            read_snapshot(path)
        await other.start()
        await other.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage,error", [
    ("write", BrokenPipeError(errno.EPIPE, "Broken pipe")),
    ("drain", ConnectionResetError("Connection lost")),
    ("drain", BrokenPipeError(errno.EPIPE, "Broken pipe")),
    ("drain", OSError(errno.ENOTCONN, "Transport endpoint is not connected")),
    ("wait_closed", BrokenPipeError(errno.EPIPE, "Broken pipe")),
    ("wait_closed", ConnectionResetError("Connection lost")),
    ("close", BrokenPipeError(errno.EPIPE, "Broken pipe")),
])
def test_snapshot_client_disconnect_during_write_or_close_is_clean(stage, error):
    class Transport:
        aborted = False
        def abort(self):
            self.aborted = True

    class Writer:
        def __init__(self):
            self.transport = Transport()
            self.closed = False
            self.data = None
        def write(self, data):
            self.data = data
            if stage == "write":
                raise error
        async def drain(self):
            if stage == "drain":
                raise error
        def close(self):
            self.closed = True
            if stage == "close":
                raise error
        async def wait_closed(self):
            if stage == "wait_closed":
                raise error

    async def scenario():
        writer = Writer()
        await AssistantStateServer(AssistantStateStore())._handle(None, writer)
        assert writer.closed and writer.data.endswith(b"\n")
        assert writer.transport.aborted == (stage == "close")
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["drain", "wait_closed"])
def test_snapshot_handler_bounds_slow_clients_without_blocking_other_work(monkeypatch, stage):
    monkeypatch.setattr("magic_mirror.assistant.display.CLIENT_IO_TIMEOUT", 0.01)

    class Writer:
        def __init__(self):
            self.transport = self
            self.aborted = False
            self.closed = False
        def write(self, data):
            pass
        async def drain(self):
            if stage == "drain":
                await asyncio.Event().wait()
        def abort(self):
            self.aborted = True
        def close(self):
            self.closed = True
        async def wait_closed(self):
            if stage == "wait_closed":
                await asyncio.Event().wait()

    async def scenario():
        writer = Writer()
        handler = asyncio.create_task(AssistantStateServer(AssistantStateStore())._handle(None, writer))
        await asyncio.sleep(0)  # audio and wake tasks remain schedulable during client I/O
        assert not handler.done()
        await asyncio.wait_for(handler, 0.2)
        assert writer.closed and writer.aborted
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["write", "drain", "wait_closed"])
def test_unexpected_snapshot_handler_errors_still_surface_and_close_writer(stage):
    class Writer:
        closed = False
        def write(self, data):
            if stage == "write":
                raise OSError(errno.EACCES, "unexpected socket error")
        async def drain(self):
            if stage == "drain":
                raise OSError(errno.EACCES, "unexpected socket error")
        def close(self):
            self.closed = True
        async def wait_closed(self):
            if stage == "wait_closed":
                raise OSError(errno.EACCES, "unexpected socket error")

    async def scenario():
        writer = Writer()
        with pytest.raises(OSError) as caught:
            await AssistantStateServer(AssistantStateStore())._handle(None, writer)
        assert caught.value.errno == errno.EACCES and writer.closed
    asyncio.run(scenario())


def test_snapshot_programming_error_still_surfaces_after_writer_cleanup():
    class BadStore:
        def snapshot(self):
            raise RuntimeError("snapshot bug")

    class Writer:
        closed = False
        def write(self, data):
            raise AssertionError("snapshot failed before write")
        def close(self):
            self.closed = True
        async def wait_closed(self):
            pass

    async def scenario():
        writer = Writer()
        with pytest.raises(RuntimeError, match="snapshot bug"):
            await AssistantStateServer(BadStore())._handle(None, writer)
        assert writer.closed
    asyncio.run(scenario())


def test_snapshot_socket_repeated_disconnect_poll_and_reconnect(tmp_path):
    async def scenario():
        path = tmp_path / "private" / "state.sock"
        store = AssistantStateStore()
        store.on_state(WakeState.IDLE)
        server = AssistantStateServer(store, path)
        loop = asyncio.get_running_loop()
        errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        await server.start()
        try:
            def disconnect_and_poll():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(path))
                return read_snapshot(path)

            for _ in range(30):
                assert (await asyncio.to_thread(disconnect_and_poll))["state"] == "idle"
            store.on_state(WakeState.LISTENING)
            assert (await asyncio.to_thread(read_snapshot, path))["state"] == "listening"
            assert path.stat().st_mode & 0o777 == 0o600
            other = AssistantStateServer(AssistantStateStore(), path)
            with pytest.raises(RuntimeError, match="Another assistant process"):
                await other.start()
        finally:
            await server.close()
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)
        assert not errors
    asyncio.run(scenario())


def test_manual_voice_cannot_start_alongside_wake_owner(tmp_path, monkeypatch):
    import os
    from magic_mirror.assistant import voice_cli
    path = tmp_path / "private" / "state.sock"
    fd = acquire_owner_lock(path)
    try:
        with pytest.raises(RuntimeError, match="Another assistant process"):
            acquire_owner_lock(path)
        monkeypatch.setattr(voice_cli, "acquire_owner_lock", lambda: acquire_owner_lock(path))
        monkeypatch.setattr(voice_cli, "VoiceConversation", lambda *a, **k: pytest.fail("Voice opened while wake active"))
        with pytest.raises(RuntimeError, match="Another assistant process"):
            asyncio.run(voice_cli._interactive(Settings()))
    finally:
        os.close(fd)
    second = acquire_owner_lock(path)
    os.close(second)


def test_dashboard_state_endpoint_is_loopback_only_and_read_only(monkeypatch):
    app = create_app(Settings(wake_enabled=True))
    monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot",
                        lambda: {"state": "responding", "user_text": "Question",
                                 "assistant_text": "Actual response", "playback": True})
    client = app.test_client()
    result = client.get("/api/assistant")
    assert result.status_code == 200 and result.json["assistant_text"] == "Actual response"
    assert result.headers["Cache-Control"] == "no-store, private"
    assert client.get("/api/assistant", environ_overrides={"REMOTE_ADDR": "192.168.1.2"}).status_code == 403
    assert client.get("/api/assistant", headers={"Host": "mirror.example"}).status_code == 403
    assert client.get("/api/assistant", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/assistant").status_code == 405
    body = client.get("/").data
    assert b'data-role="user-transcript"' in body
    assert b'data-role="assistant-transcript"' in body
    assert client.get("/dashboard-assets/assistant.js").status_code == 200
    assert client.get("/health").json == {"status": "healthy"}

    monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    unavailable = client.get("/api/assistant")
    assert unavailable.json == {"state": "unavailable"}
    assert unavailable.headers["X-Mirror-Assistant-Read-Status"] == "socket_missing"
    assert create_app(Settings()).test_client().get("/api/assistant").json == {"state": "disabled"}


@pytest.mark.parametrize("error,reason", [(PermissionError(), "socket_denied"),
                                          (TimeoutError(), "socket_timeout"),
                                          (ConnectionRefusedError(), "socket_refused"),
                                          (ValueError(), "invalid_snapshot")])
def test_unavailable_snapshot_reports_only_sanitized_local_read_reason(monkeypatch, error, reason):
    app = create_app(Settings(wake_enabled=True))

    def fail():
        raise error

    monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot", fail)
    response = app.test_client().get("/api/assistant")
    assert response.json == {"state": "unavailable"}
    assert response.headers["X-Mirror-Assistant-Read-Status"] == reason


def test_browser_refresh_reads_live_owner_snapshot(monkeypatch, tmp_path):
    async def scenario():
        path = tmp_path / "private" / "state.sock"
        store = AssistantStateStore()
        server = AssistantStateServer(store, path)
        monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot", lambda: read_snapshot(path))
        app = create_app(Settings(wake_enabled=True))
        await server.start()
        try:
            store.on_state(WakeState.IDLE)
            first = await asyncio.to_thread(lambda: app.test_client().get("/api/assistant"))
            assert first.json["state"] == "idle"
            store.on_state(WakeState.LISTENING)
            store.on_event(event("input_audio_buffer.speech_started", item="u"))
            store.on_event(event("conversation.item.input_audio_transcription.completed",
                                 item="u", role="user", text="Current question", final=True))
            refreshed = await asyncio.to_thread(lambda: app.test_client().get("/api/assistant"))
            assert refreshed.json["user_text"] == "Current question"
            assert refreshed.json["state"] == "listening"
        finally:
            await server.close()
        assert app.test_client().get("/api/assistant").json == {"state": "unavailable"}
    asyncio.run(scenario())


def test_dashboard_refresh_during_tool_call_and_after_spoken_reply(monkeypatch, tmp_path):
    async def scenario():
        path = tmp_path / "private" / "state.sock"
        store = AssistantStateStore()
        server = AssistantStateServer(store, path)
        monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot", lambda: read_snapshot(path))
        app = create_app(Settings(wake_enabled=True))
        await server.start()
        try:
            store.on_state(WakeState.LISTENING)
            store.on_event(event("input_audio_buffer.speech_started", item="u"))
            store.on_event(event("conversation.item.input_audio_transcription.completed",
                                 item="u", role="user", text="Capabilities?", final=True))
            store.on_event(event("response.created", response="tool-response"))
            during = await asyncio.to_thread(lambda: app.test_client().get("/api/assistant"))
            assert during.status_code == 200 and during.json["user_text"] == "Capabilities?"
            assert during.json["assistant_text"] == ""
            store.on_event(event("response.done", response="tool-response", status="completed"))
            store.on_event(event("response.created", response="spoken-response"))
            store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "spoken-response"))
            tracker = EventTracker(session_id="session", state=ConversationState.READY)
            store.on_event(tracker.parse({"type": "response.done", "response": {
                "id": "spoken-response", "status": "completed", "output": [{
                    "id": "spoken-item", "type": "message", "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": "I can answer questions."}]}]}}, 1))
            store.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "spoken-response"))
            store.on_state(WakeState.LISTENING)
            for _ in range(2):  # a fresh browser read must restore the current exchange
                refreshed = await asyncio.to_thread(lambda: app.test_client().get("/api/assistant"))
                assert refreshed.status_code == 200
                assert refreshed.json["assistant_text"] == "I can answer questions."
                assert refreshed.json["assistant_final"] is True
                assert refreshed.json["state"] == "listening"
            store.on_state(WakeState.IDLE)
            idle = await asyncio.to_thread(lambda: app.test_client().get("/api/assistant"))
            assert idle.json["assistant_text"] == ""
        finally:
            await server.close()

    asyncio.run(scenario())


def test_private_snapshot_remains_readable_with_bounded_unicode_transcripts(tmp_path):
    async def scenario():
        path = tmp_path / "private" / "state.sock"
        store = AssistantStateStore()
        server = AssistantStateServer(store, path)
        store.on_state(WakeState.LISTENING)
        store.on_event(event("input_audio_buffer.speech_started", item="u"))
        store.on_event(event("conversation.item.input_audio_transcription.completed", item="u",
                             role="user", text="🔊" * 240, final=True))
        store.on_event(event("response.created", response="r"))
        store.on_playback(PlaybackEvent(PlaybackAction.STARTED, "r"))
        store.on_event(event("response.output_audio_transcript.done", response="r", item="a",
                             role="assistant", text="🔊" * 240, final=True))
        await server.start()
        try:
            snapshot = await asyncio.to_thread(read_snapshot, path)
            assert snapshot["user_text"] == "🔊" * 240
            assert snapshot["assistant_text"] == "🔊" * 240
        finally:
            await server.close()

    asyncio.run(scenario())


def test_led_mapping_follows_wake_and_playback_states():
    assert led_state_for(WakeState.IDLE) == "idle"
    assert led_state_for(WakeState.WAKE_DETECTED) == "idle"
    assert led_state_for(WakeState.LISTENING) == "listening"
    assert led_state_for(WakeState.PROCESSING) == "responding"
    assert led_state_for(WakeState.RESPONDING) == "responding"
    for state in (WakeState.RETURNING_IDLE, WakeState.ERROR, WakeState.STOPPED):
        assert led_state_for(state) == "off"


def test_flask_does_not_claim_gpio_when_wake_owns_it(monkeypatch):
    from magic_mirror import __main__ as entry

    class FakeData:
        def __init__(self, settings):
            self.settings = settings
        def start(self):
            pass
        def stop(self):
            return True

    class FakeAudio:
        def __init__(self, settings):
            pass
        def close(self):
            pass

    class FakeApp:
        def run(self, **kwargs):
            assert kwargs["use_reloader"] is False

    monkeypatch.setattr(entry.Settings, "from_env", lambda: Settings(wake_enabled=True, led_enabled=True))
    monkeypatch.setattr(entry, "DashboardData", FakeData)
    monkeypatch.setattr(entry, "AudioManager", FakeAudio)
    monkeypatch.setattr(entry, "RGBLedController", lambda _: pytest.fail("Flask claimed assistant GPIO"))
    monkeypatch.setattr(entry, "create_app", lambda *args, **kwargs: FakeApp())
    monkeypatch.setattr(entry.signal, "signal", lambda *args: None)
    assert entry.main() == 0


def test_disabled_assistant_keeps_flask_led_off(monkeypatch):
    from magic_mirror import __main__ as entry
    actions = []

    class FakeData:
        def __init__(self, settings):
            pass
        def start(self):
            pass
        def stop(self):
            return True

    class FakeAudio:
        def __init__(self, settings):
            pass
        def close(self):
            pass

    class FakeLed:
        def __init__(self, settings):
            assert settings.led_initial_state == "off"
        def open(self):
            actions.append("open")
        def close(self):
            actions.append("close")

    class FakeApp:
        def run(self, **kwargs):
            pass

    monkeypatch.setattr(entry.Settings, "from_env",
                        lambda: Settings(wake_enabled=False, led_enabled=True, led_initial_state="idle"))
    monkeypatch.setattr(entry, "DashboardData", FakeData)
    monkeypatch.setattr(entry, "AudioManager", FakeAudio)
    monkeypatch.setattr(entry, "RGBLedController", FakeLed)
    monkeypatch.setattr(entry, "create_app", lambda *args, **kwargs: FakeApp())
    monkeypatch.setattr(entry.signal, "signal", lambda *args: None)
    assert entry.main() == 0
    assert actions == ["open", "close"]


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_wake_cli_owns_led_and_snapshot_until_clean_shutdown(monkeypatch, tmp_path, stop_signal):
    from magic_mirror.assistant import wake_cli

    async def scenario():
        path = tmp_path / "private" / "state.sock"
        ready, finish = asyncio.Event(), asyncio.Event()
        led_actions = []
        handlers = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda name, callback: handlers.__setitem__(name, callback))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda name: handlers.pop(name, None))

        class FakeLed:
            def __init__(self, settings):
                assert settings.led_initial_state == "off"
            def open(self):
                led_actions.append("open")
            def set_state(self, state):
                led_actions.append(state)
            def close(self):
                led_actions.append("close")

        class FakeController:
            def __init__(self, settings, *, on_state, on_voice_event, on_playback, **kwargs):
                self.on_state = on_state
                self.on_voice_event = on_voice_event
                self.on_playback = on_playback
            async def run(self):
                self.on_state(WakeState.IDLE)
                ready.set()
                await finish.wait()
                self.on_state(WakeState.WAKE_DETECTED)
                self.on_state(WakeState.LISTENING)
                self.on_voice_event(event("input_audio_buffer.speech_started", item="u"))
                self.on_voice_event(event("conversation.item.input_audio_transcription.completed",
                                          item="u", role="user", text="Hello?", final=True))
                self.on_state(WakeState.PROCESSING)
                self.on_voice_event(event("response.created", response="r"))
                self.on_state(WakeState.RESPONDING)
                self.on_voice_event(event("response.output_audio_transcript.done", response="r",
                                          item="a", role="assistant", text="Hello!", final=True))
                self.on_playback(PlaybackEvent(PlaybackAction.STARTED, "r"))
                self.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, "r"))
                self.on_state(WakeState.LISTENING)
                self.on_state(WakeState.RETURNING_IDLE)
                self.on_state(WakeState.IDLE)
                self.on_state(WakeState.STOPPED)
            def stop(self):
                finish.set()
            def end_conversation(self):
                finish.set()

        monkeypatch.setattr(wake_cli, "RGBLedController", FakeLed)
        monkeypatch.setattr(wake_cli, "WakeController", FakeController)
        monkeypatch.setattr(wake_cli, "AssistantStateServer",
                            lambda store: AssistantStateServer(store, path))
        task = asyncio.create_task(wake_cli._interactive(Settings(wake_enabled=True, led_enabled=True)))
        await asyncio.wait_for(ready.wait(), 1)
        assert (await asyncio.to_thread(read_snapshot, path))["state"] == "idle"
        handlers[stop_signal]()
        await task
        assert led_actions == ["open", "idle", "idle", "listening", "responding",
                               "responding", "listening", "off", "idle", "off", "close"]
        assert not path.exists()
    asyncio.run(scenario())
