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

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        self.base_url = base_url.rstrip('/')
        # transport：注入 httpx.ASGITransport/MockTransport 供测试使用
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=httpx.Timeout(600.0, connect=10.0),
            transport=transport,
        )
        self.session_id: str | None = None
        self._stream_stop = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._stream_response: httpx.Response | None = None
        self._stream_lock = threading.Lock()

    @property
    def streaming(self) -> bool:
        return self._stream_thread is not None and self._stream_thread.is_alive()

    # ---------- sessions ----------

    def create_session(self, session_id: str | None = None, *, workspace: str | None = None) -> str:
        response = self.http.post('/v1/sessions', json={'id': session_id, 'workspace': workspace})
        response.raise_for_status()
        self.session_id = response.json()['id']
        return self.session_id

    def list_sessions(self, limit: int = 20) -> list[dict]:
        response = self.http.get('/v1/sessions', params={'limit': limit})
        response.raise_for_status()
        return response.json()['sessions']

    def get_workspace(self, session_id: str) -> str:
        response = self.http.get(f'/v1/sessions/{session_id}/workspace')
        response.raise_for_status()
        return response.json()['workspace']

    def set_workspace(self, session_id: str, path: str) -> str:
        response = self.http.put(f'/v1/sessions/{session_id}/workspace', json={'path': path})
        response.raise_for_status()
        return response.json()['workspace']

    def list_archives(self) -> list[dict]:
        response = self.http.get('/v1/session-archives')
        response.raise_for_status()
        return response.json()['sessions']

    def manage_session(self, action: str, row: dict) -> None:
        response = self.http.post('/v1/session-actions', json={'action': action, 'session': row})
        response.raise_for_status()

    def send(self, content: str, session_id: str | None = None) -> str:
        session_id = session_id or self.session_id
        if not session_id:
            raise RuntimeError('no active session; use /new first')
        response = self.http.post(
            f'/v1/sessions/{session_id}/messages',
            json={'content': content},
        )
        response.raise_for_status()
        return response.json()['content']

    def send_stream(self, content: str, session_id: str | None = None,
                    on_delta=None, on_progress=None) -> str:
        """Run a turn over the message SSE endpoint and return final text."""
        session_id = session_id or self.session_id
        if not session_id:
            raise RuntimeError('no active session; use /new first')
        final = ''
        self.last_duration_ms = None
        self.last_duration = None
        completed = False
        with self.http.stream('POST', f'/v1/sessions/{session_id}/messages/stream',
                              json={'content': content}) as response:
            response.raise_for_status()
            event = None
            for line in response.iter_lines():
                if line.startswith('event:'):
                    event = line[6:].strip()
                elif line.startswith('data:'):
                    payload = json.loads(line[5:].strip())
                    if event == 'delta':
                        text = payload.get('text', '')
                        final += text
                        if on_delta:
                            on_delta(text)
                    elif event == 'done':
                        final = payload.get('content', final)
                        self.last_duration_ms = payload.get('duration_ms')
                        self.last_duration = payload.get('duration')
                        completed = True
                    elif event == 'progress' and on_progress:
                        on_progress(payload)
                    elif event == 'error':
                        raise RuntimeError(payload.get('detail', 'stream failed'))
        if not completed:
            raise RuntimeError('回复流提前结束，任务状态未知；请先检查状态，避免重复执行。')
        return final

    def start_stream(self) -> bool:
        """Follow the active session's SSE stream on a background thread."""
        if not self.session_id:
            print('  [stream] no active session; use /new first')
            return False
        self.stop_stream()
        stop = threading.Event()
        self._stream_stop = stop
        session_id = self.session_id

        def consume() -> None:
            try:
                after = 0
                while not stop.is_set():
                    with self.http.stream(
                        'GET', f'/v1/sessions/{session_id}/stream',
                        params={'poll': 0.3, 'follow_seconds': 5, 'after': after},
                    ) as response:
                        response.raise_for_status()
                        with self._stream_lock:
                            if stop.is_set():
                                break
                            self._stream_response = response
                        for line in response.iter_lines():
                            if stop.is_set():
                                break
                            if line.startswith('data:'):
                                event = json.loads(line[5:].strip())
                                if 'seq' in event:
                                    after = event['seq']
                                    print(format_event(event))
                                elif 'detail' in event:
                                    print(f"  [stream] {event['detail']}")
                                    return
                        with self._stream_lock:
                            self._stream_response = None
                        if stop.is_set():
                            break
            except (httpx.HTTPError, httpx.StreamError, json.JSONDecodeError) as exc:
                if not stop.is_set():
                    print(f'  [stream] disconnected: {exc}')
            finally:
                with self._stream_lock:
                    self._stream_response = None

        self._stream_thread = threading.Thread(target=consume, daemon=True)
        self._stream_thread.start()
        return True

    def stop_stream(self) -> None:
        self._stream_stop.set()
        with self._stream_lock:
            response = self._stream_response
        if response is not None:
            response.close()
        if self._stream_thread:
            self._stream_thread.join(timeout=6)
            if self._stream_thread.is_alive():
                raise RuntimeError('previous stream is still closing; try again shortly')
            self._stream_thread = None

    def close(self) -> None:
        self.stop_stream()
        try:
            self.http.close()
        except AttributeError:
            pass   # 注入的测试传输（ASGITransport）无同步 close

    # ---------- tasks ----------

    def create_task(self, command: str, cwd: str | None = None) -> dict:
        payload = {'command': command}
        if cwd:
            payload['cwd'] = cwd
        response = self.http.post('/v1/tasks', json=payload)
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

    # ---------- 远程权限审批 ----------

    def pending_permissions(self) -> list[dict]:
        response = self.http.get('/v1/permissions')
        response.raise_for_status()
        return response.json()['pending']

    def resolve_permission(self, req_id: str, allow: bool,
                           scope: str | None = None,
                           klass: str | None = None) -> dict:
        body: dict = {'allow': allow}
        if scope:
            body['scope'] = scope
        if klass:
            body['klass'] = klass
        response = self.http.post(
            f'/v1/permissions/{req_id}/resolve', json=body)
        response.raise_for_status()
        return response.json()['request']


def _print_task(task: dict) -> None:
    started = time.strftime('%H:%M:%S', time.localtime(task['started_at'])) \
        if task.get('started_at') else '-'
    duration_ms = task.get('duration_ms')
    duration = f'{duration_ms / 1000:.1f}s' if duration_ms is not None else '-'
    print(f"  {task['id']}  {task['status']:<10} exit={task.get('exit_code')} "
          f"用时={duration}  start={started}  {task['command'][:60]}")


def repl(client: GatewayClient) -> int:
    print('gateway sim ready — /new /sessions /use /stream /run /status /logs '
          '/stop /perms /approve /quit, or type a message')
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
                    if client.streaming:
                        client.stop_stream()
                        print('  [stream] stopped')
                    elif client.start_stream():
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
                elif command == '/perms':
                    pending = client.pending_permissions()
                    if not pending:
                        print('  (no pending permission requests)')
                    for index, req in enumerate(pending, 1):
                        print(f"  #{index}  {req['tool']}  "
                              f"{req['reason'][:60]}")
                        print(f"    参数: {req.get('args', {})}")
                        print(f"    → /approve {index} y|n")
                elif command == '/approve':
                    tokens = argument.split()
                    if len(tokens) not in (1, 2) or tokens[-1] not in ('y', 'n'):
                        print('  用法: /approve [序号] y|n；/perms 查看待审批操作')
                        continue
                    pending = client.pending_permissions()
                    index = int(tokens[0]) - 1 if len(tokens) == 2 else 0
                    if not 0 <= index < len(pending):
                        print('  无对应的待审批操作，请用 /perms 查看')
                        continue
                    record = client.resolve_permission(
                        pending[index]['req_id'], tokens[-1] == 'y')
                    print(f"  {'已允许' if record['status'] == 'approved' else '已拒绝'} · {record['tool']}")
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
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
            print(f'  [error] {exc}')
    client.stop_stream()
    return 0


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv()   # 与 harness 共用项目根的 .env
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
    try:
        return repl(client)
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
