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
    /help             命令清单（未知命令也会自动附带）
    /new [id] /sessions /use <前缀> /cd [dir] /run <cmd> /status [id]
    /logs <id> /stop <id> /perms /approve [req_id] y|n /quit(解绑)
    审批卡片按钮  —— 点【允许/拒绝】直接决议（首选，零输入）
    y / n、y2 / n2 —— 快捷审批：按序号决议本聊天的待审批请求
    /use <前缀>    —— 切换该聊天绑定的会话（/sessions 查列表，前缀唯一即可）
    /cd <dir>      —— 切换当前会话工作区（对话与 /run 同时生效）
    其余文本 → 作为消息发给绑定的 agent 会话
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import unicodedata
import secrets
from pathlib import Path

import httpx

from gateway_sim import GatewayClient
from perf import record as perf_record, reset as perf_reset
from session_views import group_sessions, session_details
from feishu_sessions import PAGE_SIZE, list_card, detail_card

BINDINGS_PATH = Path(os.getenv('FEISHU_BINDINGS', '.cache/feishu_bindings.json'))
PERMISSION_POLL_INTERVAL = 3.0
DISPATCH_WORKERS = 4   # 消息分发线程数：同一聊天内保序，不同聊天并行


def permission_result(record: dict) -> str:
    status = {'approved': '已允许', 'denied': '已拒绝', 'timeout': '审批超时'}.get(
        record['status'], '审批已处理')
    return f"{status} · {record.get('tool', '操作')}\n原因: {record.get('reason', '')}"


def format_permission_prompt(req: dict, index: int | None = None) -> str:
    """审批提醒文案：点卡片按钮，或直接回复 y / n（多条时 y2、n2 按序号选）。"""
    preview = str(req.get('args', ''))[:160]
    head = '⚠ 需要审批' + (f' #{index}' if index else '') \
        + f" · {req.get('tool', '?')}"
    if index in (None, 1):
        tail = '回复 y（允许）或 n（拒绝）'
    else:
        tail = f'回复 y{index}（允许）或 n{index}（拒绝）'
    return (f"{head}\n"
            f"原因: {req.get('reason', '')}\n"
            f"参数: {preview}\n"
            f"{tail}（也可直接点按钮）")


CARD_MD_LIMIT = 24000   # 飞书卡片 JSON 上限约 30KB，留余量


def markdown_to_card_md(md_text: str) -> str:
    """标准 Markdown → 飞书卡片 lark_md（子集）。

    message 卡片的 markdown 元素不支持标题/水平线/无序列表标记，
    转成卡片支持的加粗行、符号列表等近似样式；代码块原样保留。
    """
    lines = md_text.splitlines()
    out = []
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('```'):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        # lark_md does not render GitHub tables. Turn a table into compact
        # labelled rows so the information remains readable in a card.
        if ('|' in stripped and i + 1 < len(lines)
                and re.match(r'^\s*\|?\s*:?-{3,}', lines[i + 1])):
            headers = [cell.strip() for cell in stripped.strip('|').split('|')]
            i += 2  # consume header and separator
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                values = [cell.strip() for cell in lines[i].strip().strip('|').split('|')]
                pairs = [f'{h}: {v}' for h, v in zip(headers, values)]
                out.append(f'{indent}• ' + '；'.join(pairs))
                i += 1
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:   # 标题 → 加粗行
            out.append(f'{indent}**{m.group(2).strip()}**')
            i += 1
            continue
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):   # 水平线 → 空行
            out.append('')
            i += 1
            continue
        m = re.match(r'^[-*+]\s+(.*)$', stripped)
        if m:   # 无序列表 → 符号列表
            out.append(f'{indent}• {m.group(1)}')
            i += 1
            continue
        out.append(line)
        i += 1
    return '\n'.join(out)


class LarkTransport:
    """飞书消息发送传输层：im/v1/message.create（text / interactive 卡片）。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark
        self._client = lark.Client.builder() \
            .app_id(app_id).app_secret(app_secret).build()

    @staticmethod
    def validate_chat_id(chat_id: str) -> None:
        if not isinstance(chat_id, str) or not chat_id.startswith('oc_') or len(chat_id) <= 3:
            raise ValueError('飞书聊天目标必须是 oc_ 开头的 chat_id，不能使用用户或测试 ID')

    def send_text(self, chat_id: str, text: str) -> str | None:
        self.validate_chat_id(chat_id)
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
            .msg_type('text')
            .content(json.dumps({'text': text}, ensure_ascii=False)).build()
        ).build()
        response = self._client.im.v1.message.update(request)
        if not response.success():
            raise RuntimeError(f'feishu update failed: {response.code} {response.msg}')

    @staticmethod
    def _markdown_card(md_text: str) -> str:
        content = markdown_to_card_md(md_text)
        if len(content) > CARD_MD_LIMIT:
            content = content[:CARD_MD_LIMIT] + '\n…[内容过长已截断]'
        card = {
            'config': {'wide_screen_mode': True},
            # Legacy interactive cards use lark_md under a div element.
            # Sending a top-level `markdown` element succeeds but renders
            # markdown markers literally in Feishu clients.
            'elements': [{
                'tag': 'div',
                'text': {'tag': 'lark_md', 'content': content},
            }],
        }
        return json.dumps(card, ensure_ascii=False)

    def send_markdown(self, chat_id: str, md_text: str) -> str | None:
        """以交互卡片发送 markdown——飞书 text 消息不解析任何 markdown 语法，
        卡片的 markdown 元素才支持粗体/斜体/行内代码/链接。"""
        return self._send_card(chat_id, self._markdown_card(md_text))

    def send_session_card(self, chat_id: str, card: dict) -> str | None:
        return self._send_card(chat_id, json.dumps(card, ensure_ascii=False))

    def _send_card(self, chat_id: str, content: str) -> str | None:
        self.validate_chat_id(chat_id)
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        request = CreateMessageRequest.builder() \
            .receive_id_type('chat_id') \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type('interactive')
                .content(content)
                .build()
            ).build()
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f'feishu card send failed: {response.code} {response.msg}')
        data = getattr(response, 'data', None)
        return getattr(data, 'message_id', None) if data else None

    def update_markdown(self, message_id: str, md_text: str) -> None:
        """Update an existing interactive card in place.

        PATCH /im/v1/messages/:message_id 的 body 只收 content（卡片 JSON），
        带 msg_type 会报 230001 invalid msg_type——message.update 那条路不通。
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        request = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder()
            .content(self._markdown_card(md_text)).build()
        ).build()
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            raise RuntimeError(
                f'feishu card update failed: {response.code} {response.msg}')

    def send_approval_card(self, chat_id: str, prompt_md: str,
                           req_id: str) -> str | None:
        """带【允许/拒绝】按钮的审批卡片；按钮 value 携带 req_id，
        点击后走 card.action.trigger 回调决议，无需用户打字。"""
        self.validate_chat_id(chat_id)
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        card = {
            'config': {'wide_screen_mode': True},
            'elements': [
                {'tag': 'markdown',
                 'content': markdown_to_card_md(prompt_md)},
                {'tag': 'hr'},
                {'tag': 'action', 'actions': [
                    {'tag': 'button',
                     'text': {'tag': 'plain_text', 'content': '✅ 允许'},
                     'type': 'primary',
                     'value': {'approval': req_id, 'allow': True}},
                    {'tag': 'button',
                     'text': {'tag': 'plain_text', 'content': '⛔ 拒绝'},
                     'type': 'danger',
                     'value': {'approval': req_id, 'allow': False}},
                ]},
            ],
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
                f'feishu approval card failed: {response.code} {response.msg}')
        data = getattr(response, 'data', None)
        return getattr(data, 'message_id', None) if data else None


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
        self._session_choices: dict[str, list[dict]] = {}
        self._session_views: dict[str, tuple] = {}
        self._session_confirmation: dict[str, tuple] = {}
        # WS 事件解耦：长 turn 的 send 会阻塞回调线程数分钟，必须丢进队列由
        # worker 池消费；同一聊天的消息用锁保序，不同聊天并行。
        self._events: queue.Queue = queue.Queue(maxsize=1000)
        self._approval_events: queue.Queue = queue.Queue(maxsize=1000)
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
                    validator = getattr(self.transport, 'validate_chat_id', None)
                    if validator:
                        for chat_id in list(self.bindings):
                            try:
                                validator(chat_id)
                            except ValueError:
                                self.bindings.pop(chat_id)
                                print(f'[feishu] ignored invalid saved chat binding: {chat_id!r}')
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
        # 全角归一化（NFKC）：中文输入法的 ／use、／？、Ｙ２ 统一转半角再路由；
        # 发给 agent 的普通消息仍用原文，不受影响
        norm = unicodedata.normalize('NFKC', text)
        # y/n 快捷审批：该聊天有待审批请求时，y/n 不再当作普通消息；
        # y2 / n2 决议本聊天 FIFO 里第 2 条，依此类推
        m = re.fullmatch(r'(y|n)(\d{0,2})', norm.lower())
        if m and self._chat_pending.get(chat_id):
            index = int(m.group(2)) - 1 if m.group(2) else 0
            self._resolve_chat_pending(chat_id, m.group(1) == 'y', index)
            return
        session_id = self.bindings.get(chat_id)
        if norm.startswith('/'):
            self._handle_command(chat_id, norm, sender_id=sender_id)
            return
        if not session_id:
            session_id = self.api.create_session()
            with self._bindings_lock:
                self.bindings[chat_id] = session_id
                self._save_bindings()
            self._reply(chat_id, '已创建新会话。')
        try:
            self._send_agent_reply(chat_id, session_id, text)
        except Exception as exc:
            self._reply(chat_id, f'[error] {exc}')

    def _send_agent_reply(self, chat_id: str, session_id: str, text: str) -> None:
        """Stream into one editable Markdown card when supported."""
        perf_reset()
        _t_entry = time.perf_counter()
        updater = getattr(self.transport, 'update_markdown', None)
        sender = getattr(self.transport, 'send_markdown', None)
        fallback_sender = getattr(self.transport, 'send_text', None)
        if not updater or not sender:
            updater = getattr(self.transport, 'update_text', None)
            sender = getattr(self.transport, 'send_text', None) if updater else None
        message_id = None
        parts = []
        last_update = 0.0
        stream_failed = False

        def update_partial(text: str) -> None:
            nonlocal message_id, last_update, stream_failed
            if stream_failed or not updater:
                return
            now = time.monotonic()
            if updater and now - last_update >= 0.6:
                try:
                    if message_id is None:
                        # 首建卡片用本次内容（进度路径下 parts 可能为空）
                        message_id = sender(chat_id, text or '生成中…')
                        # 首张卡片可见，可能仅包含处理进度。
                        perf_record('gw.first_card', time.perf_counter() - _t_entry)
                    if message_id:
                        updater(message_id, text)
                        last_update = now
                except Exception as exc:
                    stream_failed = True
                    print(f'[feishu] stream update failed, falling back: {exc}')

        def on_delta(part: str) -> None:
            parts.append(part)
            update_partial(''.join(parts))

        def on_progress(status: dict) -> None:
            elapsed = status.get('elapsed', 0)
            if status.get('phase') == 'tool':
                label = f"正在执行 {status.get('tool') or '工具'}（{status.get('tool_elapsed', 0)} 秒）"
            else:
                label = '正在处理'
            update_partial(f'{label} · 已耗时 {elapsed} 秒\n\n' + ''.join(parts))

        try:
            streamer = getattr(self.api, 'send_stream', None)
            if streamer is not None:
                reply = streamer(text, session_id=session_id, on_delta=on_delta,
                                 on_progress=on_progress)
            else:
                reply = self.api.send(text, session_id=session_id)
        except Exception as exc:
            print(f'[feishu] agent reply failed: {exc}')
            failed = f'处理未完成：{exc}\n请先核实任务状态，再决定是否重试。\n\n' + ''.join(parts)
            if message_id and updater:
                try:
                    updater(message_id, failed)
                    return
                except Exception as update_exc:
                    print(f'[feishu] failure card update failed: {update_exc}')
            self._reply(chat_id, failed)
            return
        reply = reply or '本轮已结束，未生成文本回复。'
        if message_id and updater and not stream_failed:
            try:
                _t_final = time.perf_counter()
                updater(message_id, reply)
                perf_record('gw.final_update', time.perf_counter() - _t_final)
                return
            except Exception as exc:
                print(f'[feishu] final card update failed, falling back: {exc}')
                stream_failed = True
        if stream_failed and fallback_sender:
            try:
                fallback_sender(chat_id, reply)
                return
            except Exception:
                pass
        self._reply(chat_id, reply)

    # ---------- 快捷审批 ----------

    def _resolve_chat_pending(self, chat_id: str, allow: bool,
                              index: int = 0) -> None:
        """决议该聊天的第 index+1 条待审批（免输 req_id）。"""
        pending_ids = self._chat_pending.get(chat_id) or []
        if index >= len(pending_ids):
            self._reply(chat_id, f'(没有第 {index + 1} 条待审批，试试 /perms)')
            return
        req_id = pending_ids[index]
        try:
            record = self.api.resolve_permission(req_id, allow)
        except Exception:
            # 已决议/超时的请求：移出 FIFO，提示用户重新查看
            pending_ids.remove(req_id)
            self._reply(chat_id, '(该请求已失效或已决议，试试 /perms)')
            return
        self._forget_pending(req_id)
        self._reply(chat_id, permission_result(record))

    @staticmethod
    def _session_rows_text(rows, start=0):
        from datetime import datetime
        lines = []
        for index, row in enumerate(rows, start + 1):
            timestamp = datetime.fromtimestamp(row.get('updated', 0)).strftime('%m-%d %H:%M')
            lines.append(f"{index}. {row['title']}\n工作区: {row.get('workspace') or '未记录'} · {timestamp}")
            if row.get('preview') and row['preview'] != row['title']:
                lines.append(f"最近: {row['preview']}")
        return '\n\n'.join(lines)

    def _show_sessions(self, chat_id, rows, *, include_empty=False):
        current = self.bindings.get(chat_id)
        rows = [row for row in rows if include_empty or row.get('preview') != '尚无任务'
                or row['id'] == current]
        rows = [row for _, group in group_sessions(rows) for row in group]
        self._session_choices[chat_id] = rows
        self._session_views.pop(chat_id, None)
        if not rows:
            self._reply(chat_id, '暂无有任务内容的会话。')
            return
        token = secrets.token_urlsafe(12)
        self._session_views[chat_id] = (token, rows, time.monotonic())
        self._send_session_view(chat_id, list_card(rows, token, current),
                                self._session_rows_text(rows[:PAGE_SIZE])
                                + '\n\n/resume <序号或关键词> 切换；/sessions 2 查看下一页。')

    def _send_session_view(self, chat_id, card, fallback):
        sender = getattr(self.transport, 'send_session_card', None)
        if sender:
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] session card failed, fallback to text: {exc}')
        self._reply(chat_id, fallback)

    def _switch_session(self, chat_id, row):
        workspace = self.api.get_workspace(row['id'])
        with self._bindings_lock:
            self.bindings[chat_id] = row['id']
            self._save_bindings()
        self._session_confirmation.pop(chat_id, None)
        self._reply(chat_id, f"已切换到会话\n主题: {row['title']}\n工作区: {workspace}")

    def _handle_session_action(self, chat_id, value, sender_id):
        if self.allowed_users is not None and sender_id not in self.allowed_users:
            return
        view = self._session_views.get(chat_id)
        if not view or view[0] != value.get('session_view') or time.monotonic() - view[2] > 600:
            self._reply(chat_id, '会话列表已失效，请重新 /sessions。')
            return
        token, rows, _ = view
        index, action = value.get('index'), value.get('action')
        if type(index) is not int or index < 0:
            return
        current = self.bindings.get(chat_id)
        if action == 'page' and index < (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE:
            start = index * PAGE_SIZE
            self._send_session_view(chat_id, list_card(rows, token, current, index),
                                    self._session_rows_text(rows[start:start + PAGE_SIZE], start=start)
                                    + '\n\n/resume <序号> 切换；/sessions <页码> 翻页。')
        elif action in ('switch', 'details') and index < len(rows):
            try:
                # Re-read the session: archiving/deleting after listing must not
                # make an old card switch to a nonexistent session.
                if action == 'switch':
                    self._switch_session(chat_id, rows[index])
                else:
                    self.api.get_workspace(rows[index]['id'])
                    latest = next((row for row in self.api.list_sessions(limit=1000)
                                   if row['id'] == rows[index]['id']), rows[index])
                    fallback = '\n\n'.join(f'{label}: {text}' for label, text in session_details(latest, current))
                    reply = markdown_to_card_md(latest.get('last_assistant') or '尚无文字回复')
                    self._send_session_view(chat_id, detail_card(latest, token, index, current,
                                                                reply_markdown=reply), fallback)
            except Exception as exc:
                self._reply(chat_id, '无法打开此会话，请重新 /sessions 查看有效会话。')
                print(f'[feishu] session action failed: {exc}')
        else:
            self._reply(chat_id, '没有这一页，请重新 /sessions 查看会话列表。')

    def _manage_sessions(self, chat_id, command, argument):
        if command == '/cancel':
            self._session_confirmation.pop(chat_id, None)
            self._reply(chat_id, '已取消。')
            return
        if command == '/confirm':
            pending = self._session_confirmation.pop(chat_id, None)
            if not pending or time.monotonic() - pending[2] > 300:
                self._reply(chat_id, '没有有效的待确认操作，请重新选择。')
                return
            action, rows, _ = pending
            if action == 'delete' and argument != '删除':
                self._reply(chat_id, '未执行删除，请重新选择后输入 /confirm 删除。')
                return
            completed = 0
            for row in rows:
                if row['id'] in self.bindings.values():
                    self._reply(chat_id, f"跳过 {row['title']}：仍被聊天使用，请先 /quit 解绑。")
                    continue
                try:
                    self.api.manage_session(action, row)
                    completed += 1
                except Exception:
                    self._reply(chat_id, f"未处理 {row['title']}：记录可能已变化，请刷新列表后重试。")
            verb = {'archive': '归档', 'restore': '恢复', 'delete': '永久删除'}[action]
            self._reply(chat_id, f'已{verb} {completed} 个会话。' +
                        ('删除不可恢复。' if action == 'delete' else '项目文件未改动。'))
            return
        # A selection is tied to the exact list and action shown in this chat.
        if argument.startswith('select '):
            listing = getattr(self, '_archive_choices', {}).get(chat_id)
            if not listing or listing[0] != command or time.monotonic() - listing[3] > 300:
                self._reply(chat_id, '请先刷新归档或删除列表。')
                return
            indices = argument.split()[1:]
            rows = listing[2]
            if not indices or any(not n.isdigit() or not 1 <= int(n) <= len(rows) for n in indices):
                self._reply(chat_id, '请输入列表中的序号，例如 select 1 3。')
                return
            selected = [rows[i] for i in sorted({int(n) - 1 for n in indices})]
            self._session_confirmation[chat_id] = (listing[1], selected, time.monotonic())
            confirmation = '/confirm 删除（不可恢复）' if listing[1] == 'delete' else '/confirm'
            self._reply(chat_id, '待处理会话：\n' + self._session_rows_text(selected)
                        + f'\n\n确认: {confirmation}\n取消: /cancel')
            return
        from session_cleanup import cleanup_candidates
        if command == '/delete':
            if argument:
                self._reply(chat_id, '用法: /delete，或 /delete select <序号...>')
                return
            action, rows = 'delete', self.api.list_archives()
        elif argument == 'restore':
            action, rows = 'restore', self.api.list_archives()
        else:
            if argument not in ('', 'empty', 'all') and not (argument.isascii() and argument.isdigit()
                                                                         and 1 <= int(argument) <= 36500):
                self._reply(chat_id, '用法: /archive [天数|empty|all|restore]')
                return
            action, rows = 'archive', self.api.list_sessions(limit=1000)
            if argument != 'all':
                rows = cleanup_candidates(rows, '', days=int(argument) if argument.isdigit() else 30,
                                          empty_only=argument == 'empty')
        rows = [row for row in rows if row['id'] not in self.bindings.values()][:20]
        if not hasattr(self, '_archive_choices'):
            self._archive_choices = {}
        self._archive_choices[chat_id] = (command, action, rows, time.monotonic())
        self._session_confirmation.pop(chat_id, None)
        self._reply(chat_id, self._session_rows_text(rows) + f'\n\n{command} select <序号...> 多选。'
                    if rows else '没有符合条件的会话。当前绑定的会话不可处理；/archive all 查看其他会话。')

    def _help_text(self) -> str:
        return '\n'.join([
            '可用命令:',
            '/new [id]              新建会话并绑定到本聊天',
            '/sessions [页码]       会话卡片列表、详情与切换',
            '/resume [关键词或序号]  搜索并切换会话（/use 兼容）',
            '/archive [天数|empty|all|restore]  选择归档或恢复会话',
            '/archive select <序号...>  多选后预览归档或恢复',
            '/delete                查看可永久删除的归档',
            '/delete select <序号...>  多选后预览删除',
            '/confirm、/cancel      确认或取消待执行操作',
            '/cd [dir]              查看/切换当前会话工作区',
            '/pwd                   查看当前会话工作区',
            '/model                  查看当前模型配置',
            '/approvals、/perms       查看待审批操作',
            '/run <命令>            在任务目录执行命令',
            '/status [任务id]       查看任务状态（省略 id 看最近 10 条）',
            '/logs <任务id> [行数]  查看任务输出（默认 60 行）',
            '/stop <任务id>         终止任务',
            '/perms                 查看待审批请求',
            '/approve y|n           审批最早一条待审批操作',
            '/quit                  解除会话绑定',
            'y / n、y2 / n2         快捷审批（也可直接点审批卡片按钮）',
            '其余文本 → 作为消息发给当前绑定的 agent 会话',
        ])

    def _handle_command(self, chat_id: str, text: str, sender_id: str | None = None) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ''
        try:
            if command in ('/archive', '/delete', '/confirm', '/cancel'):
                self._manage_sessions(chat_id, command, argument)
            elif command in ('/help', '/?', '/'):
                self._reply(chat_id, self._help_text())
            elif command == '/new':
                previous = self.bindings.get(chat_id)
                workspace = self.api.get_workspace(previous) if previous else None
                session_id = self.api.create_session(argument or None, workspace=workspace)
                with self._bindings_lock:
                    self.bindings[chat_id] = session_id
                    self._save_bindings()
                self._reply(chat_id, '已创建新会话，工作区保持不变。')
            elif command == '/sessions' or command == '/resume' and not argument:
                if command == '/sessions' and argument.isdigit() and int(argument) > 0:
                    view = self._session_views.get(chat_id)
                    if view:
                        self._handle_session_action(chat_id, {'session_view': view[0], 'action': 'page',
                                                             'index': int(argument) - 1},
                                                    sender_id)
                    else:
                        self._reply(chat_id, '请先 /sessions 打开会话列表。')
                else:
                    self._show_sessions(chat_id, self.api.list_sessions(limit=1000))
            elif command in ('/use', '/resume'):
                if not argument:
                    self._reply(chat_id, '用法: /use <会话ID前缀>'
                                          '\n（/sessions 可查，前缀唯一即可）')
                    return
                rows = self.api.list_sessions(limit=1000)
                if command == '/resume' and argument.isdigit() and chat_id in self._session_choices:
                    index = int(argument) - 1
                    choices = self._session_choices[chat_id]
                    selected = choices[index]['id'] if 0 <= index < len(choices) else None
                    hits = [row for row in rows if row['id'] == selected]
                else:
                    hits = [row for row in rows if row['id'].startswith(argument)
                            or command == '/resume' and argument.casefold() in ' '.join(str(row.get(k) or '') for k in
                                ('title', 'workspace', 'preview')).casefold()]
                if not hits:
                    self._reply(chat_id, f'未找到匹配 {argument!r} 的会话')
                    return
                if len(hits) > 1:
                    self._reply(chat_id, '匹配有歧义，请从候选会话中选择序号。')
                    self._show_sessions(chat_id, hits, include_empty=True)
                    return
                self._switch_session(chat_id, hits[0])
            elif command in ('/cd', '/pwd'):
                if command == '/pwd' and argument:
                    self._reply(chat_id, '用法: /pwd')
                    return
                session_id = self.bindings.get(chat_id)
                if not session_id:
                    session_id = self.api.create_session(workspace=argument or None)
                    with self._bindings_lock:
                        self.bindings[chat_id] = session_id
                        self._save_bindings()
                    self._reply(chat_id, f'当前工作区: {self.api.get_workspace(session_id)}'
                                '\n已创建新会话，后续对话和 /run 使用此目录。')
                    return
                if not argument:
                    self._reply(chat_id, f'当前工作区: {self.api.get_workspace(session_id)}')
                    return
                workspace = self.api.set_workspace(session_id, argument)
                self._reply(chat_id, f'工作区已切换: {workspace}\n后续对话和 /run 使用此目录。')
            elif command == '/run':
                if not argument:
                    self._reply(chat_id, '用法: /run <命令>')
                    return
                session_id = self.bindings.get(chat_id)
                cwd = self.api.get_workspace(session_id) if session_id else None
                task = self.api.create_task(argument, cwd=cwd)
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
            elif command in ('/perms', '/approvals'):
                pending = self.api.pending_permissions()
                if not pending:
                    self._reply(chat_id, '(无待审批请求)')
                for i, req in enumerate(pending, 1):
                    self._push_approval(chat_id, req, i)
            elif command == '/model':
                self._reply(chat_id, f"当前模型: {os.getenv('LLM_MODEL_ID', '未配置')}\n"
                              f"服务地址: {os.getenv('LLM_BASE_URL', '默认地址')}")
            elif command == '/approve':
                tokens = argument.split()
                if tokens and tokens[-1] in ('y', 'n') and len(tokens) == 1:
                    # /approve y|n —— 决议本聊天最早的待审批请求
                    self._resolve_chat_pending(chat_id, tokens[0] == 'y')
                    return
                if len(tokens) != 2 or tokens[1] not in ('y', 'n'):
                    self._reply(chat_id, '用法: /approve y|n，或直接回复 y/n'
                                          '\n多条待审批时，用 y2/n2 选择第 2 条；/perms 查看操作内容。')
                    return
                record = self.api.resolve_permission(tokens[0], tokens[1] == 'y')
                self._forget_pending(record['req_id'])
                self._reply(chat_id, permission_result(record))
            elif command == '/quit':
                with self._bindings_lock:
                    self.bindings.pop(chat_id, None)
                    self._save_bindings()
                self._reply(chat_id, '会话绑定已解除')
            else:
                self._reply(chat_id,
                            f'未知命令: {command}\n\n' + self._help_text())
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.json().get('detail', '')
                      if exc.response.headers.get('content-type', '').startswith('application/json')
                      else exc.response.text[:200])
            self._reply(chat_id, f'操作未完成: {detail}')
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
                try:
                    self._push_approval(chat_id, req, fifo.index(req_id) + 1)
                except Exception as exc:
                    print(f'[feishu] approval notification failed for chat {chat_id!r}: {exc}')
        # 决议完成（或超时）的请求退出通知集合与各聊天 FIFO，避免无界增长
        pending_ids = {req['req_id'] for req in pending}
        self._notified &= pending_ids
        for chat_id, fifo in self._chat_pending.items():
            fifo[:] = [req_id for req_id in fifo if req_id in pending_ids]

    def _push_approval(self, chat_id: str, req: dict, index: int) -> None:
        """推送审批请求：优先带按钮的卡片，发送失败回退纯文本提示。"""
        sender = getattr(self.transport, 'send_approval_card', None)
        if sender:
            try:
                sender(chat_id, format_permission_prompt(req, index),
                       req['req_id'])
                return
            except Exception as exc:
                print(f'[feishu] approval card failed, fallback to text: {exc}')
        self._reply(chat_id, format_permission_prompt(req, index))

    def _forget_pending(self, req_id: str) -> None:
        """决议完成后清理通知集合与各聊天的 FIFO。"""
        self._notified.discard(req_id)
        for fifo in self._chat_pending.values():
            if req_id in fifo:
                fifo.remove(req_id)

    def handle_card_action(self, open_id: str | None, value: dict | None,
                           message_id: str | None, chat_id: str | None = None) -> tuple[str, bool]:
        """Card callbacks: resolve approvals directly; queue session navigation.

        返回 (toast 文案, 是否成功)。open_id 受用户白名单约束——
        群里任何能点按钮的人都算，必须过同一道闸。
        """
        if self.allowed_users is not None and open_id not in self.allowed_users:
            return ('⛔ 你没有会话操作权限' if (value or {}).get('session_view')
                    else '⛔ 你没有审批权限'), False
        if (value or {}).get('session_view'):
            view = self._session_views.get(chat_id)
            if not view or view[0] != value['session_view'] or time.monotonic() - view[2] > 600:
                return '会话列表已失效，请重新 /sessions', False
            index, action = value.get('index'), value.get('action')
            limit = ((len(view[1]) + PAGE_SIZE - 1) // PAGE_SIZE if action == 'page' else len(view[1]))
            if action not in ('page', 'details', 'switch') or type(index) is not int or not 0 <= index < limit:
                return '无效的会话按钮，请重新 /sessions', False
            try:
                self._events.put_nowait((chat_id, value, open_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理会话操作…', True
        req_id = (value or {}).get('approval')
        if not req_id:
            return '⚠ 无效的审批按钮', False
        allow = bool((value or {}).get('allow'))
        try:
            record = self.api.resolve_permission(req_id, allow)
        except Exception as exc:
            print(f'[feishu] approval failed: {exc}')
            return '⚠ 操作未能完成，请用 /perms 查看最新审批状态', False
        self._forget_pending(req_id)
        verb = '允许' if allow else '拒绝'
        updater = getattr(self.transport, 'update_markdown', None)
        if updater and message_id:
            try:
                updater(message_id, permission_result(record))
            except Exception as exc:
                print(f'[feishu] approval card update failed: {exc}')
        return f'已{verb}', True

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
        for thread in self._dispatch_threads:
            thread.join(timeout=3)
        self._dispatch_threads.clear()

    # ---------- WS 事件分发（长任务不阻塞回调线程） ----------

    def dispatch(self, chat_id: str, text: str,
                 sender_id: str | None = None) -> None:
        """把飞书事件投递到分发队列（回调线程立即返回）。"""
        norm = unicodedata.normalize('NFKC', text or '').strip().lower()
        is_approval = (norm.split()[:1] == ['/approve'] or
                       (re.fullmatch(r'(y|n)(\d{0,2})', norm) is not None
                        and bool(self._chat_pending.get(chat_id))))
        events = self._approval_events if is_approval else self._events
        try:
            events.put_nowait((chat_id, text, sender_id))
        except queue.Full:
            self._reply(chat_id, '当前消息较多，请稍后重试')

    def start_dispatcher(self, workers: int = DISPATCH_WORKERS) -> None:
        if self._dispatch_threads:
            return
        self._dispatch_stop.clear()
        def loop(events, approval=False) -> None:
            while not self._dispatch_stop.is_set():
                try:
                    item = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                chat_id, text, sender_id = item
                try:
                    if approval:
                        if self.allowed_users is not None and sender_id not in self.allowed_users:
                            continue
                        norm = unicodedata.normalize('NFKC', text or '').strip().lower()
                        # A queued shortcut must never turn into an agent request if
                        # another approval has already cleared the pending list.
                        if (re.fullmatch(r'(y|n)(\d{0,2})', norm)
                                and not self._chat_pending.get(chat_id)):
                            self._reply(chat_id, '没有待审批请求，可能已处理或已过期。')
                            continue
                        self.on_message(chat_id, text, sender_id=sender_id)
                    else:
                        with self._chat_lock(chat_id):
                            if isinstance(text, dict):
                                self._handle_session_action(chat_id, text, sender_id)
                            else:
                                self.on_message(chat_id, text, sender_id=sender_id)
                except Exception as exc:
                    print(f'[feishu] dispatch error: {exc}')
                finally:
                    events.task_done()

        queues = [(self._events, False)] * max(1, workers) + [(self._approval_events, True)]
        for events, approval in queues:
            thread = threading.Thread(target=loop, args=(events, approval), daemon=True)
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

        def on_card_action(data) -> None:
            """卡片按钮回调：快速返回提示，会话操作交给分发线程。"""
            try:
                text, ok = self.handle_card_action(
                    getattr(data.event.operator, 'open_id', None),
                    getattr(data.event.action, 'value', None),
                    getattr(data.event.context, 'open_message_id', None),
                    chat_id=getattr(data.event.context, 'open_chat_id', None))
                from lark_oapi.event.callback.model.p2_card_action_trigger \
                    import (CallBackToast, P2CardActionTriggerResponse)
                response = P2CardActionTriggerResponse()
                response.toast = CallBackToast()
                response.toast.type = 'success' if ok else 'failed'
                response.toast.content = text
                return response
            except Exception as exc:
                print(f'[feishu] card action error: {exc}')
                return None

        event_handler = (lark.EventDispatcherHandler.builder('', '')
                         .register_p2_im_message_receive_v1(on_lark_message)
                         .register_p2_card_action_trigger(on_card_action)
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
