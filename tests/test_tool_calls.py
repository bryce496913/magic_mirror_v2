"""Realtime tool lifecycle with fake sockets, audio devices, and local executors."""

import asyncio
import json
import logging
import threading

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from magic_mirror.assistant.client import RealtimeClient, RealtimeError
from magic_mirror.assistant.display import AssistantStateStore
from magic_mirror.assistant.events import ConversationEvent, ConversationState, EventTracker, FunctionCall
from magic_mirror.assistant.tools import (Permission, ToolCancelled, ToolDispatcher, ToolRegistry,
                                          ToolSpec, ToolStatus, default_registry)
from magic_mirror.assistant.voice import VoiceError, VoicePhase
from magic_mirror.assistant.wake import WakeState
from magic_mirror.config import ConfigurationError, Settings
from test_realtime import CREATED, UPDATED, FakeSocket, client_with
from test_voice import FakePlayback, event, make_voice, pcm_audio, wait_until


def tool_call(call_id="call_1", *, name="get_mirror_capabilities", arguments="{}", item_id="item_1"):
    return FunctionCall(call_id, item_id, name, arguments)


def tool_done(response_id="response_1", *calls, status="completed"):
    return ConversationEvent("response.done", ConversationState.READY, "sess_test", response_id,
                             status=status, tool_calls=calls)


def tool_registry(executor, *, timeout=1.0, max_output=512):
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="safe_echo", description="Echo a short, harmless test word.",
        parameters={"type": "object", "properties": {"word": {"type": "string", "maxLength": 16}},
                    "required": ["word"], "additionalProperties": False},
        enabled=True, permission=Permission.READ_ONLY, executor=executor,
        timeout_seconds=timeout, max_input_bytes=128, max_output_bytes=max_output,
    ))
    return registry


def sent_outputs(client):
    return [payload["item"] for payload in client.sent if payload["type"] == "conversation.item.create"]


def test_official_function_call_events_and_mock_websocket_continuation():
    delta = {"type": "response.function_call_arguments.delta", "response_id": "r1",
             "item_id": "i1", "call_id": "c1", "output_index": 0, "delta": "{"}
    done = {"type": "response.function_call_arguments.done", "response_id": "r1",
            "item_id": "i1", "call_id": "c1", "name": "get_mirror_capabilities",
            "output_index": 0, "arguments": "{}"}
    response = {"type": "response.done", "response": {"id": "r1", "status": "completed",
                "output": [{"id": "i1", "type": "function_call", "call_id": "c1",
                            "name": "get_mirror_capabilities", "arguments": "{}"}]}}

    async def scenario():
        socket = FakeSocket(CREATED, UPDATED, delta, done, response)
        client, _ = client_with(socket)
        try:
            await client.connect()
            partial, final, completed = await client.receive(), await client.receive(), await client.receive()
            assert partial.tool_call.call_id == final.tool_call.call_id == "c1"
            assert partial.tool_delta == "{" and final.tool_call.arguments == "{}"
            assert completed.tool_calls == (tool_call("c1", item_id="i1"),)
            assert client.tracker.parse(response, 1).kind == "duplicate"
            await client.send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": "c1", "output": '{"status":"success"}'}})
            await client.send({"type": "response.create"})
            assert socket.sent[-2]["item"]["call_id"] == "c1"
            assert socket.sent[-1] == {"type": "response.create"}
        finally:
            await client.close()
        assert socket.closed and client._watchdog is None
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_raw_realtime_function_sequence_dispatches_and_logs_safe_stages(caplog):
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        raw = [
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.added", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "call_id": "c1", "name": "get_mirror_capabilities"}},
            {"type": "response.function_call_arguments.done", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "name": "get_mirror_capabilities", "arguments": "{}"},
            {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": "{}"}]}},
        ]
        for payload in raw:
            client.queue.put_nowait(tracker.parse(payload, 1))
        await wait_until(lambda: any(item["type"] == "response.create" for item in client.sent))
        assert json.loads(sent_outputs(client)[0]["output"])["status"] == "success"
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    for stage in ("Function-call requested", "Function-call completion detected",
                  "validation accepted", "execution started", "execution result (success)",
                  "Function output submitted", "Response continuation requested"):
        assert stage in caplog.text
    assert '"arguments"' not in caplog.text and '"capabilities"' not in caplog.text


@pytest.mark.parametrize("added_arguments", ["", "{", "{}"])
def test_provisional_added_arguments_reconcile_with_completed_call_and_spoken_reply(added_arguments, caplog):
    """Realtime may add an item with an empty argument buffer before final JSON."""
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        raw = [
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.added", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": added_arguments}},
            {"type": "response.function_call_arguments.delta", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "delta": "{"},
            {"type": "response.function_call_arguments.delta", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "delta": "}"},
            {"type": "response.function_call_arguments.done", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "name": "get_mirror_capabilities", "arguments": "{}"},
            {"type": "response.output_item.done", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": "{}"}},
            {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": "{}"}]}},
        ]
        for payload in raw:
            client.queue.put_nowait(tracker.parse(payload, 1))
        await wait_until(lambda: any(item["type"] == "response.create" for item in client.sent))
        assert len(sent_outputs(client)) == 1
        assert json.loads(sent_outputs(client)[0]["output"])["status"] == "success"
        assert sum(item["type"] == "response.create" for item in client.sent) == 1
        client.queue.put_nowait(event("response.created", response="r2"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r2", item="spoken",
                                      audio=pcm_audio()))
        client.queue.put_nowait(event("response.done", response="r2", status="completed"))
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.playback.starts == 1)
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Tool get_mirror_capabilities execution result (success)" in caplog.text
    assert "conflicting_arguments" not in caplog.text


def test_completed_item_arguments_conflicting_with_response_done_are_rejected():
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        for payload in (
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.added", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "call_id": "c1", "name": "safe_echo",
                "arguments": '{"word":"provisional"}'}},
            {"type": "response.output_item.done", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "safe_echo", "arguments": '{"word":"first"}'}},
            {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "safe_echo", "arguments": '{"word":"second"}'}]}},
        ):
            client.queue.put_nowait(tracker.parse(payload, 1))
        await wait_until(lambda: any(item["type"] == "response.create" for item in client.sent))
        assert json.loads(sent_outputs(client)[0]["output"]) == {"status": "invalid_arguments"}
        assert calls == []
        voice.stop_event.set()
        await task

    asyncio.run(scenario())


def test_realtime_transport_continuation_and_audio_drain_after_valid_tool(caplog):
    """Exercise the real client tracker/watchdog with a fake WebSocket and devices."""
    async def scenario():
        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.sent = []
                self.closed = False
                for payload in (CREATED, UPDATED, UPDATED):
                    self.incoming.put_nowait(payload)

            async def recv(self):
                return json.dumps(await self.incoming.get())

            async def send(self, message):
                payload = json.loads(message)
                self.sent.append(payload)
                if payload["type"] == "response.create":
                    for incoming in (
                        {"type": "response.created", "response": {"id": "r2"}},
                        {"type": "response.output_audio.delta", "response_id": "r2",
                         "item_id": "spoken", "delta": pcm_audio()},
                        {"type": "response.done", "response": {"id": "r2", "status": "completed", "output": []}},
                    ):
                        self.incoming.put_nowait(incoming)
                elif payload["type"] == "input_audio_buffer.clear":
                    self.incoming.put_nowait({"type": "input_audio_buffer.cleared"})

            async def close(self):
                self.closed = True

        socket = Socket()

        async def connector(_url, **_kwargs):
            return socket

        voice, audio, _, _, _ = make_voice(tools_enabled=True,
                                          openai_api_key="fixture-only-key",
                                          realtime_idle_seconds=5,
                                          realtime_max_response_seconds=10)
        voice.client = RealtimeClient(voice.settings, connector=connector)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        for incoming in (
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.added", "response_id": "r1", "item": {
                "id": "i1", "type": "function_call", "call_id": "c1", "name": "get_mirror_capabilities",
                "arguments": ""}},
            {"type": "response.function_call_arguments.done", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "name": "get_mirror_capabilities", "arguments": "{}"},
            {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": "{}"}]}},
        ):
            socket.incoming.put_nowait(incoming)
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.playback.starts == 1)
        outputs = [item for item in socket.sent if item["type"] == "conversation.item.create"]
        assert len(outputs) == 1 and outputs[0]["item"]["call_id"] == "c1"
        assert json.loads(outputs[0]["item"]["output"])["status"] == "success"
        assert sum(item["type"] == "response.create" for item in socket.sent) == 1
        assert voice.client._expiry_reason is None
        voice.stop_event.set()
        await task
        assert socket.closed and audio.closed

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Realtime transport error" not in caplog.text
    assert caplog.text.count("Response continuation started") == 1


def test_remote_close_after_tool_continuation_releases_audio_and_logs_only_safe_details(caplog):
    async def scenario():
        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.sent = []
                self.closed = False
                for payload in (CREATED, UPDATED, UPDATED):
                    self.incoming.put_nowait(payload)

            async def recv(self):
                value = await self.incoming.get()
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value)

            async def send(self, message):
                payload = json.loads(message)
                self.sent.append(payload)
                if payload["type"] == "response.create":
                    # No response.created or response.done follows the continuation.
                    self.incoming.put_nowait(ConnectionClosedError(
                        Close(1011, "private-provider-reason"),
                        Close(1011, "private-provider-reason"), True))

            async def close(self):
                self.closed = True

        socket = Socket()

        async def connector(_url, **_kwargs):
            return socket

        voice, audio, _, _, _ = make_voice(tools_enabled=True,
                                          openai_api_key="fixture-only-key")
        voice.client = RealtimeClient(voice.settings, connector=connector)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        for incoming in (
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{
                "id": "i1", "type": "function_call", "status": "completed", "call_id": "c1",
                "name": "get_mirror_capabilities", "arguments": "{}"}]}},
        ):
            socket.incoming.put_nowait(incoming)
        with pytest.raises(RealtimeError, match="closed unexpectedly") as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value.category == "remote_error" and caught.value.close_code == 1011
        assert socket.closed and audio.closed and not audio.capture.active
        assert sum(payload["type"] == "response.create" for payload in socket.sent) == 1

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "close_category=remote_error close_code=1011" in caplog.text
    assert "Response continuation started" not in caplog.text
    assert "private-provider-reason" not in caplog.text


def test_voice_callbacks_keep_tool_and_spoken_response_ids_in_one_dashboard_exchange():
    async def scenario():
        store = AssistantStateStore()
        store.on_state(WakeState.LISTENING)
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        voice.on_event = store.on_event
        voice.on_playback = store.on_playback
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("conversation.item.input_audio_transcription.completed",
                                      item="user", role="user", text="Capabilities?", final=True))
        client.queue.put_nowait(event("response.created", response="tool-response"))
        client.queue.put_nowait(tool_done("tool-response", tool_call("c1")))
        await wait_until(lambda: any(item["type"] == "response.create" for item in client.sent))
        client.queue.put_nowait(event("response.created", response="spoken-response"))
        client.queue.put_nowait(event("response.output_audio.delta", response="spoken-response",
                                      item="spoken-item", audio=pcm_audio()))
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        completed = tracker.parse({"type": "response.done", "response": {
            "id": "spoken-response", "status": "completed", "output": [{
                "id": "spoken-item", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_audio", "transcript": "I can answer questions."}]}]}}, 1)
        client.queue.put_nowait(completed)
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.playback.starts == 1)
        assert store.snapshot().user_text == "Capabilities?"
        assert store.snapshot().assistant_text == "I can answer questions."
        assert store.snapshot().assistant_final and not store.snapshot().playback
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_raw_failed_function_response_is_diagnosed_before_dispatch(status, caplog):
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        for payload in (
            {"type": "input_audio_buffer.speech_started", "item_id": "user"},
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.function_call_arguments.done", "response_id": "r1", "item_id": "i1",
             "call_id": "c1", "name": "get_mirror_capabilities",
             "arguments": '{"private":"never log this value"}'},
            {"type": "response.done", "response": {"id": "r1", "status": status,
             "status_details": {"error": {"code": "server_error"}}, "output": [{
                 "id": "i1", "type": "function_call", "status": "incomplete", "call_id": "c1",
                 "name": "get_mirror_capabilities", "arguments": "{}"}]}},
        ):
            client.queue.put_nowait(tracker.parse(payload, 1))
        with pytest.raises(VoiceError, match=status):
            await task
        assert audio.closed and client.closed and not sent_outputs(client)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert f"Realtime response completed (status={status}, function_calls=1)" in caplog.text
    assert f"Realtime response {status} before tool dispatch" in caplog.text
    assert "execution started" not in caplog.text
    assert "never log this value" not in caplog.text


@pytest.mark.parametrize("failure,reason", [("Realtime send failed", "send_failed"),
                                            ("private transport detail", "unknown")])
def test_continuation_send_failure_closes_voice_after_one_local_execution(failure, reason, caplog):
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        original_send = client.send

        async def reject_continuation(payload):
            if payload["type"] == "response.create":
                raise RealtimeError(failure)
            await original_send(payload)

        client.send = reject_continuation
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1")))
        with pytest.raises(RealtimeError, match=failure):
            await task
        assert len(sent_outputs(client)) == 1
        assert audio.closed and client.closed

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Function output submitted (status=success)" in caplog.text
    assert "Response continuation requested" not in caplog.text
    assert f"Realtime transport error (reason={reason})" in caplog.text
    assert "private transport detail" not in caplog.text


def test_realtime_error_before_function_dispatch_is_safely_logged(caplog):
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        tracker = EventTracker(session_id="sess_test", state=ConversationState.READY)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(tracker.parse({"type": "input_audio_buffer.speech_started",
                                               "item_id": "user"}, 1))
        client.queue.put_nowait(tracker.parse({"type": "response.created", "response": {"id": "r1"}}, 2))
        client.queue.put_nowait(tracker.parse({"type": "error", "error": {
            "code": "secret value must not be logged", "message": "provider raw body"}}, 3))
        with pytest.raises(VoiceError, match="unrecognized_error"):
            await task
        assert audio.closed and client.closed and not sent_outputs(client)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Recoverable Realtime error (code=unrecognized_error)" in caplog.text
    assert "secret value" not in caplog.text and "provider raw body" not in caplog.text


def test_registered_tool_keeps_half_duplex_until_spoken_continuation_finishes():
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        assert client.sent[0]["session"]["tools"][0]["name"] == "get_mirror_capabilities"
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.done",
                                ConversationState.RESPONDING, "sess_test", "r1", "i1",
                                tool_call=tool_call("c1", item_id="i1")))
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.delta",
                                ConversationState.RESPONDING, "sess_test", "r1", "i1",
                                tool_call=FunctionCall("c1", "i1", "", None), tool_delta="{"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", item_id="i1")))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        output = sent_outputs(client)
        assert len(output) == 1 and output[0]["call_id"] == "c1"
        assert json.loads(output[0]["output"])["status"] == "success"
        assert voice.phase == VoicePhase.PROCESSING and not client.closed
        count = sum(x["type"] == "input_audio_buffer.append" for x in client.sent)
        await asyncio.sleep(0.03)
        assert sum(x["type"] == "input_audio_buffer.append" for x in client.sent) == count
        client.queue.put_nowait(event("response.created", response="r2"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r2", item="audio",
                                      audio=pcm_audio()))
        client.queue.put_nowait(event("response.done", response="r2", status="completed"))
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.playback.starts == 1)
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed and audio.capture.starts == 1
    asyncio.run(scenario())


def test_weather_result_correlates_with_spoken_continuation(monkeypatch):
    from magic_mirror.assistant.tools.weather import WeatherTools

    async def forecast(_self, arguments):
        assert arguments == {"period": "tomorrow"}
        return {"availability": "available", "location": "New York", "period": "tomorrow",
                "days": [{"date": "2026-11-03", "high": {
                    "fahrenheit": 59.0, "celsius": 15.0, "value": 15.0, "unit": "°C"}}]}

    monkeypatch.setattr(WeatherTools, "forecast", forecast)

    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        task = asyncio.create_task(voice.run())
        try:
            await wait_until(lambda: audio.capture.active)
            instructions = next(x["session"]["instructions"] for x in client.sent
                                if x["type"] == "session.update")
            assert "Fahrenheit value first, then its Celsius value" in instructions
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
            client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="user"))
            client.queue.put_nowait(event("response.created", response="weather-call"))
            call = tool_call("weather-id", name="get_weather_forecast",
                             arguments='{"period":"tomorrow"}', item_id="forecast-item")
            client.queue.put_nowait(tool_done("weather-call", call))
            await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
            output = sent_outputs(client)
            assert len(output) == 1 and output[0]["call_id"] == "weather-id"
            high = json.loads(output[0]["output"])["data"]["days"][0]["high"]
            assert high["fahrenheit"] == 59.0 and high["celsius"] == 15.0
            assert voice.phase == VoicePhase.PROCESSING and audio.playback.starts == 0
            client.queue.put_nowait(event("response.created", response="spoken-weather"))
            client.queue.put_nowait(event("response.output_audio.delta", response="spoken-weather",
                                          item="audio", audio=pcm_audio()))
            client.queue.put_nowait(event("response.done", response="spoken-weather", status="completed"))
            await wait_until(lambda: audio.playback.starts == 1 and voice.phase == VoicePhase.LISTENING)
        finally:
            voice.stop_event.set()
            await task

    asyncio.run(scenario())


def test_weather_timeout_submits_bounded_failure_and_spoken_continuation(monkeypatch):
    from magic_mirror.assistant.tools.weather import WeatherTools

    async def stalled_forecast(_self, _arguments):
        await asyncio.Event().wait()

    monkeypatch.setattr(WeatherTools, "forecast", stalled_forecast)

    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_timeout_seconds=0.02)
        task = asyncio.create_task(voice.run())
        try:
            await wait_until(lambda: audio.capture.active)
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
            client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="user"))
            client.queue.put_nowait(event("response.created", response="weather-call"))
            call = tool_call("weather-id", name="get_weather_forecast",
                             arguments='{"period":"tomorrow"}', item_id="forecast-item")
            client.queue.put_nowait(tool_done("weather-call", call))
            await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
            assert json.loads(sent_outputs(client)[0]["output"]) == {"status": "timeout"}
            assert voice.phase == VoicePhase.PROCESSING and audio.playback.starts == 0
            client.queue.put_nowait(event("response.created", response="spoken-timeout"))
            client.queue.put_nowait(event("response.output_audio.delta", response="spoken-timeout",
                                          item="audio", audio=pcm_audio()))
            client.queue.put_nowait(event("response.done", response="spoken-timeout", status="completed"))
            await wait_until(lambda: audio.playback.starts == 1 and voice.phase == VoicePhase.LISTENING)
        finally:
            voice.stop_event.set()
            await task

    asyncio.run(scenario())


def test_search_citations_wait_for_matching_spoken_audio_and_clear_on_new_turn(monkeypatch):
    from magic_mirror.assistant.display import AssistantStateStore
    from magic_mirror.assistant.tools.search import WebSearch
    from magic_mirror.assistant.wake import WakeState

    async def search(_self, arguments):
        assert arguments == {"query": "latest Raspberry Pi OS release"}
        return {"availability": "available", "summary": "Release announced [1].",
                "sources": [{"title": "Official release", "url": "https://www.raspberrypi.com/news/release/",
                             "source_name": "www.raspberrypi.com"}],
                "citations": [{"source": 0, "start": 18, "end": 21}]}

    monkeypatch.setattr(WebSearch, "search", search)

    async def scenario():
        store = AssistantStateStore()
        store.on_state(WakeState.LISTENING)
        voice, audio, client, _, _ = make_voice(tools_enabled=True, search_enabled=True,
                                                openai_api_key="fixture-only-key")
        voice.on_event = store.on_event
        voice.on_playback = store.on_playback
        voice.on_sources = store.on_sources
        task = asyncio.create_task(voice.run())
        try:
            await wait_until(lambda: audio.capture.active)
            assert "search_web" in {x["name"] for x in client.sent[0]["session"]["tools"]}
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="u1"))
            client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="u1"))
            client.queue.put_nowait(event("response.created", response="search-call"))
            call = tool_call("search-id", name="search_web",
                             arguments='{"query":"latest Raspberry Pi OS release"}', item_id="search-item")
            client.queue.put_nowait(tool_done("search-call", call))
            await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
            output = sent_outputs(client)
            assert output[0]["call_id"] == "search-id"
            assert json.loads(output[0]["output"])["data"]["sources"][0]["title"] == "Official release"
            assert store.snapshot().sources and not store.snapshot().assistant_text
            assert voice.phase == VoicePhase.PROCESSING and audio.playback.starts == 0
            client.queue.put_nowait(event("response.created", response="spoken-search"))
            client.queue.put_nowait(event("response.output_audio.delta", response="spoken-search",
                                          item="audio", audio=pcm_audio()))
            client.queue.put_nowait(event("response.output_audio_transcript.done", response="spoken-search",
                                          item="audio", role="assistant", text="Raspberry Pi reports a release.", final=True))
            client.queue.put_nowait(event("response.done", response="spoken-search", status="completed"))
            await wait_until(lambda: audio.playback.starts == 1 and voice.phase == VoicePhase.LISTENING)
            assert store.snapshot().assistant_text == "Raspberry Pi reports a release."
            assert store.snapshot().sources[0]["url"].startswith("https://")
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="u2"))
            await wait_until(lambda: store.snapshot().sources == ())
        finally:
            voice.stop_event.set()
            await task

    asyncio.run(scenario())


def test_search_never_runs_without_function_call_and_late_result_is_discarded(monkeypatch):
    from magic_mirror.assistant.tools.search import WebSearch

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(_self, _arguments):
            calls.append(1)
            entered.set()
            await release.wait()
            return {"availability": "available", "summary": "Late [1].", "sources": [{
                "title": "Late source", "url": "https://example.org/late",
                "source_name": "example.org"}]}

        monkeypatch.setattr(WebSearch, "search", slow)
        voice, audio, client, _, _ = make_voice(tools_enabled=True, search_enabled=True,
                                                openai_api_key="fixture-only-key")
        seen_sources = []
        voice.on_sources = seen_sources.append
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="stable"))
        client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="stable"))
        client.queue.put_nowait(event("response.created", response="stable-answer"))
        client.queue.put_nowait(event("response.output_audio.delta", response="stable-answer",
                                      item="audio", audio=pcm_audio()))
        client.queue.put_nowait(event("response.done", response="stable-answer", status="completed"))
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.playback.starts == 1)
        assert calls == []
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="recent"))
        client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="recent"))
        client.queue.put_nowait(event("response.created", response="search-call"))
        client.queue.put_nowait(tool_done("search-call", tool_call("late-call", name="search_web",
            arguments='{"query":"latest release"}', item_id="late-item")))
        await entered.wait()
        voice.stop_event.set()
        release.set()
        await task
        assert calls == [1] and not seen_sources
        assert not any(item["call_id"] == "late-call" for item in sent_outputs(client))
        assert audio.closed and client.closed

    asyncio.run(scenario())


def test_audio_before_function_call_drains_before_tool_continuation():
    class GatedPlayback(FakePlayback):
        def __init__(self):
            super().__init__()
            self.draining = threading.Event()
            self.release = threading.Event()
        def drain(self, timeout=None):
            self.draining.set()
            assert self.release.wait(timeout=2)
            super().drain(timeout)

    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True, audio_buffer_duration=0.02)
        playback = GatedPlayback()
        audio.playback = playback
        task = asyncio.create_task(voice.run())
        try:
            await wait_until(lambda: audio.capture.active)
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
            client.queue.put_nowait(event("response.created", response="r1"))
            client.queue.put_nowait(event("response.output_audio.delta", response="r1", item="prelude",
                                          audio=pcm_audio()))
            client.queue.put_nowait(tool_done("r1", tool_call("c1")))
            await wait_until(lambda: playback.draining.is_set())
            assert voice.phase == VoicePhase.PLAYBACK_DRAINING
            assert not any(x["type"] == "response.create" for x in client.sent)
            count = sum(x["type"] == "input_audio_buffer.append" for x in client.sent)
            await asyncio.sleep(0.03)
            assert sum(x["type"] == "input_audio_buffer.append" for x in client.sent) == count
            playback.release.set()
            await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
            assert playback._stream is None and voice.phase == VoicePhase.PROCESSING
        finally:
            playback.release.set()
            voice.stop_event.set()
            await task
    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", ["{", '{"word":1}', '{"word":"ok","extra":1}',
                                      '{"word":"abcdefghijklmnopq"}', '{"word":"a","word":"b"}',
                                      '{"word":NaN}', '[]', '"text"', "x" * 129])
def test_dispatcher_rejects_malformed_or_unauthorized_arguments(arguments):
    calls = []

    async def executor(value):
        calls.append(value)
        return {"word": value["word"]}

    async def scenario():
        dispatcher = ToolDispatcher(tool_registry(executor), Settings(tools_enabled=True))
        assert (await dispatcher.execute("safe_echo", arguments)).status == ToolStatus.INVALID_ARGUMENTS
        assert (await dispatcher.execute("not_registered", '{}')).status == ToolStatus.TOOL_UNAVAILABLE
        assert calls == []
    asyncio.run(scenario())


def test_two_calls_execute_once_each_with_one_response_continuation():
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value["word"])
            await asyncio.sleep(0)
            return {"word": value["word"]}

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        first = tool_call("c1", name="safe_echo", arguments='{"word":"one"}', item_id="i1")
        second = tool_call("c2", name="safe_echo", arguments='{"word":"two"}', item_id="i2")
        client.queue.put_nowait(tool_done("r1", first, second))
        client.queue.put_nowait(tool_done("r1", first, second))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert sorted(calls) == ["one", "two"]
        assert [x["call_id"] for x in sent_outputs(client)] == ["c1", "c2"]
        assert sum(x["type"] == "response.create" for x in client.sent) == 1
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_duplicate_call_id_fails_closed_without_execution():
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        call = tool_call("duplicate", name="safe_echo", arguments='{"word":"one"}')
        client.queue.put_nowait(tool_done("r1", call, call))
        with pytest.raises(VoiceError, match="Duplicate"):
            await task
        assert calls == [] and not sent_outputs(client) and audio.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("name,arguments,expected", [
    ("unknown_tool", "{}", "tool_unavailable"),
    ("safe_echo", "{", "invalid_arguments"),
    ("safe_echo", '{"word":7}', "invalid_arguments"),
    ("safe_echo", '{"word":"yes","extra":true}', "invalid_arguments"),
])
def test_invalid_realtime_tool_calls_return_compact_results_without_execution(name, arguments, expected):
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name=name, arguments=arguments)))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        result = json.loads(sent_outputs(client)[0]["output"])
        assert result == {"status": expected} and calls == []
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_disabled_tools_are_not_advertised_or_executed():
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=False)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        assert "tools" not in client.sent[0]["session"]
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1")))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert json.loads(sent_outputs(client)[0]["output"]) == {"status": "tool_unavailable"}
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_cancelled_model_response_never_runs_completed_argument_event():
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        call = tool_call("c1", name="safe_echo", arguments='{"word":"ok"}')
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.done",
                                ConversationState.RESPONDING, "sess_test", "r1", "item_1", tool_call=call))
        client.queue.put_nowait(tool_done("r1", call, status="cancelled"))
        await wait_until(lambda: voice.phase == VoicePhase.LISTENING and audio.capture.discards == 1)
        assert calls == [] and not sent_outputs(client)
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_tool_requires_user_turn_and_rejects_conflicting_final_arguments():
    async def no_user():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call()))
        with pytest.raises(VoiceError, match="active user turn"):
            await task
        assert not sent_outputs(client)

    async def conflicting_final():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        call = tool_call("c1", name="safe_echo", arguments='{"word":"first"}')
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.done",
                                ConversationState.RESPONDING, "sess_test", "r1", "item_1", tool_call=call))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name="safe_echo",
                                                       arguments='{"word":"second"}')))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert json.loads(sent_outputs(client)[0]["output"]) == {"status": "invalid_arguments"}
        assert calls == []
        voice.stop_event.set()
        await task

    asyncio.run(no_user())
    asyncio.run(conflicting_final())


def test_mismatched_item_oversized_delta_and_incomplete_call_never_execute():
    async def scenario():
        calls = []

        async def executor(value):
            calls.append(value)
            return value

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        first = tool_call("c1", name="safe_echo", arguments='{"word":"one"}', item_id="i1")
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.done",
                                ConversationState.RESPONDING, "sess_test", "r1", "i1", tool_call=first))
        client.queue.put_nowait(ConversationEvent("response.function_call_arguments.delta",
                                ConversationState.RESPONDING, "sess_test", "r1", "i2",
                                tool_call=FunctionCall("c2", "i2", "", None), tool_delta="x" * 4097))
        incomplete = FunctionCall("c3", "i3", "safe_echo", '{"word":"three"}', "incomplete")
        client.queue.put_nowait(tool_done("r1",
                                         tool_call("c1", name="safe_echo",
                                                   arguments='{"word":"one"}', item_id="wrong"),
                                         tool_call("c2", name="safe_echo",
                                                   arguments='{"word":"two"}', item_id="i2"), incomplete,
                                         FunctionCall("c4", "i4", "safe_echo", "\ud800")))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert [json.loads(x["output"])["status"] for x in sent_outputs(client)] == [
            "invalid_arguments", "invalid_arguments", "invalid_arguments", "invalid_arguments"]
        assert calls == []
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_tool_concurrency_and_per_conversation_call_limit():
    async def scenario():
        active = 0
        maximum = 0
        release = asyncio.Event()

        async def executor(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await release.wait()
            active -= 1
            return value

        registry = tool_registry(executor)
        settings = Settings(tools_enabled=True, tool_concurrency=1, tool_timeout_seconds=1)
        dispatcher = ToolDispatcher(registry, settings)
        first = asyncio.create_task(dispatcher.execute("safe_echo", '{"word":"one"}'))
        second = asyncio.create_task(dispatcher.execute("safe_echo", '{"word":"two"}'))
        await asyncio.sleep(0.02)
        assert active == maximum == 1
        release.set()
        assert (await first).status == (await second).status == ToolStatus.SUCCESS

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_max_calls=1,
                                                tool_registry=registry)
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        calls = (tool_call("c1", name="safe_echo", arguments='{"word":"one"}', item_id="i1"),
                 tool_call("c2", name="safe_echo", arguments='{"word":"two"}', item_id="i2"))
        client.queue.put_nowait(tool_done("r1", *calls))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert [json.loads(x["output"])["status"] for x in sent_outputs(client)] == [
            "success", "tool_unavailable"]
        continuation = next(x for x in client.sent if x["type"] == "response.create")
        assert continuation["response"] == {"tool_choice": "none", "tools": []}
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_timeout_provider_failure_output_budget_and_no_secret_logging(caplog):
    secret = "fixture-only-sensitive-marker"

    async def slow(_):
        await asyncio.Event().wait()

    async def broken(_):
        raise RuntimeError(secret)

    async def oversized(_):
        return {"word": "x" * 600}

    async def cancelled(_):
        raise ToolCancelled()

    async def scenario():
        settings = Settings(tools_enabled=True, tool_timeout_seconds=0.02)
        args = '{"word":"ok"}'
        assert (await ToolDispatcher(tool_registry(slow), settings).execute("safe_echo", args)).status == ToolStatus.TIMEOUT
        assert (await ToolDispatcher(tool_registry(broken), settings).execute("safe_echo", args)).status == ToolStatus.PROVIDER_FAILURE
        assert (await ToolDispatcher(tool_registry(oversized), settings).execute("safe_echo", args)).status == ToolStatus.PROVIDER_FAILURE
        assert (await ToolDispatcher(tool_registry(cancelled), settings).execute("safe_echo", args)).status == ToolStatus.CANCELLED
    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "RuntimeError" in caplog.text and secret not in caplog.text


@pytest.mark.parametrize("failure,expected", [("timeout", "timeout"),
                                             ("exception", "provider_failure")])
def test_realtime_tool_failure_returns_status_then_continues(failure, expected):
    async def scenario():
        async def executor(_):
            if failure == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("private provider detail")

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_timeout_seconds=0.02,
                                                tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name="safe_echo",
                                                       arguments='{"word":"ok"}')))
        await wait_until(lambda: any(x["type"] == "response.create" for x in client.sent))
        assert json.loads(sent_outputs(client)[0]["output"]) == {"status": expected}
        assert "private provider detail" not in str(client.sent)
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_cancel_during_tool_execution_drops_late_result_and_restores_listening():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def executor(value):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return {"word": "late"}  # a misbehaving provider still must not send a stale result

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_timeout_seconds=0.05,
                                                tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name="safe_echo",
                                                       arguments='{"word":"ok"}')))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(voice.cancel_reply(), 0.3)
        assert voice.phase == VoicePhase.LISTENING and not sent_outputs(client)
        release.set()
        await asyncio.sleep(0.02)
        assert not any(x["type"] == "response.create" for x in client.sent)
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed
    asyncio.run(scenario())


def test_cancel_during_inflight_tool_output_closes_session_before_new_turn():
    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        original_send = client.send
        sending, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(payload):
            if payload["type"] == "conversation.item.create":
                sending.set()
                await release.wait()
            await original_send(payload)

        client.send = delayed_send
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1")))
        await asyncio.wait_for(sending.wait(), 1)
        await voice.cancel_reply()
        assert voice.stop_event.is_set()
        release.set()
        await task
        assert audio.closed and client.closed
        assert not any(x["type"] == "response.create" for x in client.sent)
    asyncio.run(scenario())


def test_disconnect_and_shutdown_cancel_pending_tool_without_output():
    async def scenario(disconnect):
        started = asyncio.Event()

        async def executor(_):
            started.set()
            await asyncio.Event().wait()

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name="safe_echo",
                                                       arguments='{"word":"ok"}')))
        await asyncio.wait_for(started.wait(), 1)
        if disconnect:
            client.queue.put_nowait(RealtimeError("Realtime connection closed unexpectedly"))
            with pytest.raises(RealtimeError):
                await task
        else:
            voice.stop_event.set()
            await task
        assert not sent_outputs(client) and audio.closed and client.closed
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_unexpected_response_while_tool_pending_closes_without_continuation():
    async def scenario():
        started = asyncio.Event()

        async def executor(_):
            started.set()
            await asyncio.Event().wait()

        voice, audio, client, _, _ = make_voice(tools_enabled=True, tool_registry=tool_registry(executor))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user"))
        client.queue.put_nowait(event("response.created", response="r1"))
        client.queue.put_nowait(tool_done("r1", tool_call("c1", name="safe_echo",
                                                       arguments='{"word":"ok"}')))
        await asyncio.wait_for(started.wait(), 1)
        client.queue.put_nowait(event("response.created", response="unexpected"))
        with pytest.raises(VoiceError, match="tool is pending"):
            await task
        assert not sent_outputs(client) and audio.closed and client.closed
    asyncio.run(scenario())


def test_tool_configuration_defaults_validation_and_registry_guards():
    assert not Settings.from_env({}).tools_enabled
    enabled = Settings.from_env({"MIRROR_TOOLS_ENABLED": "true", "MIRROR_TOOL_CONCURRENCY": "3",
                                 "MIRROR_TOOL_MAX_CALLS": "5"})
    assert enabled.tools_enabled and enabled.tool_concurrency == 3 and enabled.tool_max_calls == 5
    for name, value in [("MIRROR_TOOLS_ENABLED", "yes"), ("MIRROR_TOOL_TIMEOUT_SECONDS", "0"),
                        ("MIRROR_TOOL_CONCURRENCY", "9"), ("MIRROR_TOOL_MAX_OUTPUT_BYTES", "32"),
                        ("MIRROR_TOOL_MAX_CALLS", "0")]:
        with pytest.raises(ConfigurationError):
            Settings.from_env({name: value})
    registry = default_registry(Settings())
    assert registry.declarations() == []
    assert {item["name"] for item in default_registry(enabled).declarations()} == {
        "get_mirror_capabilities", "get_current_weather", "get_weather_forecast"}
    with pytest.raises(ValueError, match="duplicate"):
        spec = registry._tools["get_mirror_capabilities"]
        registry.register(spec)
    async def harmless(_):
        return {}
    for bad in [ToolSpec("run/shell", "bad", {"type": "object", "additionalProperties": False},
                         True, Permission.READ_ONLY, harmless, 1, 128, 128),
                ToolSpec("write_file", "bad", {"type": "object", "additionalProperties": False},
                         True, "write", harmless, 1, 128, 128),
                ToolSpec("wrong_permission_type", "bad", {"type": "object", "additionalProperties": False},
                         True, "read_only", harmless, 1, 128, 128),
                ToolSpec("remote", "bad", {"type": "object", "additionalProperties": False,
                                            "properties": {"x": {"$ref": "https://example.test/schema"}}},
                         True, Permission.READ_ONLY, harmless, 1, 128, 128),
                ToolSpec("sync_executor", "bad", {"type": "object", "additionalProperties": False},
                         True, Permission.READ_ONLY, lambda _: {}, 1, 128, 128)]:
        with pytest.raises(ValueError):
            ToolRegistry().register(bad)
