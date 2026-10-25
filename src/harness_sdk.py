"""Small Python SDK facade for the transport-independent agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RuntimeClient(Protocol):
    def create_session(self, session_id: str | None = None, *, workspace: str | None = None) -> str: ...
    def send(self, session_id: str, content: str, on_delta=None): ...
    def history(self, session_id: str) -> list[dict]: ...
    def list_sessions(self, limit: int = 20) -> list[dict]: ...
    def search_sessions(self, query: str, limit: int = 10) -> list[dict]: ...
    def fork_session(self, session_id: str, upto_seq: int | None = None) -> str: ...
    def rename_session(self, session_id: str, title: str) -> str: ...
    def get_workspace(self, session_id: str) -> str: ...
    def set_workspace(self, session_id: str, path: str) -> str: ...
    def list_workspaces(self, current: str | None = None) -> dict: ...
    def add_workspace(self, path: str, name: str | None = None) -> dict: ...
    def remove_workspace(self, workspace_id: str) -> dict: ...


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

    def create(self, session_id: str | None = None, *, workspace: str | None = None) -> Session:
        identifier = (self._runtime.create_session(session_id) if workspace is None
                      else self._runtime.create_session(session_id, workspace=workspace))
        return Session(id=identifier, history=[])

    def get(self, session_id: str) -> Session:
        return Session(id=session_id, history=self._runtime.history(session_id))

    def list(self, limit: int = 20) -> list[dict]:
        return self._runtime.list_sessions(limit)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        return self._runtime.search_sessions(query, limit)

    def fork(self, session_id: str, upto_seq: int | None = None) -> str:
        return self._runtime.fork_session(session_id, upto_seq)

    def rename(self, session_id: str, title: str) -> str:
        return self._runtime.rename_session(session_id, title)

    def workspace(self, session_id: str) -> str:
        return self._runtime.get_workspace(session_id)

    def change_workspace(self, session_id: str, path: str) -> str:
        return self._runtime.set_workspace(session_id, path)


class Workspaces:
    def __init__(self, runtime: RuntimeClient) -> None:
        self._runtime = runtime

    def list(self, current: str | None = None) -> dict:
        return self._runtime.list_workspaces(current)

    def add(self, path: str, name: str | None = None) -> dict:
        return self._runtime.add_workspace(path, name)

    def remove(self, workspace_id: str) -> dict:
        return self._runtime.remove_workspace(workspace_id)


class Messages:
    def __init__(self, runtime: RuntimeClient) -> None:
        self._runtime = runtime

    def create(self, session_id: str, content: str, on_delta=None,
               origin_chat: str | None = None) -> MessageResponse:
        kwargs = {}
        if on_delta is not None:
            kwargs['on_delta'] = on_delta
        if origin_chat:
            kwargs['origin_chat'] = origin_chat
        result = self._runtime.send(session_id, content, **kwargs)
        return MessageResponse(session_id=result.session_id, content=result.content)


class HarnessClient:
    """Python SDK client backed by an :class:`AgentRuntime` instance."""

    def __init__(self, runtime: RuntimeClient) -> None:
        self.sessions = Sessions(runtime)
        self.workspaces = Workspaces(runtime)
        self.messages = Messages(runtime)


def create_default_client() -> HarnessClient:
    """Create a client using the application's configured runtime."""
    import harness

    return HarnessClient(harness.create_runtime())
