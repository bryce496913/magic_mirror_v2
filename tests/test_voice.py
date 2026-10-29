"""Pass 6B voice behavior without a microphone, speaker, or API key."""

import asyncio
import base64
import time

import numpy as np
import pytest

from magic_mirror.assistant.client import RealtimeError
from magic_mirror.assistant.events import ConversationEvent, ConversationState
from magic_mirror.assistant.resample import PCMRateError, StreamingPCM, StreamingPeakLimiter
from magic_mirror.assistant.voice import Transcript, VoiceConversation, VoiceError
from magic_mirror.assistant.voice_cli import main as cli_main
from magic_mirror.audio import AudioError
from magic_mirror.config import Settings


def event(kind, *, response=None, item=None, text=None, audio=None, status=None, final=False, role=None):
    return ConversationEvent(kind, ConversationState.READY, "sess_test", response, item,
                             role=role, text=text, audio_base64=audio, status=status, final=final,
                             content_index=0)


class FakeCapture:
    def __init__(self):
        self.active = False
        self.starts = 0
        self.reads = 0

    def start(self):
        if self.active:
            raise AudioError("duplicate capture")
        self.active = True
        self.starts += 1
        return True

    def read_frames(self, count):
        time.sleep(0.002)
        if not self.active:
            raise AudioError("capture stopped")
        self.reads += 1
        return np.full((count, 1), 1000, dtype=np.int16)

    def stop(self):
        self.active = False


class FakePlayback:
    def __init__(self):
        self._stream = None
        self.starts = 0
        self.writes = []
        self.stops = 0
        self.played = 0

    def start(self, initial_pcm=None):
        if self._stream is not None:
            raise AudioError("duplicate speaker")
        self._stream = object()
        self.starts += 1
        self.played = 0
        if initial_pcm is not None:
            self.write(initial_pcm)

    def write(self, frames, timeout=None):
        if self._stream is None:
            raise AudioError("speaker closed")
        self.writes.append(frames.copy())
        self.played += len(frames)

    def drain(self, timeout=None):
        if self._stream is None:
            raise AudioError("speaker closed")

    def played_frames(self):
        return self.played

    def stop(self):
        if self._stream is not None:
            self.stops += 1
        self._stream = None


class FakeAudio:
    def __init__(self):
        self.capture = FakeCapture()
        self.playback = FakePlayback()
        self.closed = False

    def selected(self, direction):
        assert direction == "output"
        return object()

    def close(self):
        self.capture.stop()
        self.playback.stop()
        self.closed = True


class FakeClient:
    def __init__(self):
        self.session_id = "sess_test"
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.connect_error = None
        self.cancelled = []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def receive(self):
        value = await self.queue.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def cancel_response(self, response_id):
        self.cancelled.append(response_id)

    async def close(self):
        self.closed = True


def make_voice(**options):
    audio, client = FakeAudio(), FakeClient()
    settings = Settings(audio_capture_rate=48000, audio_playback_rate=48000,
                        audio_capture_channels=1, audio_playback_channels=1,
                        audio_frame_format="int16", **options)
    transcripts = []
    latencies = []
    voice = VoiceConversation(settings, audio=audio, client=client,
                              on_transcript=transcripts.append, on_latency=latencies.append)
    client.queue.put_nowait(event("session.updated"))
    return voice, audio, client, transcripts, latencies


def pcm_audio(count=2400, value=1000):
    return base64.b64encode(np.full(count, value, dtype="<i2").tobytes()).decode()


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


def test_streaming_resample_frequency_and_alias_rejection():
    down = StreamingPCM(48000, 24000)
    samples = np.rint(10000 * np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000)).astype(np.int16)
    result = np.concatenate([down.convert(samples[i:i + 4800]) for i in range(0, len(samples), 4800)])
    assert len(result) == 24000
    dominant = np.argmax(np.abs(np.fft.rfft(result[1000:]))) * 24000 / len(result[1000:])
    assert abs(dominant - 1000) < 2
    high = np.rint(10000 * np.sin(2 * np.pi * 15000 * np.arange(48000) / 48000)).astype(np.int16)
    rejected = StreamingPCM(48000, 24000).convert(high)
    assert np.sqrt(np.mean(rejected[1000:].astype(float) ** 2)) < 100
    up = StreamingPCM(24000, 48000)
    rebuilt = np.concatenate([up.convert(result[i:i + 2400]) for i in range(0, len(result), 2400)])
    assert len(rebuilt) == 48000
    assert rebuilt.dtype == np.int16
    with pytest.raises(PCMRateError):
        StreamingPCM(44100, 24000)
    with pytest.raises(PCMRateError):
        down.convert(np.ones((100, 2), dtype=np.int16))


def test_voice_peak_limiter_preserves_gain_until_ceiling():
    limiter = StreamingPeakLimiter(1.4, 1.0)
    quiet = limiter.process(np.full(2400, 1000, dtype=np.int16))
    assert np.array_equal(quiet, np.full(2400, 1000, dtype=np.int16))
    loud = limiter.process(np.full(2400, 30000, dtype=np.int16))
    assert np.max(np.abs(loud.astype(np.int32) * 1.4)) <= 0.85 * 32768 + 2
    assert np.max(loud) < 30000


def test_complete_two_turn_conversation_and_streaming():
    async def scenario():
        voice, audio, client, transcripts, latencies = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active and audio.capture.reads >= 2)
        assert client.sent[0]["type"] == "session.update"
        vad = client.sent[0]["session"]["audio"]["input"]["turn_detection"]
        assert vad["type"] == "server_vad" and vad["create_response"] and vad["interrupt_response"]
        for number in (1, 2):
            client.queue.put_nowait(event("input_audio_buffer.speech_started", item=f"u{number}"))
            client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item=f"u{number}"))
            client.queue.put_nowait(event("conversation.item.input_audio_transcription.completed", item=f"u{number}", role="user", text=f"question {number}", final=True))
            client.queue.put_nowait(event("response.created", response=f"r{number}"))
            client.queue.put_nowait(event("response.output_audio.delta", response=f"r{number}", item=f"a{number}", audio=pcm_audio()))
            client.queue.put_nowait(event("response.output_audio_transcript.done", response=f"r{number}", item=f"a{number}", role="assistant", text=f"answer {number}", final=True))
            client.queue.put_nowait(event("response.done", response=f"r{number}", status="completed"))
            await wait_until(lambda: audio.playback.starts == number and audio.playback._stream is None)
        voice.stop_event.set()
        await task
        assert sum(x["type"] == "input_audio_buffer.append" for x in client.sent) >= 2
        assert len(audio.playback.writes) == 2
        assert all(len(x) == 4800 for x in audio.playback.writes)
        assert [(x.role, x.text) for x in transcripts] == [("user", "question 1"), ("assistant", "answer 1"),
                                                            ("user", "question 2"), ("assistant", "answer 2")]
        assert len(latencies) == 2 and all(x >= 0 for x in latencies)
        assert audio.closed and client.closed and not audio.capture.active
        assert audio.playback._stream is None
    asyncio.run(scenario())


def test_interruption_drops_stale_audio_and_truncates():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="old"))
        client.queue.put_nowait(event("response.output_audio.delta", response="old", item="old-item", audio=pcm_audio(value=1000)))
        await wait_until(lambda: len(audio.playback.writes) == 1)
        client.queue.put_nowait(event("input_audio_buffer.speech_started", item="user2"))
        await wait_until(lambda: any(x["type"] == "conversation.item.truncate" for x in client.sent))
        client.queue.put_nowait(event("response.output_audio.delta", response="old", item="old-item", audio=pcm_audio(value=2000)))
        client.queue.put_nowait(event("response.done", response="old", status="cancelled"))
        client.queue.put_nowait(event("input_audio_buffer.speech_stopped", item="user2"))
        client.queue.put_nowait(event("response.created", response="new"))
        client.queue.put_nowait(event("response.output_audio.delta", response="new", item="new-item", audio=pcm_audio(value=3000)))
        client.queue.put_nowait(event("response.done", response="new", status="completed"))
        await wait_until(lambda: audio.playback.starts == 2 and audio.playback._stream is None)
        voice.stop_event.set()
        await task
        assert len(audio.playback.writes) == 2
        assert audio.playback.writes[0][0, 0] != audio.playback.writes[1][0, 0]
        truncation = next(x for x in client.sent if x["type"] == "conversation.item.truncate")
        assert truncation["item_id"] == "old-item" and truncation["audio_end_ms"] >= 0
    asyncio.run(scenario())


def test_late_audio_after_response_done_is_ignored():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="r"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio()))
        client.queue.put_nowait(event("response.done", response="r", status="completed"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio(value=5000)))
        await wait_until(lambda: audio.playback.starts == 1 and audio.playback._stream is None)
        voice.stop_event.set()
        await task
        assert len(audio.playback.writes) == 1
    asyncio.run(scenario())


def test_partial_transcript_is_replaced_by_final():
    async def scenario():
        voice, _, _, final, _ = make_voice()
        await voice.handle_event(event("conversation.item.input_audio_transcription.delta", item="u", role="user", text="hel"))
        await voice.handle_event(event("conversation.item.input_audio_transcription.completed", item="u", role="user", text="hello", final=True))
        assert voice.transcripts[("user", "u", 0)].text == "hello"
        assert [x.text for x in final] == ["hello"]
        await voice.close()
    asyncio.run(scenario())


def test_failure_disconnect_and_restart_cleanup():
    async def scenario():
        for failure in (event("response.done", response="r", status="failed"),
                        RealtimeError("Realtime connection closed unexpectedly"),
                        event("error", status="error")):
            voice, audio, client, _, _ = make_voice()
            task = asyncio.create_task(voice.run())
            await wait_until(lambda: audio.capture.active)
            if isinstance(failure, ConversationEvent) and failure.kind == "response.done":
                client.queue.put_nowait(event("response.created", response="r"))
            client.queue.put_nowait(failure)
            with pytest.raises((VoiceError, RealtimeError)):
                await task
            assert audio.closed and client.closed and not audio.capture.active
            assert audio.playback._stream is None
        restarted, audio, client, _, _ = make_voice()
        task = asyncio.create_task(restarted.run())
        await wait_until(lambda: audio.capture.active)
        restarted.stop_event.set()
        await task
        assert audio.closed and client.closed
    asyncio.run(scenario())


def test_auth_failure_never_opens_audio():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        client.connect_error = RealtimeError("OpenAI authentication failed")
        with pytest.raises(RealtimeError, match="authentication"):
            await voice.run()
        assert audio.closed and client.closed and audio.capture.starts == 0
    asyncio.run(scenario())


def test_voice_ack_can_follow_informational_event():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        client.queue.get_nowait()
        client.queue.put_nowait(event("unknown"))
        client.queue.put_nowait(event("session.updated"))
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        voice.stop_event.set()
        await task
        assert audio.closed and client.closed
    asyncio.run(scenario())


def test_manual_reply_cancel_and_no_duplicate_speaker():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="r"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio()))
        await wait_until(lambda: audio.playback.starts == 1)
        await voice.cancel_reply()
        assert client.cancelled == ["r"]
        assert audio.playback._stream is None
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio()))
        await asyncio.sleep(0.01)
        assert audio.playback.starts == 1
        voice.stop_event.set()
        await task
    asyncio.run(scenario())


def test_cancel_during_full_output_queue_never_relabels_stale_chunks():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        await voice.handle_event(event("response.created", response="r"))
        incoming = asyncio.create_task(voice.handle_event(event(
            "response.output_audio.delta", response="r", item="i", audio=pcm_audio(count=24000))))
        await wait_until(lambda: voice._output.qsize() == 8)
        await voice.cancel_reply()
        await asyncio.wait_for(incoming, 1)
        assert all(entry[0] != voice._generation for entry in list(voice._output._queue))
        assert audio.playback.starts == 0
        await voice.close()
    asyncio.run(scenario())


def test_silence_closes_session_without_response(monkeypatch):
    import magic_mirror.assistant.voice as voice_module
    monkeypatch.setattr(voice_module, "SILENCE_SECONDS", 0)

    async def scenario():
        voice, audio, client, _, _ = make_voice()
        await voice.run()
        assert audio.closed and client.closed
        assert not any(x["type"] == "response.create" for x in client.sent)
    asyncio.run(scenario())


def test_wake_false_activation_uses_short_first_speech_limit():
    async def scenario():
        voice, audio, client, _, _ = make_voice()
        voice.first_speech_seconds = 0
        await voice.run()
        assert not voice.heard_speech and audio.closed and client.closed
        assert not any(x["type"] == "response.create" for x in client.sent)
    asyncio.run(scenario())


def test_speaker_failure_and_partial_response_cleanup():
    class FailedPlayback(FakePlayback):
        def start(self, initial_pcm=None):
            raise AudioError("speaker unavailable")

    async def scenario():
        voice, audio, client, _, _ = make_voice()
        audio.playback = FailedPlayback()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="r"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio()))
        with pytest.raises(AudioError, match="speaker unavailable"):
            await task
        assert audio.closed and client.closed and not audio.capture.active

        voice, audio, client, _, _ = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(event("response.created", response="r"))
        client.queue.put_nowait(event("response.output_audio.delta", response="r", item="i", audio=pcm_audio()))
        await wait_until(lambda: len(audio.playback.writes) == 1)
        client.queue.put_nowait(event("response.done", response="r", status="incomplete"))
        with pytest.raises(VoiceError, match="incomplete"):
            await task
        assert audio.closed and client.closed and audio.playback._stream is None
    asyncio.run(scenario())


def test_capture_failure_and_rate_limit_cleanup():
    class FailedCapture(FakeCapture):
        def read_frames(self, count):
            raise AudioError("microphone disconnected")

    async def scenario():
        voice, audio, client, _, _ = make_voice()
        audio.capture = FailedCapture()
        with pytest.raises(AudioError, match="microphone disconnected"):
            await voice.run()
        assert audio.closed and client.closed

        voice, audio, client, _, _ = make_voice()
        task = asyncio.create_task(voice.run())
        await wait_until(lambda: audio.capture.active)
        client.queue.put_nowait(ConversationEvent("error", ConversationState.READY, "sess_test",
                                                  error_code="rate_limit_exceeded"))
        with pytest.raises(VoiceError, match="rate_limit_exceeded"):
            await task
        assert audio.closed and client.closed
    asyncio.run(scenario())


def test_voice_cli_default_is_offline(capsys):
    assert cli_main([]) == 0
    assert "Offline" in capsys.readouterr().out
