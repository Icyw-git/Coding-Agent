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
import ipaddress
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse

import httpx

import approval_view
from turn_progress import relative_time


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


def _is_loopback(base_url: str) -> bool:
    """API 在本机时不要走系统代理。

    httpx 的 trust_env 在 Windows 上会读注册表里的系统代理；本机正跑着代理
    （例如 127.0.0.1:10090）时，连 127.0.0.1:8000 也会被转发，SSE 往往直接
    502/被缓冲。loopback 一律直连，远端 API 仍尊重环境/系统代理配置。
    """
    host = (urlparse(base_url).hostname or '').strip().lower()
    if host in ('localhost', '::1'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
            trust_env=not _is_loopback(self.base_url),
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

    # ---------- crons ----------

    @staticmethod
    def _json(response, action: str) -> dict:
        """把服务端 4xx 的 detail 透出来，而不是只给 httpx 的通用文案。"""
        if response.status_code >= 400:
            try:
                detail = response.json().get('detail')
            except ValueError:
                detail = response.text
            raise RuntimeError(f'{action} failed ({response.status_code}): {detail}')
        return response.json()

    def list_crons(self) -> list[dict]:
        return self._json(self.http.get('/v1/crons'), 'list crons')['jobs']

    def create_cron(self, cron: str, prompt: str, *,
                    recurring: bool = True, durable: bool = True,
                    job_id: str | None = None,
                    allow_side_effects: bool = False,
                    deliver_to: str = '',
                    idle_after_minutes: int = 0) -> dict:
        payload = {'cron': cron, 'prompt': prompt,
                   'recurring': recurring, 'durable': durable,
                   'allow_side_effects': allow_side_effects}
        if job_id:
            payload['id'] = job_id
        if deliver_to:
            payload['deliver_to'] = deliver_to
        if idle_after_minutes:
            payload['idle_after_minutes'] = idle_after_minutes
        return self._json(self.http.post('/v1/crons', json=payload), 'create cron')

    def update_cron(self, job_id: str, *, allow_side_effects: bool | None = None,
                    deliver_to: str | None = None,
                    idle_after_minutes: int | None = None) -> dict:
        payload = {}
        if allow_side_effects is not None:
            payload['allow_side_effects'] = allow_side_effects
        if deliver_to is not None:
            payload['deliver_to'] = deliver_to
        if idle_after_minutes is not None:
            payload['idle_after_minutes'] = idle_after_minutes
        return self._json(self.http.patch(f'/v1/crons/{job_id}', json=payload),
                          'update cron')

    def pending_cron_results(self) -> list[dict]:
        return self._json(self.http.get('/v1/cron-results'),
                          'list cron results')['pending']

    def ack_cron_result(self, item_id: str) -> dict:
        return self._json(self.http.post(f'/v1/cron-results/{item_id}/ack'),
                          'ack cron result')

    def delete_cron(self, job_id: str) -> dict:
        return self._json(self.http.delete(f'/v1/crons/{job_id}'), 'delete cron')

    def interrupt(self, session_id: str) -> dict:
        return self._json(self.http.post(f'/v1/sessions/{session_id}/interrupt'),
                          'interrupt')

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

    def list_workspaces(self, current: str | None = None) -> dict:
        params = {'current': current} if current else None
        response = self.http.get('/v1/workspaces', params=params)
        response.raise_for_status()
        return response.json()

    def add_workspace(self, path: str, name: str | None = None) -> dict:
        response = self.http.post('/v1/workspaces', json={'path': path, 'name': name})
        response.raise_for_status()
        return response.json()['workspace']

    def remove_workspace(self, workspace_id: str) -> dict:
        response = self.http.delete(f'/v1/workspaces/{workspace_id}')
        response.raise_for_status()
        return response.json()['removed']

    def list_archives(self) -> list[dict]:
        response = self.http.get('/v1/session-archives')
        response.raise_for_status()
        return response.json()['sessions']

    def manage_session(self, action: str, row: dict) -> None:
        response = self.http.post('/v1/session-actions', json={'action': action, 'session': row})
        response.raise_for_status()

    def send(self, content: str, session_id: str | None = None,
             origin_chat: str | None = None, on_screenshots=None) -> str:
        session_id = session_id or self.session_id
        if not session_id:
            raise RuntimeError('no active session; use /new first')
        payload = {'content': content}
        if origin_chat:
            payload['origin_chat'] = origin_chat
        response = self.http.post(
            f'/v1/sessions/{session_id}/messages',
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        shots = data.get('screenshots') or []
        if shots and on_screenshots:
            on_screenshots(shots)
        return data['content']

    def send_stream(self, content: str, session_id: str | None = None,
                    on_delta=None, on_progress=None,
                    origin_chat: str | None = None,
                    on_screenshots=None) -> str:
        """Run a turn over the message SSE endpoint and return final text."""
        session_id = session_id or self.session_id
        if not session_id:
            raise RuntimeError('no active session; use /new first')
        payload = {'content': content}
        if origin_chat:
            payload['origin_chat'] = origin_chat
        final = ''
        self.last_duration_ms = None
        self.last_duration = None
        completed = False
        with self.http.stream('POST', f'/v1/sessions/{session_id}/messages/stream',
                              json=payload) as response:
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
                        shots = payload.get('screenshots') or []
                        if shots and on_screenshots:
                            on_screenshots(shots)
                        completed = True
                    elif event == 'progress' and on_progress:
                        on_progress(payload)
                    elif event == 'error':
                        raise RuntimeError(payload.get('detail', 'stream failed'))
        if not completed:
            raise RuntimeError('回复流提前结束，任务状态未知；请先检查状态，避免重复执行。')
        return final

    def download_screenshot(self, session_id: str, name: str) -> bytes:
        response = self.http.get(
            f'/v1/sessions/{session_id}/screenshots/{name}')
        response.raise_for_status()
        return response.content

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

    # ---------- 心跳摘要（至少一次投递） ----------

    def pending_heartbeat(self) -> list[dict]:
        response = self.http.get('/v1/heartbeat')
        response.raise_for_status()
        return response.json()['pending']

    def ack_heartbeat(self, occurrence_id: str) -> dict:
        from urllib.parse import quote
        response = self.http.post(
            f'/v1/heartbeat/{quote(occurrence_id, safe="")}/ack')
        response.raise_for_status()
        return response.json()

    def search_sessions(self, q: str, limit: int = 10) -> list[dict]:
        response = self.http.get('/v1/sessions/search',
                                 params={'q': q, 'limit': limit})
        response.raise_for_status()
        return response.json()['sessions']

    def fork_session(self, session_id: str, upto_seq: int | None = None) -> str:
        response = self.http.post(f'/v1/sessions/{session_id}/fork',
                                  json={'upto_seq': upto_seq})
        response.raise_for_status()
        return response.json()['session_id']

    def rename_session(self, session_id: str, title: str) -> str:
        response = self.http.post(f'/v1/sessions/{session_id}/title',
                                  json={'title': title})
        response.raise_for_status()
        return response.json()['title']

    def resolve_permission(self, req_id: str, allow: bool,
                           scope: str | None = None) -> dict:
        """决议一个审批请求。只发意图（allow / 是否扩到同类），
        「同类」的键由服务端按被审批的 tool/args 重算，客户端不传。"""
        body: dict = {'allow': allow}
        if scope:
            body['scope'] = scope
        response = self.http.post(
            f'/v1/permissions/{req_id}/resolve', json=body)
        response.raise_for_status()
        return response.json()['request']

    def revoke_permissions(self, session_id: str | None = None) -> dict:
        """撤销会话预授权；服务端同时收回该会话创建的目录级持久授权。"""
        response = self.http.post('/v1/permissions/revoke',
                                  json={'session_id': session_id})
        response.raise_for_status()
        data = response.json()
        return {'revoked': int(data.get('revoked', 0)),
                'grants_revoked': int(data.get('grants_revoked', 0)),
                'grant_error': str(data.get('grant_error') or '')}

    # ---------- 目录级持久授权 ----------

    def list_grants(self) -> list[dict]:
        return self._json(self.http.get('/v1/grants'), 'list grants')['grants']

    def add_grant(self, path: str, *, expires: str | None = None,
                  note: str = '', session_id: str | None = None,
                  by: str = 'feishu') -> dict:
        payload: dict = {'path': path, 'note': note, 'by': by}
        if expires:
            payload['expires'] = expires
        if session_id:
            payload['session_id'] = session_id
        return self._json(self.http.post('/v1/grants', json=payload),
                          'add grant')

    def remove_grant(self, path: str, *, session_id: str | None = None,
                     by: str = 'feishu') -> dict:
        payload: dict = {'path': path, 'by': by}
        if session_id:
            payload['session_id'] = session_id
        return self._json(self.http.post('/v1/grants/remove', json=payload),
                          'remove grant')['removed']

    # ---------- 域名策略 ----------

    def list_domains(self) -> list[dict]:
        return self._json(self.http.get('/v1/net-grants'), 'list domains')['domains']

    def add_domain(self, host: str, *, scope: str = 'allow',
                   expires: str | None = None, note: str = '',
                   session_id: str | None = None, by: str = 'feishu') -> dict:
        payload: dict = {'host': host, 'scope': scope, 'note': note, 'by': by}
        if expires:
            payload['expires'] = expires
        if session_id:
            payload['session_id'] = session_id
        return self._json(self.http.post('/v1/net-grants', json=payload),
                          'add domain')

    def remove_domain(self, host: str, *, session_id: str | None = None,
                      by: str = 'feishu') -> dict:
        payload: dict = {'host': host, 'by': by}
        if session_id:
            payload['session_id'] = session_id
        return self._json(self.http.post('/v1/net-grants/remove', json=payload),
                          'remove domain')['removed']

    # ---------- 审计 ----------

    def list_audit(self, *, limit: int = 50, types=None,
                   since: str | None = None) -> dict:
        params: dict = {'limit': limit}
        if types:
            params['types'] = ','.join(types) if not isinstance(types, str) else types
        if since:
            params['since'] = since
        return self._json(self.http.get('/v1/audit', params=params), 'list audit')

    # ---------- 执行档位 ----------

    def get_policy(self) -> dict:
        response = self.http.get('/v1/policy')
        response.raise_for_status()
        return response.json()['policy']

    def set_policy(self, sandbox_mode: str | None = None,
                   approval_policy: str | None = None) -> dict:
        """None 表示这一档不改；非法档名 → 400，由调用方显示可选值。"""
        response = self.http.post('/v1/policy', json={
            'sandbox_mode': sandbox_mode, 'approval_policy': approval_policy})
        response.raise_for_status()
        return response.json()['policy']


_SANDBOX_USAGE = ('  usage: /sandbox [read-only|workspace-write|danger-full-access]'
                  ' 或 /sandbox approvals [untrusted|on-request|never]')


def _print_policy(policy: dict) -> None:
    print(f"  sandbox={policy['sandbox_mode']} · approvals={policy['approval_policy']}"
          '（策略级判定，非 OS 沙箱）')


def _print_task(task: dict) -> None:
    from turn_progress import format_duration
    started = time.strftime('%H:%M:%S', time.localtime(task['started_at'])) \
        if task.get('started_at') else '-'
    duration_ms = task.get('duration_ms')
    if duration_ms is None:
        if not task.get('started_at'):
            duration = '排队中' if task.get('status') == 'queued' else '-'
        else:
            # 兼容旧记录：无 duration_ms 时按 started_at/ended_at 动态计算
            end = task.get('ended_at') or time.time()
            duration_ms = round((end - task['started_at']) * 1000)
    if duration_ms is not None:
        duration = format_duration(max(0, duration_ms) / 1000)
        if not task.get('ended_at'):
            duration += '（进行中）'
    print(f"  {task['id']}  {task['status']:<10} exit={task.get('exit_code')} "
          f"总用时={duration}  start={started}  {task['command'][:60]}")


def repl(client: GatewayClient) -> int:
    print('gateway sim ready — /new /sessions /use /stream /run /status /logs '
          '/stop /perms /approve /sandbox /quit, or type a message')
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
                        # 与 CLI 口径一致：主题优先 + 相对时间；id 只留 12 位前缀供 /use
                        print(f"  {row['id'][:12]}  {row['title']}"
                              f"  ·  {relative_time(row.get('updated'))}")
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
                elif command == '/sandbox':
                    tokens = [token.casefold() for token in argument.split()]
                    if not tokens:
                        _print_policy(client.get_policy())
                    elif tokens[0] == 'approvals' and len(tokens) == 2:
                        _print_policy(client.set_policy(approval_policy=tokens[1]))
                    elif tokens[0] != 'approvals' and len(tokens) == 1:
                        _print_policy(client.set_policy(sandbox_mode=tokens[0]))
                    else:
                        # 档名是否合法交给服务端判（400 里带可选值清单），这里只管形状
                        print(_SANDBOX_USAGE)
                elif command == '/perms':
                    pending = client.pending_permissions()
                    if not pending:
                        print('  (no pending permission requests)')
                    for index, req in enumerate(pending, 1):
                        tool = str(req['tool'])
                        print(f"  #{index}  {tool}  "
                              f"{approval_view.safe_text(req['reason'])[:60]}")
                        # 决议要看的就是这条命令，参数摘要不能替人做决定
                        detail = approval_view.detail(tool, req.get('args') or {})
                        for line in detail.splitlines():
                            print(f'        {line}')
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

    import console

    console.harden_stdio()   # 模拟器也会 print 回复正文（可能含 emoji）
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
