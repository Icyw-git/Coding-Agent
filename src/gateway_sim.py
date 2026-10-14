"""Local message-gateway simulator: drives the HTTP API like an IM bot would.

This is the transport layer of the Agent OS. It owns no agent logic — it only
translates REPL input into HTTP calls and renders the SSE live stream, so the
full chain (message → session turn → live events → task run/status/logs) can
be verified locally before wiring a real Telegram/Lark bot.

Usage (server must be running):
    set HARNESS_API_URL=http://127.0.0.1:8000
    set HARNESS_API_TOKEN=<token>
    python src/gateway_sim.py

REPL commands:
    /new [id]        create a session and switch to it
    /sessions        list persisted sessions
    /use <id>        switch the active session
    /stream          start/stop the SSE live event printer (background thread)
    /run <command>   create a server task (experiment / shell job)
    /status [id]     task status (all tasks when id omitted)
    /logs <id> [n]   tail a task's output
    /stop <id>       cancel a running task
    /quit            exit
    anything else    send as a chat message to the active session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import httpx


def format_event(event: dict) -> str:
    """Render one session event for the console stream."""
    kind = event.get('type', 'event')
    if kind == 'message':
        message = event.get('message') or {}
        role = message.get('role', '?')
        content = str(message.get('content', '')).replace('\n', ' ')[:120]
        return f'[live] {role}: {content}'
    if kind == 'tool_started':
        return f"[live] tool → {event.get('name', '?')}"
    if kind in ('turn_completed', 'session_created'):
        return f'[live] {kind} (seq={event.get("seq")})'
    return f'[live] {kind} (seq={event.get("seq")})'


class GatewayClient:
    """Thin httpx wrapper over the Harness HTTP API."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self.session_id: str | None = None
        self._stream_stop = threading.Event()
        self._stream_thread: threading.Thread | None = None

    # ---------- sessions ----------

    def create_session(self, session_id: str | None = None) -> str:
        response = self.http.post('/v1/sessions', json={'id': session_id})
        response.raise_for_status()
        self.session_id = response.json()['id']
        return self.session_id

    def list_sessions(self, limit: int = 20) -> list[dict]:
        response = self.http.get('/v1/sessions', params={'limit': limit})
        response.raise_for_status()
        return response.json()['sessions']

    def send(self, content: str) -> str:
        if not self.session_id:
            raise RuntimeError('no active session; use /new first')
        response = self.http.post(
            f'/v1/sessions/{self.session_id}/messages',
            json={'content': content},
        )
        response.raise_for_status()
        return response.json()['content']

    def start_stream(self) -> bool:
        """Follow the active session's SSE stream on a background thread."""
        if not self.session_id:
            print('  [stream] no active session; use /new first')
            return False
        if self._stream_thread and self._stream_thread.is_alive():
            self.stop_stream()
        self._stream_stop.clear()

        def consume() -> None:
            try:
                with self.http.stream(
                    'GET', f'/v1/sessions/{self.session_id}/stream',
                    params={'poll': 0.3},
                ) as response:
                    event_name = 'event'
                    for line in response.iter_lines():
                        if self._stream_stop.is_set():
                            break
                        if not line:
                            continue
                        if line.startswith('event:'):
                            event_name = line[6:].strip()
                        elif line.startswith('data:'):
                            try:
                                event = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            print(format_event(event))
                            event_name = 'event'
                        # ':' heartbeat comments are ignored
            except httpx.HTTPError as exc:
                if not self._stream_stop.is_set():
                    print(f'  [stream] disconnected: {exc}')

        self._stream_thread = threading.Thread(target=consume, daemon=True)
        self._stream_thread.start()
        return True

    def stop_stream(self) -> None:
        self._stream_stop.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=3)
            self._stream_thread = None

    # ---------- tasks ----------

    def create_task(self, command: str) -> dict:
        response = self.http.post('/v1/tasks', json={'command': command})
        response.raise_for_status()
        return response.json()['task']

    def list_tasks(self) -> list[dict]:
        response = self.http.get('/v1/tasks')
        response.raise_for_status()
        return response.json()['tasks']

    def get_task(self, task_id: str) -> dict:
        response = self.http.get(f'/v1/tasks/{task_id}')
        response.raise_for_status()
        return response.json()['task']

    def stop_task(self, task_id: str) -> dict:
        response = self.http.post(f'/v1/tasks/{task_id}/stop')
        response.raise_for_status()
        return response.json()['task']

    def task_logs(self, task_id: str, lines: int = 80) -> str:
        response = self.http.get(
            f'/v1/tasks/{task_id}/logs', params={'tail': lines})
        response.raise_for_status()
        return response.text


def _print_task(task: dict) -> None:
    started = time.strftime('%H:%M:%S', time.localtime(task['started_at'])) \
        if task.get('started_at') else '-'
    print(f"  {task['id']}  {task['status']:<10} exit={task.get('exit_code')} "
          f"start={started}  {task['command'][:60]}")


def repl(client: GatewayClient) -> int:
    print('gateway sim ready — /new /sessions /use /stream /run /status /logs '
          '/stop /quit, or type a message')
    while True:
        try:
            line = input('\n[gw] > ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if line.startswith('/'):
                parts = line.split(maxsplit=1)
                command = parts[0].lower()
                argument = parts[1].strip() if len(parts) > 1 else ''
                if command in ('/quit', '/exit'):
                    break
                elif command == '/new':
                    print(f'  session {client.create_session(argument or None)}')
                elif command == '/sessions':
                    for row in client.list_sessions():
                        print(f"  {row['id']}  events={row['events']}  "
                              f"{row['title']}")
                elif command == '/use':
                    client.session_id = argument
                    print(f'  active session: {argument}')
                elif command == '/stream':
                    if client.start_stream():
                        print(f"  [stream] following {client.session_id} "
                              f"(re-run to stop)")
                elif command == '/run':
                    if not argument:
                        print('  usage: /run <command>')
                        continue
                    _print_task(client.create_task(argument))
                elif command == '/status':
                    if argument:
                        _print_task(client.get_task(argument))
                    else:
                        for task in client.list_tasks():
                            _print_task(task)
                elif command == '/logs':
                    tokens = argument.split()
                    task_id = tokens[0] if tokens else ''
                    lines = int(tokens[1]) if len(tokens) > 1 else 80
                    print(client.task_logs(task_id, lines))
                elif command == '/stop':
                    print('  stopped' if client.stop_task(argument) else '')
                else:
                    print(f'  unknown command: {command}')
            else:
                reply = client.send(line)
                print(f'\nagent: {reply}')
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get('detail', '') \
                if exc.response.headers.get('content-type', '').startswith('application/json') \
                else exc.response.text[:200]
            print(f'  [error] {exc.response.status_code} {detail}')
        except (httpx.HTTPError, RuntimeError, KeyError) as exc:
            print(f'  [error] {exc}')
    client.stop_stream()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Harness message-gateway simulator')
    parser.add_argument('--url', default=os.getenv('HARNESS_API_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--token', default=os.getenv('HARNESS_API_TOKEN', ''))
    args = parser.parse_args(argv)
    if not args.token:
        print('HARNESS_API_TOKEN (--token) is required')
        return 1
    try:
        client = GatewayClient(args.url, args.token)
    except httpx.HTTPError as exc:
        print(f'cannot reach API at {args.url}: {exc}')
        return 1
    return repl(client)


if __name__ == '__main__':
    sys.exit(main())
