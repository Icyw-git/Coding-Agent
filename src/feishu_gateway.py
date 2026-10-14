"""飞书消息网关：长连接收消息 → Agent HTTP API → 回复到聊天。

是纯传输层适配器：不持有 agent 逻辑，只做 消息 ↔ HTTP 的翻译，与
gateway_sim 共用同一套端点契约（sessions / tasks / permissions）。

运行前准备（飞书开放平台 https://open.feishu.cn）：
    1. 创建企业自建应用，开通「机器人」能力
    2. 事件订阅方式选择「使用长连接接收事件」（无需公网 IP）
    3. 订阅事件：接收消息 im.message.receive_v1
    4. 开通权限：im:message（以应用身份发消息）、im:message.group_at_msg 等

依赖（仅本网关需要）：
    pip install -r requirements-feishu.txt

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET   应用凭证
    FEISHU_ALLOWED_USERS                用户白名单（open_id，逗号分隔）；
                                        不配置则任何能 @ 到机器人的人都可触发
    HARNESS_API_URL / HARNESS_API_TOKEN Agent API 地址与令牌

聊天命令（@机器人 发送）：
    /new [id] /sessions /cd [dir] /run <cmd> /status [id] /logs <id> /stop <id>
    /perms /approve [req_id] y|n /quit(解绑)
    y / n          —— 快捷决议最早的待审批请求（无需输 ID）
    /cd <dir>      —— 切换该聊天的任务执行目录（/run 生效）
    其余文本 → 作为消息发给绑定的 agent 会话
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

import httpx

from gateway_sim import GatewayClient

BINDINGS_PATH = Path(os.getenv('FEISHU_BINDINGS', '.cache/feishu_bindings.json'))
PERMISSION_POLL_INTERVAL = 3.0
DISPATCH_WORKERS = 4   # 消息分发线程数：同一聊天内保序，不同聊天并行


def format_permission_prompt(req: dict) -> str:
    """审批提醒文案：人工只需回复 /approve <id> y|n。"""
    preview = str(req.get('args', ''))[:160]
    return (f"⚠ 需要审批 [{req['req_id']}]\n"
            f"工具: {req['tool']}\n"
            f"原因: {req['reason']}\n"
            f"参数: {preview}\n"
            f"回复 /approve {req['req_id']} y（允许）或 n（拒绝）")


CARD_MD_LIMIT = 24000   # 飞书卡片 JSON 上限约 30KB，留余量


def markdown_to_card_md(md_text: str) -> str:
    """标准 Markdown → 飞书卡片 lark_md（子集）。

    message 卡片的 markdown 元素不支持标题/水平线/无序列表标记，
    转成卡片支持的加粗行、符号列表等近似样式；代码块原样保留。
    """
    lines = md_text.splitlines()
    out = []
    in_code = False
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:   # 标题 → 加粗行
            out.append(f'{indent}**{m.group(2).strip()}**')
            continue
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):   # 水平线 → 空行
            out.append('')
            continue
        m = re.match(r'^[-*+]\s+(.*)$', stripped)
        if m:   # 无序列表 → 符号列表
            out.append(f'{indent}• {m.group(1)}')
            continue
        out.append(line)
    return '\n'.join(out)


class LarkTransport:
    """飞书消息发送传输层：im/v1/message.create（text / interactive 卡片）。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark
        self._client = lark.Client.builder() \
            .app_id(app_id).app_secret(app_secret).build()

    def send_text(self, chat_id: str, text: str) -> str | None:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        request = CreateMessageRequest.builder() \
            .receive_id_type('chat_id') \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type('text')
                .content(json.dumps({'text': text}, ensure_ascii=False))
                .build()
            ).build()
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f'feishu send failed: {response.code} {response.msg}')
        data = getattr(response, 'data', None)
        return getattr(data, 'message_id', None) if data else None

    def update_text(self, message_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import UpdateMessageRequest, UpdateMessageRequestBody
        request = UpdateMessageRequest.builder().message_id(message_id).request_body(
            UpdateMessageRequestBody.builder()
            .content(json.dumps({'text': text}, ensure_ascii=False)).build()
        ).build()
        response = self._client.im.v1.message.update(request)
        if not response.success():
            raise RuntimeError(f'feishu update failed: {response.code} {response.msg}')

    def send_markdown(self, chat_id: str, md_text: str) -> None:
        """以交互卡片发送 markdown——飞书 text 消息不解析任何 markdown 语法，
        卡片的 markdown 元素才支持粗体/斜体/行内代码/链接。"""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        content = markdown_to_card_md(md_text)
        if len(content) > CARD_MD_LIMIT:
            content = content[:CARD_MD_LIMIT] + '\n…[内容过长已截断]'
        card = {
            'config': {'wide_screen_mode': True},
            'elements': [{'tag': 'markdown', 'content': content}],
        }
        request = CreateMessageRequest.builder() \
            .receive_id_type('chat_id') \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type('interactive')
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            ).build()
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f'feishu card send failed: {response.code} {response.msg}')


class FeishuGateway:
    """核心编排：聊天绑定、命令分发、权限轮询。传输层可注入以便测试。"""

    def __init__(self, api: GatewayClient, transport=None,
                 bindings_path: Path | None = None,
                 allowed_users: set[str] | None = None) -> None:
        self.api = api
        self.transport = transport          # 需实现 send_text(chat_id, text)
        self.bindings_path = Path(bindings_path or BINDINGS_PATH)
        self.bindings: dict[str, str] = {}   # chat_id → session_id
        self._bindings_lock = threading.RLock()
        # 用户白名单（飞书 open_id）：None=不限制；配置后仅名单内用户可触发 agent。
        # 机器人一旦被拉进群，群内任何人都能 @ 它——真 shell 工具必须有这道闸。
        self.allowed_users = set(allowed_users) if allowed_users else None
        self._load_bindings()
        self._notified: set[str] = set()
        self._poll_stop = threading.Event()
        # 每聊天的待审批 FIFO：收到审批提醒后直接回复 y/n 即可，免输 req_id
        self._chat_pending: dict[str, list[str]] = {}
        # 每聊天的任务工作目录（/cd 设置，/run 使用；对话 agent 的工作区不受影响）
        self._chat_cwd: dict[str, str] = {}
        # WS 事件解耦：长 turn 的 send 会阻塞回调线程数分钟，必须丢进队列由
        # worker 池消费；同一聊天的消息用锁保序，不同聊天并行。
        self._events: queue.Queue = queue.Queue(maxsize=1000)
        self._chat_locks: dict[str, threading.Lock] = {}
        self._chat_locks_guard = threading.Lock()
        self._dispatch_stop = threading.Event()
        self._dispatch_threads: list[threading.Thread] = []

    # ---------- 绑定持久化 ----------

    def _load_bindings(self) -> None:
        if self.bindings_path.exists():
            try:
                loaded = json.loads(self.bindings_path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict) and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in loaded.items()):
                    self.bindings = loaded
            except (OSError, json.JSONDecodeError, TypeError):
                self.bindings = {}

    def _save_bindings(self) -> None:
        with self._bindings_lock:
            snapshot = dict(self.bindings)
            self.bindings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.bindings_path.with_suffix('.tmp')
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                            encoding='utf-8')
            os.replace(temp, self.bindings_path)

    # ---------- 消息入口（可测试核心） ----------

    def on_message(self, chat_id: str, text: str,
                   sender_id: str | None = None) -> None:
        """飞书文本消息入口。命令本地处理；普通文本转发给 agent 会话。"""
        text = (text or '').strip()
        if not text:
            return
        if self.allowed_users is not None and sender_id not in self.allowed_users:
            # 未授权用户：静默忽略（不回执、不建会话），避免暴露机器人行为
            print(f'[feishu] ignored message from unauthorized user '
                  f'{sender_id!r} in chat {chat_id!r}')
            return
        # y/n 快捷审批：该聊天有待审批请求时，y/n 不再当作普通消息
        if text.lower() in ('y', 'n') and self._chat_pending.get(chat_id):
            self._resolve_chat_pending(chat_id, text.lower() == 'y')
            return
        session_id = self.bindings.get(chat_id)
        if text.startswith('/'):
            self._handle_command(chat_id, text)
            return
        if not session_id:
            session_id = self.api.create_session()
            with self._bindings_lock:
                self.bindings[chat_id] = session_id
                self._save_bindings()
            self._reply(chat_id, f'已创建会话 {session_id}')
        try:
            self._send_agent_reply(chat_id, session_id, text)
        except Exception as exc:
            self._reply(chat_id, f'[error] {exc}')

    def _send_agent_reply(self, chat_id: str, session_id: str, text: str) -> None:
        """Stream into one editable text message when the transport supports it."""
        updater = getattr(self.transport, 'update_text', None)
        message_id = None
        parts = []
        last_update = 0.0

        def on_delta(part: str) -> None:
            nonlocal message_id, last_update
            parts.append(part)
            now = time.monotonic()
            if updater and now - last_update >= 0.6:
                if message_id is None:
                    message_id = self.transport.send_text(chat_id, ''.join(parts) or '生成中…')
                if message_id:
                    updater(message_id, ''.join(parts))
                    last_update = now

        try:
            reply = self.api.send_stream(text, session_id=session_id, on_delta=on_delta)
        except (AttributeError, NotImplementedError):
            reply = self.api.send(text, session_id=session_id)
        if message_id and updater:
            updater(message_id, reply)
        else:
            self._reply(chat_id, reply)

    # ---------- 快捷审批 ----------

    def _resolve_chat_pending(self, chat_id: str, allow: bool) -> None:
        """决议该聊天最早的待审批请求（免输 req_id）。"""
        pending_ids = self._chat_pending.get(chat_id) or []
        while pending_ids:
            req_id = pending_ids.pop(0)
            try:
                record = self.api.resolve_permission(req_id, allow)
            except Exception:
                continue   # 已决议/超时的请求：跳过，试下一个
            self._reply(chat_id, f"{record['req_id']} → {record['status']}")
            return
        self._reply(chat_id, '(没有可决议的待审批请求，试试 /perms 查看)')

    def _handle_command(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ''
        try:
            if command == '/new':
                session_id = self.api.create_session(argument or None)
                with self._bindings_lock:
                    self.bindings[chat_id] = session_id
                    self._save_bindings()
                self._reply(chat_id, f'已创建并绑定会话 {session_id}')
            elif command == '/sessions':
                rows = self.api.list_sessions(limit=10)
                lines = [f"{row['id'][:12]}  events={row['events']}  "
                         f"{row['title']}" for row in rows]
                self._reply(chat_id, '\n'.join(lines) or '(no sessions)')
            elif command == '/cd':
                if not argument:
                    current = self._chat_cwd.get(chat_id, '(默认：服务器工作区)')
                    self._reply(chat_id, f'当前任务目录: {current}')
                    return
                self._chat_cwd[chat_id] = argument
                self._reply(chat_id, f'任务目录已切换（下次 /run 生效，'
                              f'由服务器校验）: {argument}')
            elif command == '/run':
                if not argument:
                    self._reply(chat_id, '用法: /run <命令>')
                    return
                task = self.api.create_task(
                    argument, cwd=self._chat_cwd.get(chat_id))
                where = f"\n目录: {task['cwd']}" if task.get('cwd') else ''
                self._reply(chat_id, f"任务已创建 {task['id']} "
                              f"({task['status']}){where}")
            elif command == '/status':
                if argument:
                    task = self.api.get_task(argument)
                    self._reply(chat_id, f"{task['id']} {task['status']} "
                                          f"exit={task.get('exit_code')}")
                else:
                    lines = [f"{t['id']} {t['status']} exit={t.get('exit_code')}"
                             for t in self.api.list_tasks()[:10]]
                    self._reply(chat_id, '\n'.join(lines) or '(no tasks)')
            elif command == '/logs':
                tokens = argument.split()
                logs = self.api.task_logs(
                    tokens[0], int(tokens[1]) if len(tokens) > 1 else 60)
                self._reply(chat_id, logs or '(no output)')
            elif command == '/stop':
                task = self.api.stop_task(argument)
                self._reply(chat_id, f"任务 {task['id']} → {task['status']}")
            elif command == '/perms':
                pending = self.api.pending_permissions()
                if not pending:
                    self._reply(chat_id, '(无待审批请求)')
                for req in pending:
                    self._reply(chat_id, format_permission_prompt(req))
            elif command == '/approve':
                tokens = argument.split()
                if tokens and tokens[-1] in ('y', 'n') and len(tokens) == 1:
                    # /approve y|n —— 决议本聊天最早的待审批请求
                    self._resolve_chat_pending(chat_id, tokens[0] == 'y')
                    return
                if len(tokens) != 2 or tokens[1] not in ('y', 'n'):
                    self._reply(chat_id, '用法: /approve [req_id] y|n'
                                          '\n（省略 req_id 时决议最早的一条）')
                    return
                record = self.api.resolve_permission(tokens[0], tokens[1] == 'y')
                self._reply(chat_id, f"{record['req_id']} → {record['status']}")
            elif command == '/quit':
                with self._bindings_lock:
                    self.bindings.pop(chat_id, None)
                    self._save_bindings()
                self._chat_cwd.pop(chat_id, None)
                self._reply(chat_id, '会话绑定已解除')
            else:
                self._reply(chat_id, f'未知命令: {command}')
        except Exception as exc:
            self._reply(chat_id, f'[error] {exc}')

    # ---------- 权限轮询（推送给绑定聊天） ----------

    def poll_permissions(self) -> None:
        """把新出现的 pending 请求推给所有绑定的聊天（由定时线程调用）。"""
        try:
            pending = self.api.pending_permissions()
        except Exception:
            return
        chats = list(self.bindings)
        for req in pending:
            req_id = req['req_id']
            if req_id in self._notified:
                continue
            self._notified.add(req_id)
            for chat_id in chats:
                fifo = self._chat_pending.setdefault(chat_id, [])
                if req_id not in fifo:
                    fifo.append(req_id)
                self._reply(chat_id, format_permission_prompt(req))
        # 决议完成（或超时）的请求退出通知集合与各聊天 FIFO，避免无界增长
        pending_ids = {req['req_id'] for req in pending}
        self._notified &= pending_ids
        for chat_id, fifo in self._chat_pending.items():
            fifo[:] = [req_id for req_id in fifo if req_id in pending_ids]

    def start_permission_poller(self) -> threading.Thread:
        def loop() -> None:
            while not self._poll_stop.wait(PERMISSION_POLL_INTERVAL):
                self.poll_permissions()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._poll_stop.set()
        self._dispatch_stop.set()
        for _ in self._dispatch_threads:
            try:
                self._events.put_nowait(None)
            except queue.Full:
                break
        for thread in self._dispatch_threads:
            thread.join(timeout=3)
        self._dispatch_threads.clear()

    # ---------- WS 事件分发（长任务不阻塞回调线程） ----------

    def dispatch(self, chat_id: str, text: str,
                 sender_id: str | None = None) -> None:
        """把飞书事件投递到分发队列（回调线程立即返回）。"""
        try:
            self._events.put_nowait((chat_id, text, sender_id))
        except queue.Full:
            self._reply(chat_id, '当前消息较多，请稍后重试')

    def start_dispatcher(self, workers: int = DISPATCH_WORKERS) -> None:
        if self._dispatch_threads:
            return
        self._dispatch_stop.clear()
        def loop() -> None:
            while not self._dispatch_stop.is_set():
                try:
                    item = self._events.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    self._events.task_done()
                    break
                chat_id, text, sender_id = item
                try:
                    # Approval must be able to unblock a turn holding the chat lock.
                    if text.lstrip().lower().startswith('/approve '):
                        self.on_message(chat_id, text, sender_id=sender_id)
                    else:
                        with self._chat_lock(chat_id):
                            self.on_message(chat_id, text, sender_id=sender_id)
                except Exception as exc:
                    print(f'[feishu] dispatch error: {exc}')
                finally:
                    self._events.task_done()

        for _ in range(max(1, workers)):
            thread = threading.Thread(target=loop, daemon=True)
            self._dispatch_threads.append(thread)
            thread.start()

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        with self._chat_locks_guard:
            return self._chat_locks.setdefault(chat_id, threading.Lock())

    def _reply(self, chat_id: str, text: str):
        if self.transport is None or not text:
            return
        # 优先卡片（markdown 可渲染），失败回退纯文本
        send_md = getattr(self.transport, 'send_markdown', None)
        if send_md is not None:
            try:
                send_md(chat_id, text)
                return
            except Exception as exc:
                print(f'[feishu] card send failed, fallback to text: {exc}')
        return self.transport.send_text(chat_id, text)

    # ---------- 飞书长连接（需真实凭证） ----------

    def run(self, app_id: str, app_secret: str) -> None:
        """启动 WS 长连接（阻塞）。依赖 lark-oapi[websocket]。"""
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                'Feishu gateway requires lark-oapi; '
                'install with `pip install -r requirements-feishu.txt`'
            ) from exc

        def on_lark_message(data) -> None:
            try:
                message = data.event.message
                if message.message_type != 'text':
                    return
                content = json.loads(message.content)
                # 群聊里 @机器人 会留下 <at user_id="...">名字</at> 标签，
                # 必须剥掉，否则 "/run ..." 这类命令识别不到
                text = re.sub(r'<at[^>]*>.*?</at>', '',
                              content.get('text', ''), flags=re.DOTALL).strip()
                # 发送者 open_id 用于白名单校验（FEISHU_ALLOWED_USERS）
                sender_id = getattr(
                    getattr(getattr(data.event, 'sender', None),
                            'sender_id', None), 'open_id', None)
                print(f'[feishu] message from {sender_id!r} '
                      f'in chat {message.chat_id!r}')
                self.dispatch(message.chat_id, text,
                              sender_id=sender_id)
            except Exception as exc:
                print(f'[feishu] event error: {exc}')

        event_handler = (lark.EventDispatcherHandler.builder('', '')
                         .register_p2_im_message_receive_v1(on_lark_message)
                         .build())
        ws_client = lark.ws.Client(
            app_id, app_secret, event_handler=event_handler,
            log_level=lark.LogLevel.INFO)
        self.start_permission_poller()
        self.start_dispatcher()
        print('[feishu] long connection started; Ctrl+C to exit')
        ws_client.start()   # blocks


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv()   # 与 harness 共用项目根的 .env（FEISHU_*/HARNESS_* 都可放这里）
    app_id = os.getenv('FEISHU_APP_ID', '')
    app_secret = os.getenv('FEISHU_APP_SECRET', '')
    token = os.getenv('HARNESS_API_TOKEN', '')
    base_url = os.getenv('HARNESS_API_URL', 'http://127.0.0.1:8000')
    allowed_raw = os.getenv('FEISHU_ALLOWED_USERS', '').strip()
    allowed_users = {u.strip() for u in allowed_raw.split(',') if u.strip()} \
        or None
    if not (app_id and app_secret and token):
        print('FEISHU_APP_ID / FEISHU_APP_SECRET / HARNESS_API_TOKEN are required')
        return 1
    api = GatewayClient(base_url, token)
    transport = LarkTransport(app_id, app_secret)
    gateway = FeishuGateway(api, transport=transport,
                            allowed_users=allowed_users)
    if allowed_users:
        print(f'[feishu] user allowlist active: {", ".join(sorted(allowed_users))}')
    else:
        print('[feishu] WARNING: no FEISHU_ALLOWED_USERS set — anyone who can '
              'reach this bot can run commands on this machine')
    try:
        gateway.run(app_id, app_secret)
    except KeyboardInterrupt:
        print('\n[feishu] exited')
    finally:
        gateway.stop()
        api.close()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
