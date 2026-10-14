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
    HARNESS_API_URL / HARNESS_API_TOKEN Agent API 地址与令牌

聊天命令（@机器人 发送）：
    /new [id] /sessions /run <cmd> /status [id] /logs <id> /stop <id>
    /perms /approve <req_id> y|n /quit(解绑)
    其余文本 → 作为消息发给绑定的 agent 会话
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

from gateway_sim import GatewayClient

BINDINGS_PATH = Path(os.getenv('FEISHU_BINDINGS', '.cache/feishu_bindings.json'))
PERMISSION_POLL_INTERVAL = 3.0


def format_permission_prompt(req: dict) -> str:
    """审批提醒文案：人工只需回复 /approve <id> y|n。"""
    preview = str(req.get('args', ''))[:160]
    return (f"⚠ 需要审批 [{req['req_id']}]\n"
            f"工具: {req['tool']}\n"
            f"原因: {req['reason']}\n"
            f"参数: {preview}\n"
            f"回复 /approve {req['req_id']} y（允许）或 n（拒绝）")


class FeishuGateway:
    """核心编排：聊天绑定、命令分发、权限轮询。传输层可注入以便测试。"""

    def __init__(self, api: GatewayClient, transport=None,
                 bindings_path: Path | None = None) -> None:
        self.api = api
        self.transport = transport          # 需实现 send_text(chat_id, text)
        self.bindings_path = Path(bindings_path or BINDINGS_PATH)
        self.bindings: dict[str, str] = {}   # chat_id → session_id
        self._load_bindings()
        self._notified: set[str] = set()
        self._poll_stop = threading.Event()

    # ---------- 绑定持久化 ----------

    def _load_bindings(self) -> None:
        if self.bindings_path.exists():
            try:
                self.bindings = json.loads(
                    self.bindings_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                self.bindings = {}

    def _save_bindings(self) -> None:
        self.bindings_path.parent.mkdir(parents=True, exist_ok=True)
        self.bindings_path.write_text(
            json.dumps(self.bindings, ensure_ascii=False, indent=2),
            encoding='utf-8')

    # ---------- 消息入口（可测试核心） ----------

    def on_message(self, chat_id: str, text: str) -> None:
        """飞书文本消息入口。命令本地处理；普通文本转发给 agent 会话。"""
        text = (text or '').strip()
        if not text:
            return
        session_id = self.bindings.get(chat_id)
        if text.startswith('/'):
            self._handle_command(chat_id, text)
            return
        if not session_id:
            session_id = self.api.create_session()
            self.bindings[chat_id] = session_id
            self._save_bindings()
            self._reply(chat_id, f'已创建会话 {session_id}')
        try:
            self.api.session_id = session_id
            reply = self.api.send(text)
            self._reply(chat_id, f'{reply}')
        except Exception as exc:
            self._reply(chat_id, f'[error] {exc}')

    def _handle_command(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ''
        try:
            if command == '/new':
                session_id = self.api.create_session(argument or None)
                self.bindings[chat_id] = session_id
                self._save_bindings()
                self._reply(chat_id, f'已创建并绑定会话 {session_id}')
            elif command == '/sessions':
                rows = self.api.list_sessions(limit=10)
                lines = [f"{row['id'][:12]}  events={row['events']}  "
                         f"{row['title']}" for row in rows]
                self._reply(chat_id, '\n'.join(lines) or '(no sessions)')
            elif command == '/run':
                task = self.api.create_task(argument)
                self._reply(chat_id, f"任务已创建 {task['id']} "
                              f"({task['status']})")
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
                if len(tokens) != 2 or tokens[1] not in ('y', 'n'):
                    self._reply(chat_id, '用法: /approve <req_id> y|n')
                    return
                record = self.api.resolve_permission(tokens[0], tokens[1] == 'y')
                self._reply(chat_id, f"{record['req_id']} → {record['status']}")
            elif command == '/quit':
                self.bindings.pop(chat_id, None)
                self._save_bindings()
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
                self._reply(chat_id, format_permission_prompt(req))

    def start_permission_poller(self) -> threading.Thread:
        def loop() -> None:
            while not self._poll_stop.wait(PERMISSION_POLL_INTERVAL):
                self.poll_permissions()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._poll_stop.set()

    def _reply(self, chat_id: str, text: str) -> None:
        if self.transport is not None and text:
            self.transport.send_text(chat_id, text)

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
                self.on_message(message.chat_id, content.get('text', ''))
            except Exception as exc:
                print(f'[feishu] event error: {exc}')

        event_handler = (lark.EventDispatcherHandler.builder('', '')
                         .register_p2_im_message_receive_v1(on_lark_message)
                         .build())
        ws_client = lark.ws.Client(
            app_id, app_secret, event_handler=event_handler,
            log_level=lark.LogLevel.INFO)
        self.start_permission_poller()
        print('[feishu] long connection started; Ctrl+C to exit')
        ws_client.start()   # blocks


def main(argv=None) -> int:
    app_id = os.getenv('FEISHU_APP_ID', '')
    app_secret = os.getenv('FEISHU_APP_SECRET', '')
    token = os.getenv('HARNESS_API_TOKEN', '')
    base_url = os.getenv('HARNESS_API_URL', 'http://127.0.0.1:8000')
    if not (app_id and app_secret and token):
        print('FEISHU_APP_ID / FEISHU_APP_SECRET / HARNESS_API_TOKEN are required')
        return 1
    api = GatewayClient(base_url, token)
    gateway = FeishuGateway(api)
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
