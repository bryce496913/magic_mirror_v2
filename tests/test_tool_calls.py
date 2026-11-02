"""Realtime tool lifecycle with fake sockets, audio devices, and local executors."""

import asyncio
import json
import logging
import threading

import pytest

from magic_mirror.assistant.client import RealtimeError
from magic_mirror.assistant.events import ConversationEvent, ConversationState, FunctionCall
from magic_mirror.assistant.tools import (Permission, ToolCancelled, ToolDispatcher, ToolRegistry,
                                          ToolSpec, ToolStatus, default_registry)
from magic_mirror.assistant.voice import VoiceError, VoicePhase
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
        await client.close()
    asyncio.run(scenario())


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
                "days": [{"date": "2026-11-03", "high": {"value": 15, "unit": "°C"}}]}

    monkeypatch.setattr(WeatherTools, "forecast", forecast)

    async def scenario():
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
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
            output = sent_outputs(client)
            assert len(output) == 1 and output[0]["call_id"] == "weather-id"
            assert json.loads(output[0]["output"])["data"]["days"][0]["high"]["value"] == 15
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
        voice.on_tool_visual = store.on_tool_visual
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
            assert store.snapshot().tool and not store.snapshot().assistant_text
            assert store.snapshot().tool.call_id == "search-id"
            assert voice.phase == VoicePhase.PROCESSING and audio.playback.starts == 0
            client.queue.put_nowait(event("response.created", response="spoken-search"))
            client.queue.put_nowait(event("response.output_audio.delta", response="spoken-search",
                                          item="audio", audio=pcm_audio()))
            client.queue.put_nowait(event("response.output_audio_transcript.done", response="spoken-search",
                                          item="audio", role="assistant", text="Raspberry Pi reports a release.", final=True))
            client.queue.put_nowait(event("response.done", response="spoken-search", status="completed"))
            await wait_until(lambda: audio.playback.starts == 1 and voice.phase == VoicePhase.LISTENING)
            assert store.snapshot().assistant_text == "Raspberry Pi reports a release."
            assert store.snapshot().tool.sources[0].url.startswith("https://")
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="u2"))
            await wait_until(lambda: store.snapshot().tool is None)
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
        seen_visual = []
        voice.on_tool_visual = seen_visual.append
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
        assert calls == [1] and not any(item is not None and item.status == "completed"
                                        for item in seen_visual)
        assert not any(item["call_id"] == "late-call" for item in sent_outputs(client))
        assert audio.closed and client.closed

    asyncio.run(scenario())


def test_weather_tool_visual_tracks_actual_call_and_speech(monkeypatch):
    from magic_mirror.assistant.display import AssistantStateStore
    from magic_mirror.assistant.tools.weather import WeatherTools
    from magic_mirror.assistant.wake import WakeState

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def current(_self, _arguments):
            entered.set()
            await release.wait()
            return {"availability": "available", "location": "Auckland",
                    "conditions": "Rain", "temperature": {"value": 11, "unit": "°C"},
                    "freshness": "fresh", "precipitation_probability_percent": 70}

        monkeypatch.setattr(WeatherTools, "current", current)
        store = AssistantStateStore()
        store.on_state(WakeState.LISTENING)
        voice, audio, client, _, _ = make_voice(tools_enabled=True)
        voice.on_event = store.on_event
        voice.on_playback = store.on_playback
        voice.on_tool_visual = store.on_tool_visual
        task = asyncio.create_task(voice.run())
        try:
            await wait_until(lambda: audio.capture.active)
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="u1"))
            client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="u1"))
            client.queue.put_nowait(event("response.created", response="tool-r"))
            call = tool_call("weather-call", name="get_current_weather",
                             arguments='{"location":"Auckland"}', item_id="tool-item")
            client.queue.put_nowait(tool_done("tool-r", call))
            await entered.wait()
            snap = store.snapshot()
            assert snap.tool.status == "loading" and snap.tool.turn_id == "u1"
            assert snap.tool.call_id == "weather-call" and snap.tool.response_id == "tool-r"
            release.set()
            await wait_until(lambda: store.snapshot().tool is not None and
                             store.snapshot().tool.status == "completed")
            assert "Auckland" in store.snapshot().tool.title
            assert "11°C" in store.snapshot().tool.summary
            client.queue.put_nowait(event("response.created", response="spoken-r"))
            client.queue.put_nowait(event("response.output_audio.delta", response="spoken-r",
                                          item="audio", audio=pcm_audio()))
            client.queue.put_nowait(event("response.done", response="spoken-r", status="completed"))
            await wait_until(lambda: audio.playback.starts == 1 and voice.phase == VoicePhase.LISTENING)
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item="u2"))
            await wait_until(lambda: store.snapshot().tool is None)
        finally:
            release.set()
            voice.stop_event.set()
            await task

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
