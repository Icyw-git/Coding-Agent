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
    FEISHU_ALLOWED_USERS                用户白名单（open_id，逗号分隔）；必填。
                                        缺失时拒绝启动，只有本地测试可以显式设置
                                        FEISHU_ALLOW_ANY_USER=1 放开
    HARNESS_API_URL / HARNESS_API_TOKEN Agent API 地址与令牌

只处理单聊（p2p）：群聊/话题群消息默认忽略，审批也只推给绑定该会话的聊天。

聊天命令（@机器人 发送）：
    /help             命令清单（未知命令也会自动附带）
    /new [序号|路径|ws:名称|id:会话ID]  选择工作区新建会话
    /workspaces      列出已登记工作区；/workspace add|remove 维护登记表
    /sessions /use <前缀> /cd [dir] /run <cmd> /status [id]
    /logs <id> /stop <id> /perms /approve [req_id] y|n /sandbox /quit(解绑)
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
import secrets
import threading
import time
import unicodedata
from pathlib import Path

import httpx

import approval_view
import llm_route
import paths
from exec_policy import approval_class
from feishu_panel import control_card
from feishu_sessions import PAGE_SIZE, detail_card, list_card, workspace_card
from gateway_sim import GatewayClient
from perf import record as perf_record
from perf import reset as perf_reset
from session_views import group_sessions, session_details
from turn_progress import format_duration
from turn_watcher import TurnWatcher, WatchState
from workspace_choices import find_workspace

BINDINGS_PATH = Path(os.getenv('FEISHU_BINDINGS') or paths.cache_path('feishu_bindings.json'))
PERMISSION_POLL_INTERVAL = 3.0
DISPATCH_WORKERS = 4   # 消息分发线程数：同一聊天内保序，不同聊天并行
WORKSPACE_VIEW_TTL = 600.0   # 工作区卡片/序号的有效期：过期必须重新 /new

# 卡片更新的合流：多会话并发逐秒刷新时，同一网关实例内合并到全局
# 最小间隔，避免飞书卡片 PATCH 接口被高频调用触发频控
MIN_CARD_PATCH_INTERVAL = 0.45


def _format_task_duration(task: dict) -> str:
    """任务用时展示（统一 mm:ss / h:mm:ss）：
    排队中 → 动态已用时（进行中）→ 总用时；旧记录缺 duration_ms 时按
    started_at/ended_at 动态计算，升级后历史任务不会显示为空。"""
    if not task.get('started_at'):
        return '排队中' if task.get('status') == 'queued' else '未开始'
    ended = task.get('ended_at')
    duration_ms = task.get('duration_ms')
    if duration_ms is None:
        end = ended if ended is not None else time.time()
        duration_ms = round((end - task['started_at']) * 1000)
    text = format_duration(max(0, duration_ms) / 1000)
    return text if ended is not None else f'{text}（进行中）'


def permission_result(record: dict) -> str:
    status = {'approved': '已允许', 'denied': '已拒绝', 'timeout': '审批超时'}.get(
        record['status'], '审批已处理')
    return f"{status} · {record.get('tool', '操作')}\n原因: {record.get('reason', '')}"


def format_permission_prompt(req: dict, index: int | None = None) -> str:
    """审批提醒文案：点卡片按钮，或直接回复 y / n（多条时 y2、n2 按序号选）。

    命令不在网关这里截断：服务端登记时已按「命令留头尾、其余字段限长」压好大小
    （approval_view.summarize），网关再切一刀只会把要审批的后半段动作弄没 ——
    审批人看不到重定向目标，就等于盲批。命令按代码块完整显示，卡片支持代码块。
    """
    tool = str(req.get('tool', '?'))
    head = '⚠ 需要审批' + (f' #{index}' if index else '') + f' · {tool}'
    reason = approval_view.safe_text(req.get('reason', ''))
    detail = approval_view.detail(tool, req.get('args') or {})
    if index in (None, 1):
        tail = '回复 y（允许）或 n（拒绝）'
    else:
        tail = f'回复 y{index}（允许）或 n{index}（拒绝）'
    return (f"{head}\n"
            f"原因: {reason}\n"
            + (f"```\n{detail}\n```\n" if detail else '')
            + f"{tail}（也可直接点按钮）")


# 「本次会话允许同类」的同类判定键不在网关侧算：网关只把「允许 + 扩到同类」这个
# 意图发给服务端，服务端按登记时的 tool/args 重算（hooks.PermissionGate.resolve）。
# 发卡时算一份、决议时算一份是旧实现的 bug 来源，而让客户端传 klass 更是提权入口。

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
                # 行内单元格可以少于表头（markdown 允许残缺行），缺的列直接不显示
                pairs = [f'{h}: {v}' for h, v in zip(headers, values, strict=False)]
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


def _split_cron_add(rest: str) -> tuple[str, str]:
    """解析 `/cron add` 的参数：支持引号包住表达式，也支持五段裸写。

    cron 表达式本身含空格（5 段），所以不能直接 `partition(' ')`；引号形式
    `"0 9 * * *" 提示词` 与裸写 `0 9 * * * 提示词` 都接受。
    """
    text = rest.strip()
    if text[:1] in (chr(34), chr(39)):
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return '', ''
        return text[1:end].strip(), text[end + 1:].strip()
    parts = text.split(None, 5)
    if len(parts) < 6:
        return '', ''
    return ' '.join(parts[:5]), parts[5].strip()


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
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
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
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
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
                           req: dict) -> str | None:
        """带审批按钮的卡片；「本次会话允许同类」只传 scope=session，
        同类键与决议一样由服务端按被审批的内容重算。"""
        self.validate_chat_id(chat_id)
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req_id = req['req_id']
        # 按钮文案里的「同类」只是给人看的提示：真正的白名单键由服务端按同一规则
        # （exec_policy.approval_class）重算，展示与生效不会分叉。
        klass = approval_class(str(req.get('tool', '?')), req.get('args') or {})
        same_class = f'📌 本次会话允许同类（{klass}）' if klass else '📌 本次会话允许同类'
        actions = [{
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': '✅ 允许'},
            'type': 'primary',
            'value': {'approval': req_id, 'allow': True},
        }]
        if klass:
            actions.append({
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': same_class},
                'type': 'primary',
                'value': {'approval': req_id, 'allow': True, 'scope': 'session'},
            })
        actions.append({
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': '⛔ 拒绝'},
            'type': 'danger',
            'value': {'approval': req_id, 'allow': False},
        })
        card = {
            'config': {'wide_screen_mode': True},
            'elements': [
                {'tag': 'markdown',
                 'content': markdown_to_card_md(prompt_md)},
                {'tag': 'hr'},
                {'tag': 'action', 'actions': actions},
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
                 allowed_users: set[str] | None = None,
                 p2p_only: bool = True) -> None:
        self.api = api
        self.transport = transport          # 需实现 send_text(chat_id, text)
        self.bindings_path = Path(bindings_path or BINDINGS_PATH)
        self.bindings: dict[str, str] = {}   # chat_id → session_id
        # 个人助手只服务单聊；群聊里任何人都能 @ 机器人，审批也只该归属
        # 绑定会话的那个聊天。
        self.p2p_only = p2p_only
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
        # chat_id → (token, rows, created_at)：卡片按钮与「/new 序号」共用同一快照
        self._workspace_views: dict[str, tuple[str, list[dict], float]] = {}
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
        self._last_card_patch = 0.0   # 实例级合流：多会话逐秒刷新共用一个节拍
        # 回合终态主动通知：状态与 bindings 分开存，避免改动后者严格的 chat->session 结构
        state_path = Path(os.getenv('FEISHU_WATCH_STATE')
                          or self.bindings_path.with_name('turn_watch.json'))
        self.watch_state = WatchState(state_path)
        self.watcher = TurnWatcher(
            self.watch_state,
            fetch=self._watch_fetch,
            notify=self._watch_notify,
            sessions=self._watched_sessions,
            poll_interval=float(os.getenv('FEISHU_WATCH_POLL', '1.0')),
            stalled_after=float(os.getenv('FEISHU_WATCH_STALLED', '300')),
        )

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
            temp.replace(self.bindings_path)

    # ---------- 消息入口（可测试核心） ----------

    def on_message(self, chat_id: str, text: str,
                   sender_id: str | None = None,
                   chat_type: str | None = None) -> None:
        """飞书文本消息入口。命令本地处理；普通文本转发给 agent 会话。

        chat_type=None 表示来自测试/模拟器（无来源信息）；真实飞书事件一定带
        p2p/group/topic，只放行 p2p。
        """
        text = (text or '').strip()
        if not text:
            return
        if self.p2p_only and chat_type not in (None, 'p2p'):
            print(f'[feishu] ignored {chat_type!r} message in chat {chat_id!r}')
            return
        if self.allowed_users is not None and sender_id not in self.allowed_users:
            # 不建会话；告诉对方自己的 open_id，省去“去后台翻 ID”这一步。
            print(f'[feishu] ignored message from unauthorized user '
                  f'{sender_id!r} in chat {chat_id!r}')
            if self.transport is not None:
                self._reply(chat_id,
                            '还没有使用权限。把下面这行加入 .env 后重启网关：'
                            + chr(10) + f'FEISHU_ALLOWED_USERS={sender_id or "ou_xxx"}')
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
            self._send_panel(chat_id,
                             intro='已就绪，直接发消息即可；需要切换/停止时点按钮。')
        try:
            self._send_agent_reply(chat_id, session_id, text)
        except Exception as exc:
            self._reply(chat_id, f'[error] {exc}')

    def _card_patch_gate(self) -> bool:
        """多会话并发时合并卡片更新：实例内最小间隔 0.45s，未到点跳过本拍
        （下一秒的进度 tick 会补上最新状态）。"""
        now = time.monotonic()
        if now - self._last_card_patch < MIN_CARD_PATCH_INTERVAL:
            return False
        self._last_card_patch = now
        return True

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
        last_sent = None
        stream_failed = False

        def update_partial(text: str) -> None:
            nonlocal message_id, last_update, stream_failed, last_sent
            if stream_failed or not updater:
                return
            if text == last_sent:
                return   # 内容与计时都没变化，不重复更新卡片
            now = time.monotonic()
            if updater and now - last_update >= 0.6:
                try:
                    if message_id is None:
                        # 首建卡片用本次内容（进度路径下 parts 可能为空）
                        message_id = sender(chat_id, text or '生成中…')
                        # 首张卡片可见，可能仅包含处理进度。
                        perf_record('gw.first_card', time.perf_counter() - _t_entry)
                    elif self._card_patch_gate():
                        updater(message_id, text)
                        last_update = now
                    last_sent = text
                except Exception as exc:
                    stream_failed = True
                    print(f'[feishu] stream update failed, falling back: {exc}')

        def on_delta(part: str) -> None:
            parts.append(part)
            update_partial(''.join(parts))

        def on_progress(status: dict) -> None:
            elapsed = status.get('elapsed_text') \
                or format_duration(status.get('elapsed', 0))
            if status.get('phase') == 'tool':
                label = f"正在执行 {status.get('tool') or '工具'}"
            else:
                label = '正在处理'
            update_partial(f'{label} · 已用时 {elapsed}\n\n' + ''.join(parts))

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
        duration = getattr(self.api, 'last_duration', None)
        if duration:
            reply = f'{reply}\n\n已完成 · 总用时 {duration}'
        if message_id and updater and not stream_failed:
            if reply == last_sent:
                return   # 卡片已显示最终内容，无需重复 PATCH 或重发
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
        from turn_progress import relative_time
        lines = []
        for index, row in enumerate(rows, start + 1):
            timestamp = relative_time(row.get('updated'))
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
        if row['id'] == self.bindings.get(chat_id):
            # /resume 直达最近会话时，最近会话往往就是当前绑定：如实提示而非假装切换
            self._reply(chat_id, f"已绑定该会话\n主题: {row['title']}")
            return
        workspace = self.api.get_workspace(row['id'])
        with self._bindings_lock:
            self.bindings[chat_id] = row['id']
            self._save_bindings()
        self._session_confirmation.pop(chat_id, None)
        self._invalidate_workspace_view(chat_id)
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
        elif action == 'fork' and index < len(rows):
            # 卡片上的 fork 入口：与 /fork 命令同一条 API 链路，原会话保留并切换绑定
            try:
                new_id = self.api.fork_session(rows[index]['id'])
            except Exception as exc:
                self._reply(chat_id, f'[fork] 分叉失败: {exc}')
                return
            with self._bindings_lock:
                self.bindings[chat_id] = new_id
                self._save_bindings()
            self._invalidate_workspace_view(chat_id)
            self._reply(chat_id, f'已分叉出新会话 {new_id}（原会话保留），已切换。')
        else:
            self._reply(chat_id, '没有这一页，请重新 /sessions 查看会话列表。')

    def _handle_workspace_action(self, chat_id, value, sender_id):
        view = self._workspace_view(chat_id)
        if not view or view[0] != value.get('workspace_view'):
            self._reply(chat_id, '工作区列表已失效，请重新 /new。')
            return
        index = value.get('index')
        if type(index) is not int or not 0 <= index < len(view[1]):
            self._reply(chat_id, '无效的工作区按钮，请重新 /new。')
            return
        choice = view[1][index]
        if not choice.get('exists', True):
            self._reply(chat_id, f"目录不存在：{choice['path']}")
            return
        if not choice.get('allowed', True):
            self._reply(chat_id,
                        '该目录未授权；请把它加入 HARNESS_WORKSPACE_ROOTS 后重启服务。')
            return
        try:
            self._start_session_at(chat_id, choice['path'])
        except Exception as exc:
            self._reply(chat_id, f'[new] 创建失败: {exc}')

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
            if indices == ['all']:
                selected = list(rows)
            elif not indices or any(not n.isdigit() or not 1 <= int(n) <= len(rows)
                                    for n in indices):
                self._reply(chat_id, '请输入列表中的序号，例如 select 1 3；或 select all 全选。')
                return
            else:
                selected = [rows[i] for i in sorted({int(n) - 1 for n in indices})]
            self._session_confirmation[chat_id] = (listing[1], selected, time.monotonic())
            confirmation = '/confirm 删除（不可恢复）' if listing[1] == 'delete' else '/confirm'
            self._reply(chat_id, '待处理会话：\n' + self._session_rows_text(selected)
                        + f'\n\n确认: {confirmation}\n取消: /cancel')
            return
        from session_cleanup import cleanup_candidates
        if command == '/delete':
            if argument:
                self._reply(chat_id, '用法: /delete，或 /delete select <序号...|all>')
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
        self._reply(chat_id, self._session_rows_text(rows)
                    + f'\n\n{command} select <序号...> 多选，或 {command} select all 全选。'
                    if rows else '没有符合条件的会话。当前绑定的会话不可处理；/archive all 查看其他会话。')

    def _change_policy(self, chat_id: str, argument: str) -> None:
        """/sandbox：读写 Agent 服务端的执行/审批档位（网关只转发，不自行判定）。"""
        # 与 REPL 一致：档位名与 approvals 关键字都大小写不敏感（服务端 casefold 读）
        tokens = [token.casefold() for token in argument.split()]
        usage = ('用法: /sandbox [read-only|workspace-write|danger-full-access]\n'
                 '或 /sandbox approvals [untrusted|on-request|never]')
        try:
            if not tokens:
                policy = self.api.get_policy()
            elif tokens[0] == 'approvals' and len(tokens) == 2:
                policy = self.api.set_policy(approval_policy=tokens[1])
            elif tokens[0] != 'approvals' and len(tokens) == 1:
                policy = self.api.set_policy(sandbox_mode=tokens[0])
            else:
                self._reply(chat_id, usage)
                return
        except Exception as exc:   # 档位名写错时服务端 400 里带可选值，转给用户
            self._reply(chat_id, f'[sandbox] {exc}')
            return
        self._reply(chat_id, f"执行档位: {policy['sandbox_mode']}\n"
                      f"审批档位: {policy['approval_policy']}\n"
                      '注意：以上是策略级判定（所有执行入口同一套规则），不是 OS 级沙箱。')

    def _model_summary(self) -> str:
        main = llm_route.target_for('main')
        return main.ref if main is not None else os.getenv('LLM_MODEL_ID', '未配置')

    def _send_panel(self, chat_id: str, *, intro: str = '') -> None:
        """控制台卡片；没有卡片能力的传输层回退成文字说明。"""
        if self.transport is None:
            return
        session_id = self.bindings.get(chat_id) or ''
        workspace = ''
        if session_id:
            try:
                workspace = self.api.get_workspace(session_id)
            except Exception:
                workspace = ''
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, control_card(session_id=session_id,
                                             workspace=workspace,
                                             model=self._model_summary(),
                                             intro=intro))
                return
            except Exception as exc:
                print(f'[feishu] panel card failed, fallback to text: {exc}')
        self._reply(chat_id, (intro + chr(10) if intro else '')
                    + '直接发消息即可；/new 新会话 · /sessions 会话列表 · '
                      '/help 全部命令')

    def _handle_panel_action(self, chat_id: str, action: str,
                             sender_id: str | None = None) -> None:
        action = str(action or '')
        if action == 'new':
            self._handle_new(chat_id, '')
        elif action == 'sessions':
            self._show_sessions(chat_id, self.api.list_sessions(limit=1000))
        elif action == 'stop':
            self._panel_stop(chat_id)
        elif action == 'status':
            self._panel_status(chat_id)
        else:
            self._reply(chat_id, self._help_text())

    def _panel_stop(self, chat_id: str) -> None:
        session_id = self.bindings.get(chat_id)
        if not session_id:
            self._reply(chat_id, '当前聊天还没有会话。')
            return
        try:
            result = self.api.interrupt(session_id)
        except Exception as exc:
            self._reply(chat_id, f'[stop] {exc}')
            return
        status = result.get('status')
        self._reply(chat_id, {
            'cancelling': '已请求取消当前回合。',
            'idle': '当前没有正在执行的回合。',
        }.get(status, f'状态: {status}'))

    def _panel_status(self, chat_id: str) -> None:
        session_id = self.bindings.get(chat_id)
        if not session_id:
            self._reply(chat_id, '还没有会话；直接发一条消息就会自动创建。')
            return
        lines = [f'会话 {session_id[:8]}', f'模型 {self._model_summary()}']
        try:
            lines.append(f'工作区 {self.api.get_workspace(session_id)}')
        except Exception as exc:
            lines.append(f'工作区不可用: {exc}')
        self._reply(chat_id, chr(10).join(lines))

    def _handle_cron(self, chat_id: str, argument: str) -> None:
        """/cron：查看/创建/删除定时任务；cron 表达式由服务端校验。"""
        usage = chr(10).join([
            '用法:',
            '/cron                     列出定时任务',
            '/cron add "<分 时 日 月 周>" <提示词>',
            '/cron delete <job_id>',
        ])
        text = argument.strip()

        if not text or text.casefold() in ('list', 'ls'):
            try:
                jobs = self.api.list_crons()
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            if not jobs:
                self._reply(chat_id, '（没有定时任务）')
                return
            lines = ['定时任务:']
            for job in jobs:
                kind = 'recurring' if job.get('recurring') else 'one-shot'
                lines.append(f"  {job['id']}: '{job['cron']}' → "
                             f"{job.get('prompt', '')[:40]} [{kind}]")
            self._reply(chat_id, chr(10).join(lines))
            return

        action, _, rest = text.partition(' ')
        action = action.casefold()
        rest = rest.strip()
        if action in ('add', 'create'):
            expr, prompt = _split_cron_add(rest)
            if not expr or not prompt:
                self._reply(chat_id, usage)
                return
            try:
                job = self.api.create_cron(expr, prompt)
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            self._reply(chat_id, f"已创建 {job['id']}: '{job['cron']}' → "
                                 f"{job['prompt']}")
            return
        if action in ('delete', 'del', 'rm'):
            if not rest:
                self._reply(chat_id, usage)
                return
            try:
                self.api.delete_cron(rest)
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            self._reply(chat_id, f'已删除 {rest}')
            return
        self._reply(chat_id, usage)

    def _help_text(self) -> str:
        return '\n'.join([
            '可用命令:',
            '/new [序号|路径|ws:名称|id:会话ID]  选择工作区新建会话并绑定到本聊天',
            '/workspaces            列出已登记工作区',
            '/workspace add|remove  登记或移除工作区（add 支持 --name 名称）',
            '/sessions [页码]       会话卡片列表、详情与切换',
            '/resume [关键词或序号]  搜索并切换会话（/use 兼容）',
            '/archive [天数|empty|all|restore]  选择归档或恢复会话',
            '/archive select <序号...|all>  多选后预览归档或恢复',
            '/delete                查看可永久删除的归档',
            '/delete select <序号...|all>  多选后预览删除',
            '/confirm、/cancel      确认或取消待执行操作',
            '/cd [dir]              查看/切换当前会话工作区',
            '/pwd                   查看当前会话工作区',
            '/model                  查看当前模型配置',
            '/approvals、/perms       查看待审批操作',
            '/revoke                 撤销当前会话已允许的同类操作',
            '/sandbox [档位|approvals <档位>]  查看/切换执行与审批档位',
            '/cron [list|add "<表达式>" <提示词>|delete <id>]  定时任务',
            '/resume                 直达最近会话（带前缀/关键词则模糊匹配）',
            '/fork [事件序号]         从当前会话分叉出新会话（原会话保留）',
            '/find <关键词>           跨会话搜索历史内容',
            '/rename <新主题>        设置当前会话主题',
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

    def _bound_workspace(self, chat_id: str) -> str:
        session_id = self.bindings.get(chat_id)
        if not session_id:
            return ''
        try:
            return self.api.get_workspace(session_id)
        except Exception:
            return ''

    def _remember_workspace_view(self, chat_id: str, choices) -> str:
        token = secrets.token_urlsafe(12)
        self._workspace_views[chat_id] = (token, list(choices), time.monotonic())
        return token

    def _workspace_view(self, chat_id: str):
        view = self._workspace_views.get(chat_id)
        if view is None:
            return None
        if time.monotonic() - view[2] > WORKSPACE_VIEW_TTL:
            self._workspace_views.pop(chat_id, None)
            return None
        return view

    def _workspace_choices_now(self, chat_id: str) -> list[dict]:
        view = self._workspace_view(chat_id)
        return list(view[1]) if view else []

    def _invalidate_workspace_view(self, chat_id: str) -> None:
        self._workspace_views.pop(chat_id, None)

    def _send_workspace_view(self, chat_id, card, fallback):
        for name in ('send_workspace_card', 'send_session_card'):
            sender = getattr(self.transport, name, None)
            if sender is None:
                continue
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] workspace card failed, fallback to text: {exc}')
                break
        self._reply(chat_id, fallback)

    def _resolve_workspace_target(self, chat_id, selector, choices, current):
        selector = selector.strip()
        if not selector:
            self._reply(chat_id, '用法：/new ws:<序号|名称|路径>')
            return None
        if not choices:
            # 没先 /new 也可以按名称直接指定：补一次当前快照，只是不展示列表。
            listing = self.api.list_workspaces(current=current or None)
            choices = listing.get('workspaces') or []
        try:
            return find_workspace(choices, selector)['path']
        except ValueError as exc:
            candidate = Path(selector).expanduser()
            if not candidate.is_absolute():
                candidate = Path(current or '.').expanduser() / candidate
            if candidate.is_dir():
                return str(candidate.resolve())
            self._reply(chat_id, f'[workspace] {exc}')
            return None

    def _start_session_at(self, chat_id, workspace, session_id=None):
        created = self.api.create_session(session_id, workspace=workspace)
        with self._bindings_lock:
            self.bindings[chat_id] = created
            self._save_bindings()
        self._invalidate_workspace_view(chat_id)
        self._reply(chat_id,
                    f'已创建新会话，工作区：{self.api.get_workspace(created)}')
        return created

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        return bool(value) and (value.startswith(('.', '~', '/', '\\'))
                                or ':' in value or '/' in value or '\\' in value)

    def _handle_new(self, chat_id: str, argument: str) -> None:
        """新建会话：先列工作区候选再按序号选，也接受路径、名称与会话 ID。

        不带参数只列候选、不建会话；候选由 API 按「当前目录 + 登记表 +
        最近会话」合并。序号与卡片只在最近一次列表内有效（10 分钟）。
        """
        current = self._bound_workspace(chat_id)
        if not argument:
            listing = self.api.list_workspaces(current=current or None)
            choices = listing.get('workspaces') or []
            if not choices:
                self._reply(chat_id, '暂无可选工作区；先发消息或 /cd <路径> 创建。')
                return
            token = self._remember_workspace_view(chat_id, choices)
            lines = ['新建会话 · 选择工作区']
            for index, choice in enumerate(choices, 1):
                marks = []
                if choice.get('current'):
                    marks.append('当前')
                if not choice.get('exists', True):
                    marks.append('目录不存在')
                elif not choice.get('allowed', True):
                    marks.append('未授权')
                mark = f"（{'，'.join(marks)}）" if marks else ''
                lines.append(f"{index}. {choice['path']}{mark} · {choice['sessions']} 个会话")
            lines.append('回复 /new <序号> 选择；也可 /new ws:<名称|序号>、'
                         '/new <路径> 或 /new id:<会话ID>。')
            self._send_workspace_view(chat_id, workspace_card(choices, token),
                                      '\n'.join(lines))
            return
        value = argument.strip().strip('"\'')
        choices = self._workspace_choices_now(chat_id)
        session_id = None
        workspace = None
        lower = value.casefold()
        if lower.startswith('id:'):
            session_id = value[3:].strip()
            workspace = current or None   # 兼容旧语义：会话 ID 沿用当前工作区
        elif lower.startswith('ws:'):
            workspace = self._resolve_workspace_target(
                chat_id, value[3:].strip(), choices, current)
            if workspace is None:
                return
        elif value.isdigit():
            # 序号只对最近一次 /new 的列表有效；过期后不悄悄当会话 ID 用。
            if not choices or not 0 < int(value) <= len(choices):
                self._reply(chat_id, '工作区列表已失效，请重新 /new 后再选。')
                return
            workspace = choices[int(value) - 1]['path']
        elif self._looks_like_path(value):
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = Path(current or '.').expanduser() / candidate
            workspace = str(candidate)
        else:
            if not choices:
                listing = self.api.list_workspaces(current=current or None)
                choices = listing.get('workspaces') or []
            match = next((row for row in choices
                          if str(row.get('name') or '').casefold() == value.casefold()),
                         None)
            if match is not None:
                workspace = match['path']
            else:
                workspace = current or None
                session_id = value
        if session_id is not None and (
                not session_id
                or not session_id.replace('-', '').replace('_', '').isalnum()):
            self._reply(chat_id, '会话 ID 只能包含字母、数字、- 和 _。')
            return
        self._start_session_at(chat_id, workspace, session_id)

    def _handle_workspaces(self, chat_id: str, command: str, argument: str) -> None:
        parts = argument.split(maxsplit=1)
        action = parts[0].lower() if parts else 'list'
        current = self._bound_workspace(chat_id)
        if command == '/workspaces' or action in ('', 'list'):
            listing = self.api.list_workspaces(current=current or None)
            saved = listing.get('saved') or [
                row for row in listing.get('workspaces') or [] if row.get('saved')]
            if not saved:
                self._reply(chat_id, '尚未登记工作区。'
                            '用 /workspace add <路径> [--name 名称] 登记。')
                return
            lines = ['已登记工作区：']
            for index, row in enumerate(saved, 1):
                mark = '（当前）' if row.get('current') else ''
                lines.append(f"{index}. {row['name']}  {row['path']}{mark}"
                             f" · {row['sessions']} 个会话")
            lines.append('新增 /workspace add <路径> [--name 名称]；'
                         '移除 /workspace remove <序号|名称|路径>。')
            self._reply(chat_id, '\n'.join(lines))
            return
        if action == 'add':
            text = parts[1].strip() if len(parts) > 1 else ''
            name = None
            match = re.search(r'\s+--name\s+(.+)$', text)
            if match:
                name = match.group(1).strip().strip('"\'')
                text = text[:match.start()].strip()
            text = text.strip('"\'')
            if not text:
                self._reply(chat_id, '用法：/workspace add <路径> [--name 名称]')
                return
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = Path(current or '.').expanduser() / candidate
            try:
                entry = self.api.add_workspace(str(candidate), name)
            except Exception as exc:
                self._reply(chat_id, f'[workspace] {exc}')
                return
            self._invalidate_workspace_view(chat_id)
            self._reply(chat_id, f"已登记工作区：{entry['name']}  {entry['path']}")
            return
        if action == 'remove':
            selector = parts[1].strip() if len(parts) > 1 else ''
            if not selector:
                self._reply(chat_id, '用法：/workspace remove <序号|名称|路径>')
                return
            listing = self.api.list_workspaces(current=current or None)
            saved = listing.get('saved') or [
                row for row in listing.get('workspaces') or [] if row.get('saved')]
            try:
                row = find_workspace(saved, selector)
                removed = self.api.remove_workspace(row['id'])
            except Exception as exc:
                self._reply(chat_id, f'[workspace] {exc}')
                return
            self._invalidate_workspace_view(chat_id)
            self._reply(chat_id, f"已移除登记：{removed['name']}  {removed['path']}")
            return
        self._reply(chat_id, '用法：/workspaces；/workspace add <路径> [--name 名称]；'
                             '/workspace remove <序号|名称|路径>')

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
                self._handle_new(chat_id, argument)
            elif command in ('/workspaces', '/workspace'):
                self._handle_workspaces(chat_id, command, argument)
            elif command == '/sessions':
                if argument.isdigit() and int(argument) > 0:
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
                    if command == '/resume':
                        rows = self.api.list_sessions(limit=1)
                        if not rows:
                            self._reply(chat_id, '还没有任何会话，直接发消息即可创建。')
                            return
                        self._switch_session(chat_id, rows[0])   # /resume 直达最近会话
                        return
                    self._reply(chat_id, '用法: /use <会话ID前缀>'
                                          '\n（/sessions 可查，前缀唯一即可；/resume 直达最近会话）')
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
                    self._invalidate_workspace_view(chat_id)
                    self._reply(chat_id, f'当前工作区: {self.api.get_workspace(session_id)}'
                                '\n已创建新会话，后续对话和 /run 使用此目录。')
                    return
                if not argument:
                    self._reply(chat_id, f'当前工作区: {self.api.get_workspace(session_id)}')
                    return
                workspace = self.api.set_workspace(session_id, argument)
                self._invalidate_workspace_view(chat_id)
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
                    duration = _format_task_duration(task)
                    self._reply(chat_id, f"{task['id']} {task['status']} "
                                          f"exit={task.get('exit_code')} 总用时={duration}")
                else:
                    lines = [f"{t['id']} {t['status']} exit={t.get('exit_code')} "
                             f"总用时={_format_task_duration(t)}"
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
                pending = [req for req in self.api.pending_permissions()
                           if self._request_belongs_to(chat_id, req)]
                if not pending:
                    self._reply(chat_id, '(无待审批请求)')
                    return
                # 手动查看也重建 FIFO：后面 y2/n2 的序号与这里显示的一致
                self._chat_pending[chat_id] = [req['req_id'] for req in pending]
                for i, req in enumerate(pending, 1):
                    self._push_approval(chat_id, req, i)
            elif command == '/revoke':
                if argument:
                    self._reply(chat_id, '用法: /revoke')
                    return
                try:
                    count = self.api.revoke_permissions(self.bindings.get(chat_id))
                except Exception as exc:
                    self._reply(chat_id, f'[revoke] {exc}')
                    return
                self._reply(chat_id, f'已撤销 {count} 条当前会话预授权。')
            elif command == '/model':
                main = llm_route.target_for('main')
                if main is not None:
                    model_text = main.ref
                    base_text = main.base_url or '默认地址'
                else:
                    model_text = os.getenv('LLM_MODEL_ID', '未配置')
                    base_text = os.getenv('LLM_BASE_URL', '默认地址')
                self._reply(chat_id, f'当前模型: {model_text}\n服务地址: {base_text}')
            elif command == '/sandbox':
                self._change_policy(chat_id, argument)
            elif command == '/cron':
                self._handle_cron(chat_id, argument)
            elif command == '/fork':
                sid = self.bindings.get(chat_id)
                if not sid:
                    self._reply(chat_id, '当前聊天没有绑定会话；先发消息或 /resume。')
                    return
                upto = int(argument) if argument.strip().isdigit() else None
                try:
                    new_id = self.api.fork_session(sid, upto)
                except Exception as exc:
                    self._reply(chat_id, f'[fork] 分叉失败: {exc}')
                    return
                with self._bindings_lock:
                    self.bindings[chat_id] = new_id
                    self._save_bindings()
                self._reply(chat_id, f'已分叉出新会话 {new_id}（原会话 {sid[:12]} 保留），已切换。')
            elif command == '/find':
                if not argument:
                    self._reply(chat_id, '用法: /find <关键词>（跨会话搜索历史内容）')
                    return
                rows = self.api.search_sessions(argument, limit=5)
                if not rows:
                    self._reply(chat_id, f'没有包含「{argument}」的会话。')
                    return
                from turn_progress import relative_time
                lines = [f"{row['id'][:12]} · {relative_time(row.get('updated'))} · "
                         f"{row['title']}\n    {row.get('matched', '')}" for row in rows]
                self._reply(chat_id, '\n\n'.join(lines))
            elif command == '/rename':
                sid = self.bindings.get(chat_id)
                if not sid:
                    self._reply(chat_id, '当前聊天没有绑定会话；先发消息或 /resume。')
                    return
                title = self.api.rename_session(sid, argument.strip())
                self._reply(chat_id, f'会话主题已设置为：{title}')
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
                if tokens[0] not in self._chat_pending.get(chat_id, []):
                    self._reply(chat_id, '该审批不属于当前聊天（或已失效），用 /perms 查看。')
                    return
                record = self.api.resolve_permission(tokens[0], tokens[1] == 'y')
                self._forget_pending(record['req_id'])
                self._reply(chat_id, permission_result(record))
            elif command == '/quit':
                with self._bindings_lock:
                    self.bindings.pop(chat_id, None)
                    self._save_bindings()
                self._invalidate_workspace_view(chat_id)
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

    def _request_belongs_to(self, chat_id: str, req: dict) -> bool:
        """审批归属登记时的会话；无会话上下文的请求（后台任务）对所有聊天可见。"""
        session_id = req.get('session_id') or ''
        bound = self.bindings.get(chat_id)
        return not session_id or (bool(bound) and bound == session_id)

    def poll_permissions(self) -> None:
        """把新出现的 pending 请求推给绑定其会话的聊天（由定时线程调用）。

        后台任务没有会话上下文（session_id 为空），仍推给所有聊天；有会话的
        请求一旦广播，任何绑定聊天都能替别的会话批操作——这是审批归属漏洞。
        """
        try:
            pending = self.api.pending_permissions()
        except Exception:
            return
        with self._bindings_lock:
            bindings = dict(self.bindings)
        for req in pending:
            req_id = req['req_id']
            if req_id in self._notified:
                continue
            session_id = req.get('session_id') or ''
            targets = ([chat_id for chat_id, bound in bindings.items()
                        if bound == session_id] if session_id
                       else list(bindings))
            if not targets:
                # 会话还没绑定到任何聊天：先不标记，等绑定出现后的下一轮再推
                continue
            self._notified.add(req_id)
            for chat_id in targets:
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
        for fifo in self._chat_pending.values():
            fifo[:] = [req_id for req_id in fifo if req_id in pending_ids]

    def _push_approval(self, chat_id: str, req: dict, index: int) -> None:
        """推送审批请求：优先带按钮的卡片，发送失败回退纯文本提示。"""
        sender = getattr(self.transport, 'send_approval_card', None)
        if sender:
            try:
                sender(chat_id, format_permission_prompt(req, index), req)
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

        返回 (toast 文案, 是否成功)。open_id 受用户白名单约束；审批按钮
        还必须属于本聊天已收到的请求。
        """
        if self.allowed_users is not None and open_id not in self.allowed_users:
            return ('⛔ 你没有会话操作权限'
                    if (value or {}).get('session_view') or (value or {}).get('workspace_view')
                    else '⛔ 你没有审批权限'), False
        if (value or {}).get('workspace_view'):
            view = self._workspace_view(chat_id)
            index = value.get('index')
            if not view or view[0] != value['workspace_view']:
                return '工作区列表已失效，请重新 /new', False
            if type(index) is not int or not 0 <= index < len(view[1]):
                return '无效的工作区按钮，请重新 /new', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在创建会话…', True
        if (value or {}).get('session_view'):
            view = self._session_views.get(chat_id)
            if not view or view[0] != value['session_view'] or time.monotonic() - view[2] > 600:
                return '会话列表已失效，请重新 /sessions', False
            index, action = value.get('index'), value.get('action')
            limit = ((len(view[1]) + PAGE_SIZE - 1) // PAGE_SIZE if action == 'page' else len(view[1]))
            if action not in ('page', 'details', 'switch', 'fork') or type(index) is not int or not 0 <= index < limit:
                return '无效的会话按钮，请重新 /sessions', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理会话操作…', True
        if (value or {}).get('panel'):
            try:
                self._events.put_nowait((chat_id, value, open_id, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在打开…', True
        req_id = (value or {}).get('approval')
        if not req_id:
            return '⚠ 无效的审批按钮', False
        if not chat_id or req_id not in self._chat_pending.get(chat_id, []):
            # 按钮只对「推送过这条审批的聊天」有效。群成员或别的单聊即使拿到
            # req_id，也不能替绑定会话决议。
            return '⚠ 该审批不属于当前聊天，请用 /perms 查看', False
        allow = bool((value or {}).get('allow'))
        scope = (value or {}).get('scope')
        try:
            record = self.api.resolve_permission(req_id, allow, scope=scope)
        except Exception as exc:
            print(f'[feishu] approval failed: {exc}')
            return '⚠ 操作未能完成，请用 /perms 查看最新审批状态', False
        self._forget_pending(req_id)
        verb = '允许' if allow else '拒绝'
        session_note = '，本会话同类自动通过' if (allow and scope == 'session') else ''
        updater = getattr(self.transport, 'update_markdown', None)
        if updater and message_id:
            try:
                updater(message_id, permission_result(record) + session_note)
            except Exception as exc:
                print(f'[feishu] approval card update failed: {exc}')
        return f'已{verb}{session_note}', True

    # ---------- 回合终态主动通知（TUI/API/cron 发起的回合） ----------

    def _watched_sessions(self) -> list[str]:
        with self._bindings_lock:
            return sorted(set(self.bindings.values()))

    def _chat_for_session(self, session_id: str) -> str | None:
        with self._bindings_lock:
            for chat_id, bound in self.bindings.items():
                if bound == session_id:
                    return chat_id
        return None

    def _watch_fetch(self, session_id: str, after: int):
        """拉一次增量：复用 SSE 的 after 游标 + 一次性快照模式。"""
        events: list[dict] = []
        latest = after
        with self.api.http.stream(
                'GET', f'/v1/sessions/{session_id}/stream',
                params={'after': after, 'once': 1, 'poll': 0.05}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith('data:'):
                    continue
                payload = json.loads(line[5:].strip())
                if 'seq' in payload:
                    latest = max(latest, int(payload['seq']))
                    events.append(payload)
        return events, latest

    def _watch_notify(self, session_id: str, kind: str, payload: dict) -> None:
        chat_id = self._chat_for_session(session_id)
        if chat_id is None or self.transport is None:
            raise RuntimeError('no bound chat for session')
        turn = str(payload.get('turn_id') or '')[:8]
        short = session_id[:8]
        if kind == 'stalled':
            minutes = max(1, round(float(payload.get('elapsed_s') or 0) / 60))
            head = f'⏳ 回合仍在运行（约 {minutes} 分钟），可能在等待审批或卡住。'
            text = head + chr(10) + f'会话 {short} · turn {turn}'
        else:
            status = str(payload.get('status') or 'completed')
            source = str(payload.get('source') or '?')
            if status == 'completed':
                head = '✅ 回合完成'
            elif status == 'cancelled':
                head = '⏹ 回合已取消'
            else:
                head = '⚠️ 回合失败（' + str(payload.get('error_type') or 'error') + '）'
            text = head + chr(10) + f'会话 {short} · turn {turn} · 来源 {source}'
        self.transport.send_text(chat_id, text)

    def start_watcher(self) -> bool:
        return self.watcher.start()

    def stop_watcher(self, timeout: float = 5.0) -> bool:
        return self.watcher.stop(timeout=timeout)

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
        try:
            self.stop_watcher()
        except Exception as exc:
            print(f'[feishu] watcher stop failed: {exc}')
        for thread in self._dispatch_threads:
            thread.join(timeout=3)
        self._dispatch_threads.clear()

    # ---------- WS 事件分发（长任务不阻塞回调线程） ----------

    def dispatch(self, chat_id: str, text: str,
                 sender_id: str | None = None,
                 chat_type: str | None = None) -> None:
        """把飞书事件投递到分发队列（回调线程立即返回）。"""
        norm = unicodedata.normalize('NFKC', text or '').strip().lower()
        is_approval = (norm.split()[:1] == ['/approve'] or
                       (re.fullmatch(r'(y|n)(\d{0,2})', norm) is not None
                        and bool(self._chat_pending.get(chat_id))))
        events = self._approval_events if is_approval else self._events
        try:
            events.put_nowait((chat_id, text, sender_id, chat_type))
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
                chat_id, text, sender_id, chat_type = item
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
                        self.on_message(chat_id, text, sender_id=sender_id,
                                        chat_type=chat_type)
                    else:
                        with self._chat_lock(chat_id):
                            if isinstance(text, dict):
                                if text.get('panel'):
                                    self._handle_panel_action(chat_id, text['panel'], sender_id)
                                elif text.get('workspace_view'):
                                    self._handle_workspace_action(chat_id, text, sender_id)
                                else:
                                    self._handle_session_action(chat_id, text, sender_id)
                            else:
                                self.on_message(chat_id, text, sender_id=sender_id,
                                                chat_type=chat_type)
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
                chat_type = getattr(message, 'chat_type', None)
                print(f'[feishu] message from {sender_id!r} '
                      f'in chat {message.chat_id!r} ({chat_type})')
                self.dispatch(message.chat_id, text,
                              sender_id=sender_id, chat_type=chat_type)
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
                from lark_oapi.event.callback.model.p2_card_action_trigger import (
                    CallBackToast,
                    P2CardActionTriggerResponse,
                )
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
        self.start_watcher()
        print('[feishu] long connection started; Ctrl+C to exit')
        ws_client.start()   # blocks


REQUIRED_ENV = ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'HARNESS_API_TOKEN')


def check_setup(environ=None) -> list[str]:
    """离线检查启动所需配置；返回问题列表（空表示可以启动）。"""
    env = os.environ if environ is None else environ
    problems = [f'缺少 {key}' for key in REQUIRED_ENV
                if not (env.get(key) or '').strip()]
    allowed = (env.get('FEISHU_ALLOWED_USERS') or '').strip()
    allow_any = (env.get('FEISHU_ALLOW_ANY_USER') or '').strip().lower()         in ('1', 'true', 'yes')
    if not allowed and not allow_any:
        problems.append('缺少 FEISHU_ALLOWED_USERS（本地测试可设 FEISHU_ALLOW_ANY_USER=1）')
    return problems


def _print_setup_help() -> None:
    print('飞书接入步骤（详见 docs/FEISHU_SETUP.md）：')
    print('  1. 飞书开放平台创建企业自建应用，开启「机器人」+「长连接接收事件」')
    print('  2. 订阅 im.message.receive_v1，添加卡片回调；权限 im:message')
    print('  3. 启动 API：uvicorn api:create_app --factory --port 8000')
    print('  4. 启动网关：python src/feishu_gateway.py')
    print('  首次私聊机器人会回你的 open_id，把它填进 FEISHU_ALLOWED_USERS 即可。')


def main(argv=()) -> int:
    import argparse

    parser = argparse.ArgumentParser(description='Feishu gateway')
    parser.add_argument('--check', action='store_true',
                        help='只检查配置，不连接飞书')
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()   # 与 harness 共用项目根的 .env（FEISHU_*/HARNESS_* 都可放这里）
    if args.check:
        problems = check_setup()
        for problem in problems:
            print(f'[missing] {problem}')
        if not problems:
            print('[ok] 配置齐全，可以启动网关')
            print(f'  API: {os.getenv("HARNESS_API_URL", "http://127.0.0.1:8000")}')
        _print_setup_help()
        return 0 if not problems else 1
    app_id = os.getenv('FEISHU_APP_ID', '')
    app_secret = os.getenv('FEISHU_APP_SECRET', '')
    token = os.getenv('HARNESS_API_TOKEN', '')
    base_url = os.getenv('HARNESS_API_URL', 'http://127.0.0.1:8000')
    allowed_raw = os.getenv('FEISHU_ALLOWED_USERS', '').strip()
    allowed_users = {u.strip() for u in allowed_raw.split(',') if u.strip()} \
        or None
    allow_any = os.getenv('FEISHU_ALLOW_ANY_USER', '').strip().lower() \
        in ('1', 'true', 'yes')
    if not (app_id and app_secret and token):
        print('FEISHU_APP_ID / FEISHU_APP_SECRET / HARNESS_API_TOKEN are required')
        return 1
    if not allowed_users and not allow_any:
        # 机器人背后是真 shell：没有名单就不启动，避免“默认谁都能用”。
        print('FEISHU_ALLOWED_USERS is required (comma-separated open_id). '
              'Set FEISHU_ALLOW_ANY_USER=1 only for local testing.')
        return 1
    api = GatewayClient(base_url, token)
    transport = LarkTransport(app_id, app_secret)
    gateway = FeishuGateway(api, transport=transport,
                            allowed_users=allowed_users)
    if allowed_users:
        print(f'[feishu] user allowlist active: {", ".join(sorted(allowed_users))}')
    else:
        print('[feishu] WARNING: FEISHU_ALLOW_ANY_USER=1 — anyone who can '
              'reach this bot can run commands on this machine')
    print('[feishu] p2p only: group/topic messages are ignored')
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
    sys.exit(main(sys.argv[1:]))
