"""No network, hardware, or credentials are needed for Realtime tests."""

import asyncio
import json

import pytest

from magic_mirror.assistant.client import RealtimeClient, RealtimeError
from magic_mirror.assistant.events import ConversationState, EventTracker
from magic_mirror.assistant.realtime_test import main as cli_main
from magic_mirror.config import ConfigurationError, Settings
from magic_mirror.app import create_app


def run(coro):
    return asyncio.run(coro)


class FakeSocket:
    def __init__(self, *events):
        self.events = list(events)
        self.sent = []
        self.closed = False

    async def recv(self):
        if not self.events:
            raise OSError("closed")
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        self.closed = True


CREATED = {"type": "session.created", "session": {"id": "sess_test"}}
UPDATED = {"type": "session.updated", "session": {"id": "sess_test"}}


def client_with(socket, **settings):
    calls = []

    async def connector(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    client = RealtimeClient(Settings(openai_api_key="test-only-key", **settings), connector=connector)
    return client, calls


def test_missing_key_never_connects():
    async def scenario():
        client = RealtimeClient(Settings())
        with pytest.raises(RealtimeError, match="not configured"):
            await client.connect()
    run(scenario())


def test_connect_ack_settings_and_close():
    async def scenario():
        socket = FakeSocket(CREATED, UPDATED)
        client, calls = client_with(socket)
        ack = await client.connect()
        assert ack.session_id == client.session_id == "sess_test"
        assert client.state == ConversationState.READY
        assert calls[0][0].endswith("model=gpt-realtime-2.1-mini")
        assert calls[0][1]["additional_headers"]["Authorization"] == "Bearer test-only-key"
        assert socket.sent[0]["type"] == "session.update"
        assert socket.sent[0]["session"]["model"] == "gpt-realtime-2.1-mini"
        assert socket.sent[0]["session"]["output_modalities"] == ["text"]
        assert socket.sent[0]["session"]["audio"]["input"]["format"]["rate"] == 24000
        await client.close()
        await client.close()
        assert socket.closed and client.state == ConversationState.DISCONNECTED
    run(scenario())


def test_custom_model_and_validation():
    assert Settings.from_env({"MIRROR_REALTIME_MODEL": "gpt-realtime-mini"}).realtime_model == "gpt-realtime-mini"
    for values in ({"MIRROR_REALTIME_MODEL": ""},
                   {"MIRROR_REALTIME_MAX_SESSION_SECONDS": "3601"},
                   {"MIRROR_REALTIME_IDLE_SECONDS": "901"},
                   {"MIRROR_REALTIME_RETRY_LIMIT": "-1"},
                   {"MIRROR_REALTIME_MAX_RESPONSE_SECONDS": "901"}):
        with pytest.raises(ConfigurationError):
            Settings.from_env(values)
    assert "test-only-key" not in repr(Settings(openai_api_key="test-only-key"))


def test_connection_timeout_and_retry_limit():
    async def scenario():
        calls = []
        delays = []

        async def connector(*args, **kwargs):
            calls.append(1)
            raise TimeoutError()

        async def sleep(delay):
            delays.append(delay)

        client = RealtimeClient(Settings(openai_api_key="key", realtime_retry_limit=2),
                                connector=connector, sleep=sleep)
        with pytest.raises(RealtimeError, match="failed or timed out"):
            await client.connect()
        assert len(calls) == 3
        assert delays == [1.0, 2.0]
        assert client.state == ConversationState.DISCONNECTED
    run(scenario())


def test_auth_failure_does_not_retry():
    from websockets.exceptions import InvalidStatus

    class Response:
        status_code = 401

    async def scenario():
        calls = []

        async def connector(*args, **kwargs):
            calls.append(1)
            raise InvalidStatus(Response())

        client = RealtimeClient(Settings(openai_api_key="secret"), connector=connector)
        with pytest.raises(RealtimeError, match="authentication failed") as exc:
            await client.connect()
        assert "secret" not in str(exc.value)
        assert len(calls) == 1
    run(scenario())


def test_bad_ack_and_server_error_close_socket():
    async def scenario():
        for events in [(CREATED, {"type": "error", "error": {"code": "invalid_model"}}),
                       (CREATED, {"type": "session.updated", "session": {"id": "other"}}),
                       ({"type": "session.created", "session": {}},)]:
            socket = FakeSocket(*events)
            client, _ = client_with(socket)
            with pytest.raises(RealtimeError):
                await client.connect()
            assert socket.closed
    run(scenario())


def test_ack_timeout_and_repeated_start_stop():
    class HangingSocket(FakeSocket):
        async def recv(self):
            await asyncio.Future()

    async def scenario():
        socket = HangingSocket()
        client, _ = client_with(socket, realtime_connect_timeout=1)
        with pytest.raises(RealtimeError, match="Timed out waiting for session.created"):
            await client.connect()
        assert socket.closed
        good = FakeSocket(CREATED, UPDATED)

        async def connector(*args, **kwargs):
            return good

        client.connector = connector
        await client.connect()
        with pytest.raises(RealtimeError, match="already connected"):
            await client.connect()
        await client.close()
        assert good.closed
    run(scenario())


def test_event_ordering_and_transcript_final_replaces_delta():
    tracker = EventTracker(state=ConversationState.READY, session_id="sess_test")
    speech = tracker.parse({"type": "input_audio_buffer.speech_started", "item_id": "user1"}, 0)
    assert speech.state == ConversationState.LISTENING
    delta = tracker.parse({"type": "conversation.item.input_audio_transcription.delta", "item_id": "user1", "delta": "Hel"}, 1)
    final = tracker.parse({"type": "conversation.item.input_audio_transcription.completed", "item_id": "user1", "transcript": "Hello"}, 2)
    assert (delta.text, delta.final, final.text, final.final, final.role) == ("Hel", False, "Hello", True, "user")
    assert tracker.parse({"type": "conversation.item.input_audio_transcription.completed", "item_id": "user1", "transcript": "Hello"}, 3).kind == "duplicate"
    assert tracker.parse({"type": "conversation.item.input_audio_transcription.delta", "item_id": "user1", "delta": "late"}, 4).kind == "duplicate"
    tracker.parse({"type": "response.created", "response": {"id": "resp1"}}, 5)
    draft = tracker.parse({"type": "response.output_audio_transcript.delta", "response_id": "resp1", "item_id": "assistant1", "delta": "Hi"}, 6)
    done = tracker.parse({"type": "response.output_audio_transcript.done", "response_id": "resp1", "item_id": "assistant1", "transcript": "Hi there"}, 7)
    assert draft.role == done.role == "assistant" and not draft.final and done.final
    assert done.response_id == "resp1" and done.session_id == "sess_test"
    assert done.content_index == 0


def test_partial_audio_text_failed_response_and_usage():
    tracker = EventTracker(state=ConversationState.READY, session_id="sess_test")
    tracker.parse({"type": "response.created", "response": {"id": "r"}}, 1)
    text = tracker.parse({"type": "response.output_text.delta", "response_id": "r", "item_id": "i", "delta": "hello"}, 2)
    audio = tracker.parse({"type": "response.output_audio.delta", "response_id": "r", "item_id": "i", "delta": "AQI="}, 3)
    assert text.text == "hello" and not text.final and audio.audio_base64 == "AQI="
    final = tracker.parse({"type": "response.output_text.done", "response_id": "r", "item_id": "i", "text": "hello!"}, 4)
    assert final.final and final.text == "hello!"
    failed = tracker.parse({"type": "response.done", "response": {"id": "r", "status": "failed", "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}}, 5)
    assert failed.status == "failed" and failed.state == ConversationState.ERROR
    assert tracker.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


def test_invalid_and_unknown_events_are_safe():
    tracker = EventTracker()
    assert tracker.parse("not json", 0).kind == "invalid"
    assert tracker.parse([], 0).kind == "invalid"
    assert tracker.parse({"type": "response.done", "response": {"status": "completed"}}, 0).kind == "invalid"
    assert tracker.parse({"type": "future.event", "event_id": "e"}, 0).kind == "unknown"


def test_cancel_and_unexpected_disconnect():
    async def scenario():
        socket = FakeSocket(CREATED, UPDATED, {"type": "response.created", "response": {"id": "r"}})
        client, _ = client_with(socket)
        await client.connect()
        await client.receive()
        await client.cancel_response("r")
        assert socket.sent[-1] == {"type": "response.cancel", "response_id": "r"}
        with pytest.raises(RealtimeError, match="closed unexpectedly"):
            await client.receive()
        assert client.state == ConversationState.ERROR
        assert socket.closed
    run(scenario())


def test_expiry_idle_response_and_session_limits():
    async def scenario():
        for trigger in ("idle", "session", "response"):
            now = [0.0]
            socket = FakeSocket(CREATED, UPDATED)
            client, _ = client_with(socket, realtime_max_session_seconds=20,
                                   realtime_idle_seconds=10, realtime_max_response_seconds=5)
            client.clock = lambda: now[0]
            await client.connect()
            if trigger == "idle":
                now[0] = 10.0
            elif trigger == "session":
                client.last_activity = 19.0
                now[0] = 20.0
            else:
                client.last_activity = 4.0
                client.tracker.response_started["r"] = 0.0
                now[0] = 5.0
            with pytest.raises(RealtimeError, match={"idle": "idle", "session": "Session duration", "response": "Response duration"}[trigger]):
                await client.receive()
            assert socket.closed
    run(scenario())


def test_idle_watchdog_closes_without_receive():
    async def scenario():
        now = [0.0]
        socket = FakeSocket(CREATED, UPDATED)
        client, _ = client_with(socket, realtime_max_session_seconds=10,
                               realtime_idle_seconds=2, realtime_max_response_seconds=5)
        client.clock = lambda: now[0]
        await client.connect()
        watchdog = client._watchdog
        await asyncio.sleep(0)
        now[0] = 2.0
        client._activity.set()
        await asyncio.wait_for(watchdog, 1)
        assert socket.closed and client.state == ConversationState.DISCONNECTED
        with pytest.raises(RealtimeError, match="idle timeout"):
            await client.receive()
    run(scenario())


def test_shutdown_while_receive_is_waiting():
    class WaitingSocket(FakeSocket):
        def __init__(self):
            super().__init__(CREATED, UPDATED)
            self.done = asyncio.Event()

        async def recv(self):
            if self.events:
                return await super().recv()
            await self.done.wait()
            raise OSError("socket closed")

        async def close(self):
            self.closed = True
            self.done.set()

    async def scenario():
        socket = WaitingSocket()
        client, _ = client_with(socket)
        await client.connect()
        waiting = asyncio.create_task(client.receive())
        await asyncio.sleep(0)
        await client.close()
        with pytest.raises(RealtimeError, match="closed unexpectedly"):
            await waiting
        assert socket.closed
    run(scenario())


def test_cli_default_offline_and_redacted(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    assert cli_main([]) == 0
    output = capsys.readouterr().out
    assert "API key configured: yes" in output
    assert "no connection opened" in output
    assert "private-test-value" not in output


def test_flask_init_and_routes_do_not_expose_key_or_connect():
    app = create_app(Settings(openai_api_key="private-test-value"))
    with app.test_client() as browser:
        assert browser.get("/").status_code == 200
        assert browser.get("/health").json == {"status": "healthy"}
        assert b"private-test-value" not in browser.get("/").data
