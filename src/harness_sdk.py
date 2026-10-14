"""Small Python SDK facade for the transport-independent agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RuntimeClient(Protocol):
    def create_session(self, session_id: str | None = None) -> str: ...
    def send(self, session_id: str, content: str): ...
    def history(self, session_id: str) -> list[dict]: ...
    def list_sessions(self, limit: int = 20) -> list[dict]: ...


@dataclass(frozen=True)
class Session:
    id: str
    history: list[dict]


@dataclass(frozen=True)
class MessageResponse:
    session_id: str
    content: str


class Sessions:
    def __init__(self, runtime: RuntimeClient) -> None:
        self._runtime = runtime

    def create(self, session_id: str | None = None) -> Session:
        identifier = self._runtime.create_session(session_id)
        return Session(id=identifier, history=[])

    def get(self, session_id: str) -> Session:
        return Session(id=session_id, history=self._runtime.history(session_id))

    def list(self, limit: int = 20) -> list[dict]:
        return self._runtime.list_sessions(limit)


class Messages:
    def __init__(self, runtime: RuntimeClient) -> None:
        self._runtime = runtime

    def create(self, session_id: str, content: str) -> MessageResponse:
        result = self._runtime.send(session_id, content)
        return MessageResponse(session_id=result.session_id, content=result.content)


class HarnessClient:
    """Python SDK client backed by an :class:`AgentRuntime` instance."""

    def __init__(self, runtime: RuntimeClient) -> None:
        self.sessions = Sessions(runtime)
        self.messages = Messages(runtime)


def create_default_client() -> HarnessClient:
    """Create a client using the application's configured runtime."""
    import harness

    return HarnessClient(harness.create_runtime())
