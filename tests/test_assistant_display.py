"""Pass 6D state, IPC, local access, and LED integration without Pi hardware."""

import asyncio
import signal
import pytest

from magic_mirror import create_app
from magic_mirror.assistant.display import (AssistantStateServer, AssistantStateStore,
                                            acquire_owner_lock, read_snapshot)
from magic_mirror.assistant.events import (ConversationEvent, ConversationState,
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
    assert client.get("/api/assistant").json == {"state": "unavailable"}
    assert create_app(Settings()).test_client().get("/api/assistant").json == {"state": "disabled"}


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
