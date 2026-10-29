"""Typed, content-free-by-default state tracking for GA Realtime server events."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ConversationState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    STOPPING = "stopping"
    ERROR = "error"


class PlaybackAction(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class PlaybackEvent:
    action: PlaybackAction
    response_id: str


@dataclass(frozen=True)
class ConversationEvent:
    kind: str
    state: ConversationState
    session_id: str | None = None
    response_id: str | None = None
    item_id: str | None = None
    event_id: str | None = None
    role: str | None = None
    text: str | None = None
    audio_base64: str | None = None
    final: bool = False
    status: str | None = None
    usage: dict[str, Any] | None = None
    error_code: str | None = None
    content_index: int | None = None


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass
class EventTracker:
    session_id: str | None = None
    state: ConversationState = ConversationState.DISCONNECTED
    response_started: dict[str, float] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    _final_transcripts: set[tuple[str, str, int]] = field(default_factory=set)

    def parse(self, raw: str | bytes | dict[str, Any], now: float) -> ConversationEvent:
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, UnicodeError):
            return ConversationEvent("invalid", self.state, self.session_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            return ConversationEvent("invalid", self.state, self.session_id)
        kind = payload["type"]
        event_id = _id(payload.get("event_id"))
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        response_id = _id(payload.get("response_id")) or _id(response.get("id"))
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        item_id = _id(payload.get("item_id")) or _id(item.get("id"))
        text = None
        audio = None
        final = False
        status = None
        usage = None
        role = None
        code = None
        content_index = None

        if kind in {"session.created", "session.updated"}:
            session = payload.get("session")
            if not isinstance(session, dict) or not _id(session.get("id")):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            if self.session_id and session["id"] != self.session_id:
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            self.session_id = session["id"]
            if kind == "session.updated":
                self.state = ConversationState.READY
        elif kind == "input_audio_buffer.speech_started":
            self.state = ConversationState.LISTENING
        elif kind == "input_audio_buffer.speech_stopped":
            self.state = ConversationState.PROCESSING
        elif kind == "response.created":
            if not response_id:
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            self.response_started[response_id] = now
            self.state = ConversationState.RESPONDING
        elif kind == "response.done":
            if not response_id or not isinstance(response.get("status"), str):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            self.response_started.pop(response_id, None)
            status = response["status"]
            if status not in {"completed", "cancelled", "failed", "incomplete"}:
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            if isinstance(response.get("usage"), dict):
                usage = {k: v for k, v in response["usage"].items()
                         if k in self.usage and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                for key, value in usage.items():
                    self.usage[key] += value
            details = response.get("status_details")
            if isinstance(details, dict) and isinstance(details.get("error"), dict):
                code = _id(details["error"].get("code")) or _id(details["error"].get("type"))
            self.state = ConversationState.RESPONDING if self.response_started else (ConversationState.ERROR if status == "failed" else ConversationState.READY)
        elif kind == "error":
            error = payload.get("error")
            if not isinstance(error, dict):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            code = _id(error.get("code")) or _id(error.get("type"))
            status = "error"
            # Server errors are often recoverable; the caller decides whether to close.
        elif kind in {"conversation.item.input_audio_transcription.delta",
                      "conversation.item.input_audio_transcription.completed",
                      "response.output_audio_transcript.delta", "response.output_audio_transcript.done",
                      "response.output_text.delta", "response.output_text.done"}:
            role = "user" if kind.startswith("conversation.item") else "assistant"
            if not item_id or (role == "assistant" and not response_id):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            field_name = "transcript" if kind.endswith(".completed") or kind.endswith("transcript.done") else ("text" if kind.endswith("text.done") else "delta")
            text = payload.get(field_name)
            if not isinstance(text, str):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            final = kind.endswith((".completed", ".done"))
            content_index = payload.get("content_index", 0)
            if not isinstance(content_index, int) or isinstance(content_index, bool) or content_index < 0:
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            key = (kind.split(".")[0] if role == "user" else kind.rsplit(".", 1)[0], item_id, content_index)
            if final:
                if key in self._final_transcripts:
                    return ConversationEvent("duplicate", self.state, self.session_id, response_id, item_id, event_id, role)
                self._final_transcripts.add(key)
            elif key in self._final_transcripts:
                return ConversationEvent("duplicate", self.state, self.session_id, response_id, item_id, event_id, role)
            self.state = ConversationState.LISTENING if role == "user" and not final else (ConversationState.PROCESSING if role == "user" else ConversationState.RESPONDING)
        elif kind == "response.output_audio.delta":
            if not response_id or not item_id or not isinstance(payload.get("delta"), str):
                return ConversationEvent("invalid", self.state, self.session_id, event_id=event_id)
            audio = payload["delta"]
            content_index = payload.get("content_index")
            role = "assistant"
            self.state = ConversationState.RESPONDING
        elif kind == "response.output_audio.done":
            role = "assistant"
            final = True
        elif kind in {"conversation.item.added", "conversation.item.done", "response.output_item.added", "response.output_item.done"}:
            role = _id(item.get("role"))
        else:
            return ConversationEvent("unknown", self.state, self.session_id, response_id, item_id, event_id)

        return ConversationEvent(kind, self.state, self.session_id, response_id, item_id,
                                 event_id, role, text, audio, final, status, usage, code,
                                 content_index)
