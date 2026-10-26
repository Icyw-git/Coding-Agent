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
    /grants [add|remove]  查看/维护目录级持久写授权
    审批卡片按钮  —— 点【允许/拒绝】直接决议（首选，零输入）
    y / n、y2 / n2 —— 快捷审批：按序号决议本聊天的待审批请求
    /use <前缀>    —— 切换该聊天绑定的会话（/sessions 查列表，前缀唯一即可）
    /cd <dir>      —— 切换当前会话工作区（对话与 /run 同时生效）
    其余文本 → 作为消息发给绑定的 agent 会话
"""

from __future__ import annotations

import inspect
import io
import json
import os
import queue
import re
import secrets
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import httpx

import approval_view
import grants
import llm_route
import notify
import paths
import screen_capture
import tool_cache
import turn_screenshots
from exec_policy import CLIPBOARD_ENV, SCREEN_ENV, approval_class, clipboard_tools_enabled, screen_tools_enabled
from feishu_approval_card import approval_card, approval_text
from feishu_cron_card import cron_card, cron_delete_card, cron_text
from feishu_domain_card import domain_card, domain_text
from feishu_grant_card import grant_card, grant_text
from feishu_help_card import help_card
from feishu_panel import control_card
from feishu_sessions import PAGE_SIZE, detail_card, list_card, workspace_card
from feishu_step_card import step_card, truncated_note
from feishu_task_card import logs_card, tasks_card, tasks_text
from file_lock import FileLockTimeout, exclusive_file_lock
from gateway_sim import GatewayClient
from perf import record as perf_record
from perf import reset as perf_reset
from session_views import group_sessions, session_details
from turn_progress import format_duration
from turn_watcher import TurnWatcher, WatchState
from workspace_choices import find_workspace

BINDINGS_PATH = Path(os.getenv('FEISHU_BINDINGS') or paths.cache_path('feishu_bindings.json'))
# 单实例锁：两个网关同时跑会对同一条消息各回一次、把同一批推送发两遍。
# 个人部署只需要一个进程；确需并行（不同应用凭证）时用 FEISHU_ALLOW_MULTI=1 跳过。
INSTANCE_LOCK_PATH = Path(paths.cache_path('locks/feishu-gateway.lock'))
PERMISSION_POLL_INTERVAL = 3.0
HEARTBEAT_POLL_INTERVAL = 60.0   # 心跳是每日摘要，轮询间隔远大于审批
CRON_RESULT_POLL_INTERVAL = 15.0  # 定时任务结果：晚十几秒可接受，别每秒打 API
DISPATCH_WORKERS = 4   # 消息分发线程数：同一聊天内保序，不同聊天并行
WORKSPACE_VIEW_TTL = 600.0   # 工作区卡片/序号的有效期：过期必须重新 /new

# 卡片更新的合流：多会话并发逐秒刷新时，同一网关实例内合并到全局
# 最小间隔，避免飞书卡片 PATCH 接口被高频调用触发频控
MIN_CARD_PATCH_INTERVAL = 0.45

# 流式延迟诊断：FEISHU_STREAM_TRACE=1 时每个回合打印一行时间戳，
# 用来区分「模型慢」「SSE 慢」「卡片 PATCH 慢」。默认关闭，不刷屏。
STREAM_TRACE_ENV = 'FEISHU_STREAM_TRACE'


def stream_trace_enabled() -> bool:
    return (os.getenv(STREAM_TRACE_ENV) or '').strip().lower() in ('1', 'true', 'yes', 'on')

# 截图回传：飞书图片上传上限 10MB，单回合最多回传 4 张；可用环境变量关闭。
SCREENSHOT_REPLY_LIMIT = 4
SCREENSHOT_REPLY_ENV = 'FEISHU_SEND_SCREENSHOTS'
FEISHU_IMAGE_MAX_BYTES = 10 * 1024 * 1024

# 飞书发不出去时的本机兜底通知。远端故障一般是持续的（断网、凭证失效、
# 被移出群），每轮轮询都会失败，所以按事件键在窗口内只弹一次。
LOCAL_ALERT_WINDOW = 120.0
LOCAL_ALERT_ENV = 'FEISHU_LOCAL_ALERTS'
LOCAL_ALERT_BODY = 400


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


def parse_post_content(raw) -> tuple[str, list[str]]:
    """飞书富文本（post）→ (文本, 图片 key 列表)。

    兼容两种外形：直接的 `{title, content}`，以及按语言包装的
    `{zh_cn: {title, content}}`。链接保留为 `文字 (href)`，图片取 `image_key`。
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        return '', []
    if not isinstance(data, dict):
        return '', []
    if not ('content' in data or 'title' in data):
        for value in data.values():
            if isinstance(value, dict) and ('content' in value or 'title' in value):
                data = value
                break
    title = str(data.get('title') or '').strip()
    images: list[str] = []
    lines: list[str] = []
    for paragraph in data.get('content') or []:
        parts = []
        for element in paragraph if isinstance(paragraph, list) else []:
            if not isinstance(element, dict):
                continue
            tag = element.get('tag')
            if tag == 'img':
                key = element.get('image_key')
                if key:
                    images.append(str(key))
                parts.append('[图片]')
            elif tag == 'a':
                text = str(element.get('text') or '').strip()
                href = str(element.get('href') or '').strip()
                parts.append(f'{text} ({href})' if href else text)
            else:
                parts.append(str(element.get('text') or ''))
        line = ''.join(parts).strip()
        if line:
            lines.append(line)
    text = chr(10).join(([title] if title else []) + lines).strip()
    return text, images


def _attachment_filename(kind: str, key: str, name: str | None,
                         data: bytes) -> str:
    raw = str(name or '').replace(chr(92), '/').split('/')[-1].strip()
    safe = ''.join(ch if (ch.isalnum() or ch in '._-') else '_' for ch in raw)
    if safe and '.' in safe:
        return safe[:80]
    if kind == 'image':
        if data[:4] == bytes([0x89]) + b'PNG':
            ext = '.png'
        elif data[:3] == bytes([0xFF, 0xD8, 0xFF]):
            ext = '.jpg'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            ext = '.gif'
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ext = '.webp'
        else:
            ext = '.bin'
        return f'image_{safe or key[-8:]}{ext}'
    return (safe or f'file_{key[-8:]}')[:80]


def _accepts_kwarg(fn, name: str) -> bool:
    """传输层是否支持某个可选参数（测试替身/旧实现可能没有 reply_to）。"""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# 常见失败 → 一句能照着做的话。未知错误保留原文，不做猜测性改写。
_ERROR_HINTS = (
    (r'402|insufficient balance|insufficient_quota|quota exceeded',
     '模型 provider 余额/额度不足（402）。充值或换个模型后再试。'),
    (r'401|unauthorized|invalid api key|invalid_api_key',
     '模型鉴权失败（401）：API key 可能失效或被禁用。'),
    (r'403|forbidden|permission denied', '模型拒绝访问（403）：检查 key 的权限与地区限制。'),
    (r'429|rate limit|too many requests', '请求过于频繁（429）：等几秒再发一次。'),
    (r'timeout|timed out', '请求超时：网络或 provider 响应太慢，可直接重试。'),
    (r'turn_busy|another turn|409', '上一轮还在执行：等它结束，或点「停止这个回合」再发。'),
)


# 飞书是境内域名：走本机系统代理（Clash/VPN 等）既慢又易失败——代理没开时
# requests 会直接抛 ProxyError，连 open.feishu.cn 都到不了。这里把飞书域名加进
# NO_PROXY，其余流量（如 OpenAI）仍按原来的代理设置走。
_FEISHU_NO_PROXY = ('.feishu.cn', 'feishu.cn', 'open.feishu.cn',
                    'msg-frontier.feishu.cn')


def bypass_proxy_for_feishu() -> None:
    """让飞书域名绕过系统代理；FEISHU_USE_PROXY=1 时保持原样。"""
    if (os.getenv('FEISHU_USE_PROXY') or '').strip().lower() in ('1', 'true', 'yes'):
        return
    current = os.getenv('NO_PROXY') or os.getenv('no_proxy') or ''
    hosts = [item.strip() for item in current.split(',') if item.strip()]
    for host in _FEISHU_NO_PROXY:
        if host not in hosts:
            hosts.append(host)
    value = ','.join(hosts)
    os.environ['NO_PROXY'] = value
    # Windows 上 os.environ 的键不区分大小写，NO_PROXY 与 no_proxy 是同一个变量：
    # 这里绝不能 pop('no_proxy')，否则会把刚设置的值删掉（实测踩过）。
    if os.name != 'nt':
        os.environ['no_proxy'] = value


def friendly_error(exc: BaseException) -> str:
    """把 provider/HTTP 失败翻成一句可执行的提示；无法识别时返回空串。"""
    raw = str(exc or '').casefold()
    for pattern, hint in _ERROR_HINTS:
        if re.search(pattern, raw):
            return hint
    return ''


def _is_missing_session(exc: BaseException) -> bool:
    """把“会话不存在”从其它失败里分出来：404 / FileNotFoundError。"""
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, 'response', None)
    return getattr(response, 'status_code', None) == 404


_CRON_WRITE_FLAGS = ('--writes', '--allow-writes', '--side-effects')


def _split_cron_add(rest: str) -> tuple[str, str, bool]:
    """解析 `/cron add` 的参数：支持引号包住表达式，也支持五段裸写。

    cron 表达式本身含空格（5 段），所以不能直接 `partition(' ')`；引号形式
    `"0 9 * * *" 提示词` 与裸写 `0 9 * * * 提示词` 都接受。

    默认创建只读任务；`--writes` 必须写在最前面，表示显式授权定时回合产生
    副作用（写文件、联网、第三方工具）。返回值第三项就是这份授权。
    """
    text = rest.strip()
    allow_side_effects = False
    for flag in _CRON_WRITE_FLAGS:
        if text.casefold() == flag:
            return '', '', False
        if text.casefold().startswith(flag + ' '):
            allow_side_effects = True
            text = text[len(flag):].strip()
            break
    if text[:1] in (chr(34), chr(39)):
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return '', '', allow_side_effects
        return (text[1:end].strip(), text[end + 1:].strip(),
                allow_side_effects)
    parts = text.split(None, 5)
    if len(parts) < 6:
        return '', '', allow_side_effects
    return ' '.join(parts[:5]), parts[5].strip(), allow_side_effects


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

    def send_image(self, chat_id: str, image: bytes, *,
                   reply_to: str | None = None) -> str | None:
        """上传 PNG 并作为图片消息发送；reply_to 时引用原消息。"""
        self.validate_chat_id(chat_id)
        data = bytes(image)
        if not data.startswith(screen_capture.PNG_MAGIC):
            raise ValueError('feishu image must be a PNG screenshot')
        if len(data) > FEISHU_IMAGE_MAX_BYTES:
            raise ValueError(f'image too large for feishu: {len(data)} bytes')
        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )
        upload = CreateImageRequest.builder().request_body(
            CreateImageRequestBody.builder()
            .image_type('message')
            .image(io.BytesIO(data))
            .build()
        ).build()
        response = self._client.im.v1.image.create(upload)
        if not response.success():
            raise RuntimeError(
                f'feishu image upload failed: {response.code} {response.msg}')
        payload = getattr(response, 'data', None)
        image_key = getattr(payload, 'image_key', None) if payload else None
        if not image_key:
            raise RuntimeError('feishu image upload returned no image_key')
        content = json.dumps({'image_key': image_key})
        if reply_to:
            request = ReplyMessageRequest.builder().message_id(reply_to).request_body(
                ReplyMessageRequestBody.builder()
                .msg_type('image')
                .content(content)
                .build()
            ).build()
            response = self._client.im.v1.message.reply(request)
            action = 'reply'
        else:
            request = CreateMessageRequest.builder() \
                .receive_id_type('chat_id') \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type('image')
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            action = 'send'
        if not response.success():
            raise RuntimeError(
                f'feishu image {action} failed: {response.code} {response.msg}')
        payload = getattr(response, 'data', None)
        return getattr(payload, 'message_id', None) if payload else None

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

    def download_message_resource(self, message_id: str, key: str,
                                  kind: str) -> bytes:
        """下载消息里的图片/文件。

        必须用 `messages/{message_id}/resources/{key}`：消息里的 image_key 调
        `im/v1/images` 会报 234001 Invalid request param，这个接口才是消息资源入口。
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        request = (GetMessageResourceRequest.builder()
                   .message_id(message_id)
                   .file_key(key)
                   .type('image' if kind == 'image' else 'file')
                   .build())
        response = self._client.im.v1.message_resource.get(request)
        if not response.success():
            raise RuntimeError(
                f'feishu resource download failed: {response.code} {response.msg}')
        return self._read_bytes(response)

    @staticmethod
    def _read_bytes(response) -> bytes:
        data = getattr(response, 'file', None)
        if data is not None and hasattr(data, 'read'):
            return data.read()
        raw = getattr(response, 'raw', None)
        if raw is not None and hasattr(raw, 'read'):
            return raw.read()
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        raise RuntimeError('feishu download returned no content')

    def send_step_card(self, chat_id: str, card: dict,
                       reply_to: str | None = None) -> str | None:
        """schema 2.0 过程卡；reply_to 时用「回复消息」以带上引用。"""
        content = json.dumps(card, ensure_ascii=False)
        if reply_to:
            return self._reply_card(reply_to, content)
        return self._send_card(chat_id, content)

    def update_step_card(self, message_id: str, card: dict) -> None:
        self.update_markdown(message_id, json.dumps(card, ensure_ascii=False),
                             raw_content=True)

    def _reply_card(self, message_id: str, content: str) -> str | None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(
            ReplyMessageRequestBody.builder()
            .msg_type('interactive')
            .content(content).build()
        ).build()
        response = self._client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f'feishu reply failed: {response.code} {response.msg}')
        data = getattr(response, 'data', None)
        return getattr(data, 'message_id', None) if data else None

    def send_markdown(self, chat_id: str, md_text: str,
                      reply_to: str | None = None) -> str | None:
        """以交互卡片发送 markdown——飞书 text 消息不解析任何 markdown 语法，
        卡片的 markdown 元素才支持粗体/斜体/行内代码/链接。"""
        content = self._markdown_card(md_text)
        if reply_to:
            return self._reply_card(reply_to, content)
        return self._send_card(chat_id, content)

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

    def update_markdown(self, message_id: str, md_text: str,
                        *, raw_content: bool = False) -> None:
        """Update an existing interactive card in place.

        PATCH /im/v1/messages/:message_id 的 body 只收 content（卡片 JSON），
        带 msg_type 会报 230001 invalid msg_type——message.update 那条路不通。
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        request = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder()
            .content(md_text if raw_content else self._markdown_card(md_text)).build()
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
                 p2p_only: bool = True,
                 notifier=None) -> None:
        self.api = api
        self.transport = transport          # 需实现 send_text(chat_id, text)
        # 飞书发不出去时的兜底：人在机器旁至少能看到系统通知。默认关闭，
        # 由入口显式注入 notify.notify；测试不注入就不会弹真通知。
        self.notifier = notifier
        self._alerted: dict[str, float] = {}
        self._alert_lock = threading.Lock()
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
        # req_id → 登记时的请求体：合并审批卡片要还原每条的工具/原因/参数
        self._pending_requests: dict[str, dict] = {}
        # chat_id → 最近一次用户输入：失败卡片的「重试」用它，30 分钟内有效
        self._last_user: dict[str, dict] = {}
        # chat_id → 审批卡片 message_id：新请求/决议后 PATCH 同一张卡，不刷屏
        self._approval_cards: dict[str, str] = {}
        self._poll_stop = threading.Event()
        # 每聊天的待审批 FIFO：收到审批提醒后直接回复 y/n 即可，免输 req_id
        self._chat_pending: dict[str, list[str]] = {}
        self._session_choices: dict[str, list[dict]] = {}
        # chat_id → (token, rows, created_at)：卡片按钮与「/new 序号」共用同一快照
        self._workspace_views: dict[str, tuple[str, list[dict], float]] = {}
        self._session_views: dict[str, tuple] = {}
        # chat_id → 完整会话列表 / 是否只看当前工作区：过滤只影响展示，不改快照
        self._session_all: dict[str, list[dict]] = {}
        self._session_filters: dict[str, bool] = {}
        # chat_id → (token, created_at)：定时任务卡片的按钮有效期（10 分钟）
        self._cron_views: dict[str, tuple[str, float]] = {}
        # chat_id → (token, created_at)：目录授权卡片按钮同理
        self._grant_views: dict[str, tuple[str, float]] = {}
        self._domain_views: dict[str, tuple[str, float]] = {}
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
            on_missing=self._watch_missing,
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
                   chat_type: str | None = None,
                   message_id: str | None = None) -> None:
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
            self._send_panel(
                chat_id,
                intro=('已就绪，直接说话就行。试试：' + chr(10)
                       + '· 帮我看看这个项目最近改了什么' + chr(10)
                       + '· 每天早上 9 点提醒我喝水' + chr(10)
                       + '· 我们上次聊到哪了'))
        try:
            kwargs = {'reply_to': message_id} if message_id else {}
            self._send_agent_reply(chat_id, session_id, text, **kwargs)
        except Exception as exc:
            self._reply(chat_id, friendly_error(exc) or f'[error] {exc}')

    def _card_patch_gate(self) -> bool:
        """多会话并发时合并卡片更新：实例内最小间隔 0.45s，未到点跳过本拍
        （下一秒的进度 tick 会补上最新状态）。"""
        now = time.monotonic()
        if now - self._last_card_patch < MIN_CARD_PATCH_INTERVAL:
            return False
        self._last_card_patch = now
        return True

    def _step_cards_enabled(self) -> bool:
        raw = (os.getenv('FEISHU_STEP_CARD') or '').strip().lower()
        return raw not in ('0', 'false', 'no', 'off')

    def _send_agent_reply(self, chat_id: str, session_id: str, text: str,
                          reply_to: str | None = None) -> None:
        """每个回合一张可编辑卡片：默认 schema 2.0 过程卡，可退回 Markdown 卡。"""
        self._last_user[chat_id] = {'text': text, 'session_id': session_id,
                                    'at': time.monotonic()}
        perf_reset()
        _t_entry = time.perf_counter()
        step_sender = getattr(self.transport, 'send_step_card', None)
        step_updater = getattr(self.transport, 'update_step_card', None)
        step_mode = bool(step_sender and step_updater and self._step_cards_enabled())
        fallback_sender = getattr(self.transport, 'send_text', None)
        if step_mode:
            sender, updater = step_sender, step_updater
        else:
            updater = getattr(self.transport, 'update_markdown', None)
            sender = getattr(self.transport, 'send_markdown', None)
            if not updater or not sender:
                updater = getattr(self.transport, 'update_text', None)
                sender = getattr(self.transport, 'send_text', None) if updater else None
        reply_kwargs = ({'reply_to': reply_to}
                        if reply_to and sender is not None
                        and _accepts_kwarg(sender, 'reply_to') else {})
        message_id = None
        parts = []
        screenshots: list[dict] = []
        last_update = 0.0
        last_sent = None
        stream_failed = False
        progress: dict = {'status': {}}
        # 流式延迟诊断：只在开启时累加，不影响既有行为。
        trace = {'card_sends': 0, 'card_patches': 0, 'patch_ms': 0.0,
                 'deltas': 0, 'progress': 0, 'sses': 0}

        def _trace_out(error: str = '') -> None:
            if not stream_trace_enabled():
                return
            total = time.perf_counter() - _t_entry
            first = trace.get('first_ms')
            first_text = '-' if first is None else f'{first / 1000:.2f}s'
            patches = trace['card_patches']
            patch_avg = '-' if not patches else f"{trace['patch_ms'] / patches:.0f}ms"
            print('[feishu][stream] '
                  f'total={total:.2f}s first_delta={first_text} '
                  f'deltas={trace["deltas"]} progress={trace["progress"]} '
                  f'cards={trace["card_sends"]} patches={patches} '
                  f'patch_avg={patch_avg} failed={stream_failed}'
                  + (f' error={error}' if error else ''))

        def _note(status: dict | None = None) -> str:
            status = status or progress['status'] or {}
            elapsed = status.get('elapsed_text')                 or format_duration(status.get('elapsed', 0))
            if status.get('phase') == 'tool':
                return f"运行中 · 正在执行 {status.get('tool') or '工具'} · {elapsed}"
            return f'运行中 · {elapsed}'

        def _legacy(answer: str, status: dict | None = None) -> str:
            label = ('正在执行 ' + (status or {}).get('tool') if (status or {}).get('phase') == 'tool'
                     else '正在处理')
            elapsed = (status or {}).get('elapsed_text') or format_duration(
                (status or {}).get('elapsed', 0))
            return f'{label} · 已用时 {elapsed}' + chr(10) + chr(10) + answer

        def _payload(answer_text: str, *, running: bool, note: str = '') -> object:
            state = progress['status'] or {}
            return step_card(
                answer=answer_text,
                status='running' if running else 'completed',
                tool_counts=state.get('tool_counts'),
                tool_errors=state.get('tool_errors'),
                total_ms=state.get('tool_total_ms') or 0,
                tool_batches=state.get('tool_batches'),
                note=note,
                actions='stop' if running else 'menu',
            )

        def update_partial(display_text: str, *, answer_text: str | None = None,
                           running: bool = True, note: str = '') -> None:
            nonlocal message_id, last_update, stream_failed, last_sent
            if stream_failed or updater is None or sender is None:
                return
            answer = display_text if answer_text is None else answer_text
            payload = _payload(answer, running=running, note=note) if step_mode else display_text
            if payload == last_sent:
                return   # 内容与计时都没变化，不重复更新卡片
            now = time.monotonic()
            if now - last_update < 0.6:
                return
            try:
                _t_card = time.perf_counter()
                if message_id is None:
                    # 首建卡片用本次内容（进度路径下 parts 可能为空）
                    if step_mode:
                        message_id = sender(chat_id, payload, **reply_kwargs)
                    else:
                        message_id = sender(chat_id, payload or '生成中…',
                                            **reply_kwargs)
                    perf_record('gw.first_card', time.perf_counter() - _t_entry)
                    trace['card_sends'] += 1
                elif self._card_patch_gate():
                    updater(message_id, payload)
                    last_update = now
                    trace['card_patches'] += 1
                    trace['patch_ms'] += (time.perf_counter() - _t_card) * 1000
                last_sent = payload
            except Exception as exc:
                stream_failed = True
                print(f'[feishu] stream update failed, falling back: {exc}')

        def on_delta(part: str) -> None:
            parts.append(part)
            trace['deltas'] += 1
            if 'first_ms' not in trace:
                trace['first_ms'] = (time.perf_counter() - _t_entry) * 1000
            answer = ''.join(parts)
            if step_mode:
                update_partial(answer, answer_text=answer, running=True, note=_note())
            else:
                update_partial(answer)

        def on_progress(status: dict) -> None:
            progress['status'] = status
            trace['progress'] += 1
            answer = ''.join(parts)
            if step_mode:
                update_partial(answer, answer_text=answer, running=True,
                               note=_note(status))
            else:
                update_partial(_legacy(answer, status))

        def _invoke(target_session: str):
            # 来源聊天随消息带走：模型在回合内建的定时任务结果要能推回这里。
            streamer = getattr(self.api, 'send_stream', None)
            if streamer is not None:
                extra = ({'origin_chat': chat_id}
                         if _accepts_kwarg(streamer, 'origin_chat') else {})
                if _accepts_kwarg(streamer, 'on_screenshots'):
                    extra['on_screenshots'] = screenshots.extend
                return streamer(text, session_id=target_session,
                                on_delta=on_delta, on_progress=on_progress,
                                **extra)
            extra = ({'origin_chat': chat_id}
                     if _accepts_kwarg(self.api.send, 'origin_chat') else {})
            if _accepts_kwarg(self.api.send, 'on_screenshots'):
                extra['on_screenshots'] = screenshots.extend
            return self.api.send(text, session_id=target_session, **extra)

        try:
            try:
                reply = _invoke(session_id)
            except Exception as exc:
                if not _is_missing_session(exc):
                    raise
                # 老绑定指向已删除的会话：新建一个再重试，不把 404 丢给用户。
                session_id = self._rebind_new_session(chat_id)
                parts.clear()
                screenshots.clear()
                reply = _invoke(session_id)
        except Exception as exc:
            print(f'[feishu] agent reply failed: {exc}')
            _trace_out(str(exc)[:80])
            hint = friendly_error(exc)
            failed = ('处理未完成：' + (hint or str(exc)) + chr(10)
                      + ('技术细节：' + str(exc) + chr(10) if hint else '')
                      + '请先核实任务状态，再决定是否重试。' + chr(10) + chr(10)
                      + ''.join(parts))
            if message_id and updater and not stream_failed:
                try:
                    if step_mode:
                        state = progress['status'] or {}
                        updater(message_id, step_card(
                            answer=failed, status='failed',
                            tool_counts=state.get('tool_counts'),
                            tool_errors=state.get('tool_errors'),
                            total_ms=state.get('tool_total_ms') or 0,
                            tool_batches=state.get('tool_batches'),
                            actions='retry'))
                    else:
                        updater(message_id, failed)
                    return
                except Exception as update_exc:
                    print(f'[feishu] failure card update failed: {update_exc}')
            # 首请求就失败（402/401/超时）时还没有卡片：补发一张带「重试」的失败卡，
            # 手机上不用重新打字。
            if step_mode and sender is not None and not stream_failed:
                try:
                    state = progress['status'] or {}
                    sender(chat_id, step_card(
                        answer=failed, status='failed',
                        tool_counts=state.get('tool_counts'),
                        tool_errors=state.get('tool_errors'),
                        total_ms=state.get('tool_total_ms') or 0,
                        tool_batches=state.get('tool_batches'),
                        actions='retry'), **reply_kwargs)
                    return
                except Exception as send_exc:
                    print(f'[feishu] failure card send failed: {send_exc}')
            self._reply(chat_id, failed)
            return

        reply = reply or '本轮已结束，未生成文本回复。'
        _trace_out()
        self._send_turn_screenshots(chat_id, session_id, screenshots,
                                    reply_to=reply_to)
        duration = getattr(self.api, 'last_duration', None)
        if step_mode:
            elapsed = duration or format_duration(time.monotonic() - _t_entry)
            note = f'已完成 · 总用时 {elapsed}'
            clipped = truncated_note(reply)
            if clipped:
                note += chr(10) + clipped
            final_payload = _payload(reply, running=False, note=note)
        else:
            if duration:
                reply = reply + chr(10) + chr(10) + f'已完成 · 总用时 {duration}'
            final_payload = reply
        if message_id and updater and not stream_failed and final_payload != last_sent:
            try:
                _t_final = time.perf_counter()
                updater(message_id, final_payload)
                perf_record('gw.final_update', time.perf_counter() - _t_final)
                return
            except Exception as exc:
                print(f'[feishu] final card update failed, falling back: {exc}')
                stream_failed = True
        if message_id and updater and not stream_failed:
            return   # 卡片已显示最终内容，无需重复 PATCH 或重发
        if step_mode and sender is not None and not stream_failed:
            # 没有流式增量（非流式回退、一键重试）时还没有卡片：补发最终卡，
            # 否则同一个回合会因为“有没有 delta”而在卡片/纯文本之间摇摆。
            try:
                sender(chat_id, final_payload, **reply_kwargs)
                return
            except Exception as exc:
                print(f'[feishu] final card send failed, falling back: {exc}')
        if stream_failed and fallback_sender:
            try:
                fallback_sender(chat_id, reply)
                return
            except Exception:
                pass
        self._reply(chat_id, reply)

    @staticmethod
    def _screenshots_enabled() -> bool:
        raw = (os.getenv(SCREENSHOT_REPLY_ENV) or '').strip().lower()
        return raw not in ('0', 'false', 'no', 'off', 'disabled')

    def _send_turn_screenshots(self, chat_id: str, session_id: str, items,
                               *, reply_to: str | None = None) -> int:
        """把本回合截到的图传回聊天；失败只记日志，不影响文字答复。"""
        if not items or self.transport is None or not self._screenshots_enabled():
            return 0
        sender = getattr(self.transport, 'send_image', None)
        downloader = getattr(self.api, 'download_screenshot', None)
        if sender is None or downloader is None:
            return 0
        valid = []
        seen = set()
        for item in items:
            name = str((item or {}).get('name') or '')
            if not turn_screenshots.valid_name(name) or name in seen:
                continue
            seen.add(name)
            valid.append(name)
        selected = valid[:SCREENSHOT_REPLY_LIMIT]
        sent = 0
        too_large = 0
        for name in selected:
            try:
                data = downloader(session_id, name)
            except Exception as exc:
                print(f'[feishu] screenshot download failed: {exc}')
                continue
            if len(data) > FEISHU_IMAGE_MAX_BYTES:
                too_large += 1
                continue
            kwargs = ({'reply_to': reply_to}
                      if reply_to and _accepts_kwarg(sender, 'reply_to') else {})
            try:
                sender(chat_id, data, **kwargs)
                sent += 1
            except Exception as exc:
                print(f'[feishu] screenshot send failed: {exc}')
        notes = []
        if len(valid) > len(selected):
            notes.append(f'只回传前 {len(selected)} 张')
        if too_large:
            notes.append(f'{too_large} 张超过飞书 10MB 限制未上传')
        if notes:
            self._reply(chat_id, '截图回传：' + '；'.join(notes) + '。')
        return sent

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
        self._session_all[chat_id] = rows
        self._session_filters[chat_id] = False
        self._render_sessions(chat_id, page=0)

    def _render_sessions(self, chat_id, page=0):
        """渲染会话列表：可只看当前工作区；按钮序号始终对应本次展示的行。"""
        rows_all = self._session_all.get(chat_id) or []
        current = self.bindings.get(chat_id)
        filtered = bool(self._session_filters.get(chat_id))
        rows = rows_all
        if filtered:
            workspace = next((row.get('workspace') or ''
                              for row in rows_all if row['id'] == current), '')
            rows = [row for row in rows_all
                    if (row.get('workspace') or '') == workspace]
        if not rows:
            self._reply(chat_id, '当前工作区下没有会话；点「显示全部工作区」查看其它工作区。')
            return
        view = self._session_views.get(chat_id)
        token = view[0] if view else secrets.token_urlsafe(12)
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, int(page or 0)), pages - 1)
        # 展示用的行写回视图：按钮序号必须与用户看到的顺序一致
        self._session_views[chat_id] = (token, rows, time.monotonic())
        self._session_choices[chat_id] = rows
        start = page * PAGE_SIZE
        self._send_session_view(
            chat_id, list_card(rows, token, current, page, filtered=filtered),
            self._session_rows_text(rows[start:start + PAGE_SIZE], start=start)
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
        if action == 'scope':
            self._session_filters[chat_id] = not self._session_filters.get(chat_id, False)
            self._render_sessions(chat_id, page=0)
        elif action == 'page' and index < (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE:
            self._render_sessions(chat_id, page=index)
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
        pending = len(self._chat_pending.get(chat_id) or [])
        try:
            crons = len(self.api.list_crons())
        except Exception:
            crons = 0   # 面板不能因为 cron 接口抖动就打不开
        try:
            path_grants = len(self.api.list_grants())
        except Exception:
            path_grants = 0
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, control_card(session_id=session_id,
                                             workspace=workspace,
                                             model=self._model_summary(),
                                             intro=intro, pending=pending,
                                             crons=crons, grants=path_grants))
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
        elif action in ('crons', 'cron'):
            self._show_crons(chat_id)
        elif action in ('grants', 'grant'):
            self._show_grants(chat_id)
        elif action in ('perms', 'approvals'):
            pending = [req for req in self.api.pending_permissions()
                       if self._request_belongs_to(chat_id, req)]
            if not pending:
                self._reply(chat_id, '(无待审批请求)')
                return
            self._chat_pending[chat_id] = [req['req_id'] for req in pending]
            self._render_approvals(chat_id)
        elif action in ('menu', 'panel'):
            self._send_panel(chat_id, intro='常用操作：')
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
        lines = [f'模型 {self._model_summary()}']
        try:
            lines.append(f'工作区 {self.api.get_workspace(session_id)}')
        except Exception as exc:
            lines.append(f'工作区不可用: {exc}')
        lines.append(f'会话 {session_id[:8]}')
        pending = len(self._chat_pending.get(chat_id) or [])
        # 只报“有几条”，不贴命令正文/参数，避免在聊天里泄露执行细节。
        lines.append(f'待审批 {pending} 条' + ('（回 y / n 或点卡片按钮）' if pending else ''))
        try:
            lines.append(f"定时任务 {len(self.api.list_crons())} 个（点「定时任务」管理）")
        except Exception as exc:
            lines.append(f'定时任务不可用: {exc}')
        try:
            lines.append(f"目录写授权 {len(self.api.list_grants())} 个"
                         '（点「目录授权」管理）')
        except Exception as exc:
            lines.append(f'目录授权不可用: {exc}')
        try:
            shots = screen_capture.stats(paths.cache_path('screenshots'))
            state = '开' if screen_tools_enabled() else f'关（{SCREEN_ENV}=0）'
            lines.append(f"屏幕工具 {state} · 截图 {shots['files']} 张"
                         f"（{tool_cache.human_size(shots['bytes'])}）")
        except Exception as exc:
            lines.append(f'屏幕工具不可用: {exc}')
        clip_state = ('开（读写均需审批）' if clipboard_tools_enabled()
                      else f'关（{CLIPBOARD_ENV}=0）')
        lines.append(f'剪贴板工具 {clip_state}')
        lines.append('直接发消息即可；停止与详情都在控制台卡片上。')
        self._reply(chat_id, chr(10).join(lines))

    def _send_task_view(self, chat_id: str, card: dict, fallback: str,
                        message_id: str | None = None) -> None:
        """任务卡片：优先就地更新（刷新/看日志都复用同一张卡）。"""
        patcher = getattr(self.transport, 'update_step_card', None)
        if message_id and patcher is not None:
            try:
                patcher(message_id, card)
                return
            except Exception as exc:
                print(f'[feishu] task card patch failed: {exc}')
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] task card failed, fallback to text: {exc}')
        self._reply(chat_id, fallback)

    def _show_tasks(self, chat_id: str, *, message_id: str | None = None) -> None:
        try:
            tasks = self.api.list_tasks()
        except Exception as exc:
            self._reply(chat_id, f'[status] {exc}')
            return
        self._send_task_view(chat_id, tasks_card(tasks), tasks_text(tasks),
                             message_id=message_id)

    def _show_logs(self, chat_id: str, task: dict, *, lines: int = 60,
                   message_id: str | None = None) -> None:
        try:
            logs = self.api.task_logs(task['id'], lines)
        except Exception as exc:
            self._reply(chat_id, f'[logs] {exc}')
            return
        self._send_task_view(chat_id, logs_card(task, logs, lines=lines),
                             logs or '(no output)', message_id=message_id)

    RETRY_TTL = 1800.0

    def _retry_last(self, chat_id: str) -> None:
        """重发上一条用户输入；没有可重试内容时明确说明。"""
        item = self._last_user.get(chat_id)
        if not item or time.monotonic() - item['at'] > self.RETRY_TTL:
            self._reply(chat_id, '没有可重试的消息（超过 30 分钟或还没发过）；直接再发一次即可。')
            return
        session_id = item['session_id']
        if not self.bindings.get(chat_id):
            # 绑定被解除了：仍用当时的会话，避免悄悄换会话重跑
            with self._bindings_lock:
                self.bindings[chat_id] = session_id
                self._save_bindings()
        self._send_agent_reply(chat_id, session_id, item['text'])

    def _handle_task_action(self, chat_id: str, value: dict,
                            sender_id: str | None = None,
                            message_id: str | None = None) -> None:
        """任务卡片按钮：刷新列表 / 看日志 / 停止。"""
        action = str(value.get('task_action') or '')
        task_id = str(value.get('task_id') or '')
        if action == 'refresh':
            self._show_tasks(chat_id, message_id=message_id)
            return
        try:
            if action == 'logs':
                lines = value.get('lines')
                task = self.api.get_task(task_id)
                self._show_logs(chat_id, task,
                                lines=lines if type(lines) is int else 60,
                                message_id=message_id)
                return
            if action == 'stop':
                task = self.api.stop_task(task_id)
                self._reply(chat_id, f"任务 {task['id']} → {task['status']}")
                self._show_tasks(chat_id, message_id=message_id)
                return
        except Exception as exc:
            self._reply(chat_id, f'[{action}] {exc}')
            return
        self._reply(chat_id, '未知的任务操作，请重新 /status。')

    def _handle_cron(self, chat_id: str, argument: str) -> None:
        """/cron：查看/创建/删除定时任务；cron 表达式由服务端校验。"""
        usage = chr(10).join([
            '用法:',
            '/cron                     列出定时任务',
            '/cron add "<分 时 日 月 周>" <提示词>',
            '/cron add --writes "<分 时 日 月 周>" <提示词>  允许副作用',
            '/cron delete <job_id>',
            '/cron writes|readonly <job_id>   授权/收回副作用（也可点卡片按钮）',
        ])
        text = argument.strip()

        if not text or text.casefold() in ('list', 'ls'):
            self._show_crons(chat_id)
            return

        action, _, rest = text.partition(' ')
        action = action.casefold()
        rest = rest.strip()
        if action in ('add', 'create'):
            expr, prompt, allow_side_effects = _split_cron_add(rest)
            if not expr or not prompt:
                self._reply(chat_id, usage)
                return
            push = _accepts_kwarg(self.api.create_cron, 'deliver_to')
            try:
                extra = {'deliver_to': chat_id} if push else {}
                job = self.api.create_cron(
                    expr, prompt, allow_side_effects=allow_side_effects,
                    **extra)
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            mode = '可写（副作用仍需审批）' if allow_side_effects else '只读'
            tail = ' · 结果推送回本聊天' if push else ''
            self._reply(chat_id,
                        f"已创建 {job['id']}: '{job['cron']}' → "
                        f"{job['prompt']}（{mode}{tail}）" + chr(10)
                        + '需要改授权或删除，点「定时任务」卡片即可。')
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
        if action in ('writes', 'readonly'):
            if not rest:
                self._reply(chat_id, usage)
                return
            try:
                job = self.api.update_cron(
                    rest, allow_side_effects=action == 'writes')
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            mode = '可写（副作用仍需审批）' if job.get('allow_side_effects') else '只读'
            self._reply(chat_id, f"已更新 {rest}：{mode}")
            return
        self._reply(chat_id, usage)

    def _show_crons(self, chat_id: str, *, page: int = 0,
                    intro: str = '', message_id: str | None = None) -> None:
        """定时任务卡片：按最近触发排序并分页；有原消息时就地更新，不刷屏。

        翻页时如果每次都新发一张卡，聊天记录会被“下一页”塞满，所以按钮回调
        带来的 `message_id` 优先用 PATCH 复用同一张卡。
        """
        try:
            jobs = self.api.list_crons()
        except Exception as exc:
            self._reply(chat_id, f'[cron] {exc}')
            return
        token = secrets.token_hex(4)
        self._cron_views[chat_id] = (token, time.monotonic())
        card = cron_card(jobs, chat_id=chat_id, token=token, intro=intro,
                         page=page)
        patcher = getattr(self.transport, 'update_step_card', None)
        if message_id and patcher is not None:
            try:
                patcher(message_id, card)
                return
            except Exception as exc:
                print(f'[feishu] cron card patch failed, sending new: {exc}')
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] cron card failed, fallback to text: {exc}')
        self._reply(chat_id, (intro + chr(10) if intro else '')
                    + cron_text(jobs, chat_id=chat_id))

    def _cron_view_ok(self, chat_id: str, value: dict) -> bool:
        view = self._cron_views.get(chat_id)
        return bool(view and value.get('cron_view') == view[0]
                    and time.monotonic() - view[1] <= 600)

    def _handle_cron_action(self, chat_id: str, value: dict,
                            sender_id: str | None = None,
                            message_id: str | None = None) -> None:
        """卡片按钮：允许写 / 收回写 / 删除（二次确认）。"""
        action = str(value.get('cron_action') or '')
        job_id = str(value.get('job_id') or '')
        if not self._cron_view_ok(chat_id, value):
            # 只提示，不清掉最新视图：旧卡片被点不该让新卡片一起失效。
            self._reply(chat_id, '这张定时任务卡片已过期，请重新发送 /cron 打开。')
            return
        if action == 'page':
            target = value.get('page')
            if type(target) is not int or target < 0:
                self._reply(chat_id, '无效的页码，请重新打开 /cron。')
                return
            self._show_crons(chat_id, page=target, message_id=message_id)
            return
        try:
            jobs = self.api.list_crons()
        except Exception as exc:
            self._reply(chat_id, f'[cron] {exc}')
            return
        job = next((item for item in jobs if item.get('id') == job_id), None)
        if job is None:
            self._reply(chat_id, f'任务 {job_id} 已不存在，请重新打开 /cron。')
            return
        if action == 'cancel':
            self._reply(chat_id, f'已取消，{job_id} 保持不变。')
            return
        if action == 'delete':
            sender = getattr(self.transport, 'send_session_card', None)
            if sender is not None:
                try:
                    sender(chat_id, cron_delete_card(
                        job, token=self._cron_views[chat_id][0]))
                    return
                except Exception as exc:
                    print(f'[feishu] cron delete card failed: {exc}')
            self._reply(chat_id, f'删除 {job_id} 不可恢复；确认请发送 /cron delete {job_id}')
            return
        if action == 'delete_confirm':
            try:
                self.api.delete_cron(job_id)
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            self._reply(chat_id, f'已删除 {job_id}')
            self._show_crons(chat_id, message_id=message_id)
            return
        if action in ('writes', 'readonly'):
            allow = action == 'writes'
            try:
                self.api.update_cron(job_id, allow_side_effects=allow)
            except Exception as exc:
                self._reply(chat_id, f'[cron] {exc}')
                return
            note = ('已允许写操作。定时回合的每次写操作仍会单独请求审批。'
                    if allow else '已收回写权限，该任务恢复只读。')
            self._reply(chat_id, f'{job_id}：{note}')
            self._show_crons(chat_id, message_id=message_id)
            return
        self._reply(chat_id, '未知的定时任务操作，请重新发送 /cron。')

    def _show_grants(self, chat_id: str, *, page: int = 0,
                     intro: str = '', message_id: str | None = None) -> None:
        """目录写授权卡片：列出现有授权，每条带「撤销」。"""
        try:
            rows = self.api.list_grants()
        except Exception as exc:
            self._reply(chat_id, f'[grant] {exc}')
            return
        token = secrets.token_hex(4)
        self._grant_views[chat_id] = (token, time.monotonic())
        card = grant_card(rows, token=token, intro=intro, page=page)
        patcher = getattr(self.transport, 'update_step_card', None)
        if message_id and patcher is not None:
            try:
                patcher(message_id, card)
                return
            except Exception as exc:
                print(f'[feishu] grant card patch failed, sending new: {exc}')
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] grant card failed, fallback to text: {exc}')
        self._reply(chat_id, (intro + chr(10) if intro else '')
                    + grant_text(rows))

    def _grant_view_ok(self, chat_id: str, value: dict) -> bool:
        view = self._grant_views.get(chat_id)
        return bool(view and value.get('grant_view') == view[0]
                    and time.monotonic() - view[1] <= 600)

    def _handle_grant_action(self, chat_id: str, value: dict,
                             sender_id: str | None = None,
                             message_id: str | None = None) -> None:
        """卡片按钮：翻页 / 撤销某条目录写授权。"""
        action = str(value.get('grant_action') or '')
        if not self._grant_view_ok(chat_id, value):
            self._reply(chat_id, '这张目录授权卡片已过期，请重新发送 /grants 打开。')
            return
        if action == 'page':
            target = value.get('page')
            if type(target) is not int or target < 0:
                self._reply(chat_id, '无效的页码，请重新打开 /grants。')
                return
            self._show_grants(chat_id, page=target, message_id=message_id)
            return
        if action != 'remove':
            self._reply(chat_id, '未知的目录授权操作，请重新发送 /grants。')
            return
        path = str(value.get('grant_path') or '')
        if not path:
            self._reply(chat_id, '无效的授权路径，请重新打开 /grants。')
            return
        try:
            self.api.remove_grant(
                path, session_id=self.bindings.get(chat_id),
                by=f'feishu:{sender_id or chat_id}')
        except Exception as exc:
            self._reply(chat_id, f'[grant] {exc}')
            return
        self._reply(chat_id, f'已撤销目录写授权: {path}')
        self._show_grants(chat_id, message_id=message_id)

    def _handle_grant_command(self, chat_id: str, argument: str,
                              sender_id: str | None = None) -> None:
        """/grants [add|remove]：查看/维护目录级持久写授权。"""
        usage = ('用法:\n'
                 '/grants                    查看（按钮可撤销）\n'
                 '/grants add "<目录>" [until YYYY-MM-DD] [note 备注]\n'
                 '/grants remove "<目录>"\n'
                 '路径含空格时用引号包起来；只允许已存在的绝对目录。')
        parts = argument.split(maxsplit=1)
        action = parts[0].casefold() if parts else ''
        rest = parts[1] if len(parts) > 1 else ''
        if not action:
            self._show_grants(chat_id)
            return
        if action not in ('add', 'remove'):
            self._reply(chat_id, usage)
            return
        try:
            path, options = grants.split_path_argument(rest)
        except ValueError as exc:
            self._reply(chat_id, f'[grant] {exc}')
            return
        if not path:
            self._reply(chat_id, usage)
            return
        by = f'feishu:{sender_id or chat_id}'
        session_id = self.bindings.get(chat_id)
        if action == 'remove':
            if options:
                self._reply(chat_id, usage)
                return
            try:
                self.api.remove_grant(path, session_id=session_id, by=by)
            except Exception as exc:
                self._reply(chat_id, f'[grant] {exc}')
                return
            self._reply(chat_id, f'已撤销目录写授权: {path}')
            self._show_grants(chat_id)
            return
        try:
            expires, note = grants.parse_options(options)
            record = self.api.add_grant(path, expires=expires, note=note,
                                        session_id=session_id, by=by)
        except Exception as exc:
            self._reply(chat_id, f'[grant] {exc}')
            return
        expiry = (f"有效至 {record.get('expires')}" if record.get('expires')
                  else '永久')
        self._reply(chat_id, f"已授权目录写操作: {record.get('path')}（{expiry}）\n"
                             '只放宽工作区外写入的审批；秘密文件与系统目录仍硬拒。')
        self._show_grants(chat_id)

    # ---------- 域名策略 ----------

    def _show_domains(self, chat_id: str, *, page: int = 0,
                      intro: str = '', message_id: str | None = None) -> None:
        """域名策略卡片：列出 allow/deny，每条带「撤销」。"""
        try:
            rows = self.api.list_domains()
        except Exception as exc:
            self._reply(chat_id, f'[domains] {exc}')
            return
        token = secrets.token_hex(4)
        self._domain_views[chat_id] = (token, time.monotonic())
        card = domain_card(rows, token=token, intro=intro, page=page)
        patcher = getattr(self.transport, 'update_step_card', None)
        if message_id and patcher is not None:
            try:
                patcher(message_id, card)
                return
            except Exception as exc:
                print(f'[feishu] domain card patch failed, sending new: {exc}')
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sender(chat_id, card)
                return
            except Exception as exc:
                print(f'[feishu] domain card failed, fallback to text: {exc}')
        self._reply(chat_id, (intro + chr(10) if intro else '')
                    + domain_text(rows))

    def _domain_view_ok(self, chat_id: str, value: dict) -> bool:
        view = self._domain_views.get(chat_id)
        return bool(view and value.get('domain_view') == view[0]
                    and time.monotonic() - view[1] <= 600)

    def _handle_domain_action(self, chat_id: str, value: dict,
                              sender_id: str | None = None,
                              message_id: str | None = None) -> None:
        """卡片按钮：翻页 / 撤销某条域名规则。"""
        action = str(value.get('domain_action') or '')
        if not self._domain_view_ok(chat_id, value):
            self._reply(chat_id, '这张域名卡片已过期，请重新发送 /domains 打开。')
            return
        if action == 'page':
            target = value.get('page')
            if type(target) is not int or target < 0:
                self._reply(chat_id, '无效的页码，请重新打开 /domains。')
                return
            self._show_domains(chat_id, page=target, message_id=message_id)
            return
        if action != 'remove':
            self._reply(chat_id, '未知的域名操作，请重新发送 /domains。')
            return
        host = str(value.get('domain_host') or '')
        if not host:
            self._reply(chat_id, '无效的域名规则，请重新打开 /domains。')
            return
        try:
            self.api.remove_domain(
                host, session_id=self.bindings.get(chat_id),
                by=f'feishu:{sender_id or chat_id}')
        except Exception as exc:
            self._reply(chat_id, f'[domains] {exc}')
            return
        self._reply(chat_id, f'已撤销域名规则: {host}')
        self._show_domains(chat_id, message_id=message_id)

    def _handle_domain_command(self, chat_id: str, argument: str,
                               sender_id: str | None = None) -> None:
        """/domains [allow|deny|remove] <主机> [until YYYY-MM-DD] [note 备注]"""
        usage = ('用法:\n'
                 '/domains                       查看（按钮可撤销）\n'
                 '/domains allow <主机> [until YYYY-MM-DD] [note 备注]\n'
                 '/domains deny  <主机> [until YYYY-MM-DD] [note 备注]\n'
                 '/domains remove <主机>\n'
                 '主机写精确名（api.example.com）或通配子域（*.example.com）；\n'
                 '通配不含根域本身，想同时放行要两条都写。')
        parts = argument.split(maxsplit=2)
        action = parts[0].casefold() if parts else ''
        rest = parts[1] if len(parts) > 1 else ''
        tail = parts[2] if len(parts) > 2 else ''
        if not action:
            self._show_domains(chat_id)
            return
        if action not in ('allow', 'deny', 'remove'):
            self._reply(chat_id, usage)
            return
        try:
            host, options = grants.split_path_argument(
                (rest + ' ' + tail).strip())
        except ValueError as exc:
            self._reply(chat_id, f'[domains] {exc}')
            return
        if not host:
            self._reply(chat_id, usage)
            return
        by = f'feishu:{sender_id or chat_id}'
        session_id = self.bindings.get(chat_id)
        if action == 'remove':
            if options:
                self._reply(chat_id, usage)
                return
            try:
                self.api.remove_domain(host, session_id=session_id, by=by)
            except Exception as exc:
                self._reply(chat_id, f'[domains] {exc}')
                return
            self._reply(chat_id, f'已撤销域名规则: {host}')
            self._show_domains(chat_id)
            return
        try:
            expires, note = grants.parse_options(options)
            record = self.api.add_domain(host, scope=action, expires=expires,
                                         note=note, session_id=session_id,
                                         by=by)
        except Exception as exc:
            self._reply(chat_id, f'[domains] {exc}')
            return
        expiry = (f"有效至 {record.get('expires')}" if record.get('expires')
                  else '永久')
        effect = ('访问会直接拒绝（硬拒，任何档位都绕不过）'
                  if record.get('scope') == 'deny'
                  else '访问不再逐次审批（仍受私网拦截与 URL 校验）')
        self._reply(chat_id, f"已添加域名规则: {record.get('scope')} "
                             f"{record.get('host')}（{expiry}）\n{effect}")
        self._show_domains(chat_id)

    def _send_help(self, chat_id: str) -> None:
        """帮助卡：常用入口按钮 + 折叠命令清单；无 2.0 卡片能力时回退纯文本。"""
        sender = getattr(self.transport, 'send_step_card', None)
        if sender is not None:
            try:
                sender(chat_id, help_card(self._help_text()))
                return
            except Exception as exc:
                print(f'[feishu] help card failed, fallback to text: {exc}')
        self._reply(chat_id, self._help_text())

    def _help_text(self) -> str:
        return '\n'.join([
            '日常不用记命令：直接说“每天早上 9 点提醒我…”或“我们上次聊到哪了”，',
            '常用会话操作也有按钮卡片。以下命令仅供高级用法/排障：',
            '',
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
            '/grants [add|remove]   查看/维护目录级持久写授权',
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
                self._send_help(chat_id)
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
                    try:
                        task = self.api.get_task(argument)
                    except Exception as exc:
                        self._reply(chat_id, f'[status] {exc}')
                        return
                    self._show_logs(chat_id, task, lines=60)
                else:
                    self._show_tasks(chat_id)
            elif command == '/logs':
                tokens = argument.split()
                if not tokens:
                    self._reply(chat_id, '用法: /logs <任务id> [行数]')
                    return
                try:
                    task = self.api.get_task(tokens[0])
                except Exception as exc:
                    self._reply(chat_id, f'[logs] {exc}')
                    return
                lines = int(tokens[1]) if len(tokens) > 1 else 60
                self._show_logs(chat_id, task, lines=lines)
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
                    result = self.api.revoke_permissions(self.bindings.get(chat_id))
                except Exception as exc:
                    self._reply(chat_id, f'[revoke] {exc}')
                    return
                text = f"已撤销 {result['revoked']} 条当前会话预授权"
                if result.get('grants_revoked'):
                    text += f"，以及 {result['grants_revoked']} 条目录写授权"
                text += '。'
                if result.get('grant_error'):
                    text += f"\n目录授权撤销失败: {result['grant_error']}"
                self._reply(chat_id, text)
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
            elif command in ('/grants', '/grant'):
                self._handle_grant_command(chat_id, argument, sender_id)
            elif command in ('/domains', '/domain'):
                self._handle_domain_command(chat_id, argument, sender_id)
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
        pending_by_id = {req['req_id']: req for req in pending}
        changed: set[str] = set()   # 只有变化过的聊天才重渲染，避免每轮轮询都 PATCH
        for req in pending:
            req_id = req['req_id']
            self._pending_requests[req_id] = req
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
                    changed.add(chat_id)
        # 决议完成（或超时）的请求退出通知集合与各聊天 FIFO，避免无界增长
        pending_ids = set(pending_by_id)
        self._notified &= pending_ids
        self._pending_requests = {req_id: req for req_id, req in
                                  self._pending_requests.items()
                                  if req_id in pending_ids}
        for chat_id, fifo in self._chat_pending.items():
            before = len(fifo)
            fifo[:] = [req_id for req_id in fifo if req_id in pending_ids]
            if len(fifo) != before:
                changed.add(chat_id)   # 超时/别处已决议：卡片也要跟着更新
        # 每个聊天只渲染一次合并卡：一次批量操作产生多条请求时不再一条一张卡
        for chat_id in sorted(changed):
            try:
                self._render_approvals(chat_id)
            except Exception as exc:
                print(f'[feishu] approval notification failed for chat {chat_id!r}: {exc}')

    def _push_approval(self, chat_id: str, req: dict, index: int) -> None:
        """兼容入口：旧调用方按“单条请求”推送，现在统一走合并卡片。"""
        self._render_approvals(chat_id)

    def _render_approvals(self, chat_id: str) -> None:
        """渲染该聊天的合并审批卡：首次发送，之后 PATCH 同一张卡。

        一次批量操作可能同时产生十几条审批请求；每条一张卡会把聊天刷满，也看不出
        “还剩几条”。这里始终渲染当前待审批的**全部**请求，并复用同一个 message_id。
        """
        ids = list(self._chat_pending.get(chat_id) or [])
        requests = [self._pending_requests[req_id] for req_id in ids
                    if req_id in self._pending_requests]
        card = approval_card(requests)
        message_id = self._approval_cards.get(chat_id)
        if message_id:
            patcher = getattr(self.transport, 'update_step_card', None)
            if patcher is not None:
                try:
                    patcher(message_id, card)
                    return
                except Exception as exc:
                    print(f'[feishu] approval card patch failed: {exc}')
            else:
                updater = getattr(self.transport, 'update_markdown', None)
                if updater is not None:
                    try:
                        updater(message_id, json.dumps(card, ensure_ascii=False),
                                raw_content=True)
                        return
                    except Exception as exc:
                        print(f'[feishu] approval card update failed: {exc}')
        sender = getattr(self.transport, 'send_session_card', None)
        if sender is not None:
            try:
                sent = sender(chat_id, card)
            except Exception as exc:
                print(f'[feishu] approval card failed, fallback to text: {exc}')
            else:
                if sent:
                    self._approval_cards[chat_id] = sent
                return
        self._reply(chat_id, approval_text(requests))

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
        view_keys = ('session_view', 'workspace_view', 'cron_action', 'task_action',
                     'retry', 'panel')
        if self.allowed_users is not None and open_id not in self.allowed_users:
            return ('⛔ 你没有会话操作权限'
                    if any((value or {}).get(key) for key in view_keys)
                    else '⛔ 你没有审批权限'), False
        if (value or {}).get('workspace_view'):
            view = self._workspace_view(chat_id)
            index = value.get('index')
            if not view or view[0] != value['workspace_view']:
                return '工作区列表已失效，请重新 /new', False
            if type(index) is not int or not 0 <= index < len(view[1]):
                return '无效的工作区按钮，请重新 /new', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在创建会话…', True
        if (value or {}).get('session_view'):
            view = self._session_views.get(chat_id)
            if not view or view[0] != value['session_view'] or time.monotonic() - view[2] > 600:
                return '会话列表已失效，请重新 /sessions', False
            index, action = value.get('index'), value.get('action')
            limit = ((len(view[1]) + PAGE_SIZE - 1) // PAGE_SIZE if action == 'page' else len(view[1]))
            allowed = ('page', 'details', 'switch', 'fork', 'scope')
            if (action not in allowed or type(index) is not int
                    or not 0 <= index < limit):
                return '无效的会话按钮，请重新 /sessions', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理会话操作…', True
        if (value or {}).get('retry'):
            try:
                self._events.put_nowait((chat_id, value, open_id, None,
                                         message_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在重试…', True
        if (value or {}).get('task_action'):
            try:
                self._events.put_nowait((chat_id, value, open_id, None,
                                         message_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理…', True
        if (value or {}).get('grant_action'):
            if not self._grant_view_ok(chat_id, value):
                return '目录授权卡片已过期，请重新打开', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None,
                                         message_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理…', True
        if (value or {}).get('domain_action'):
            if not self._domain_view_ok(chat_id, value):
                return '域名卡片已过期，请重新打开', False
            try:
                self._events.put_nowait((chat_id, value, open_id, None,
                                         message_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理…', True
        if (value or {}).get('cron_action'):
            if not self._cron_view_ok(chat_id, value):
                return '定时任务卡片已过期，请重新打开', False
            try:
                # 带上原消息 id：翻页/刷新时 PATCH 同一张卡，不在聊天里堆新卡。
                self._events.put_nowait((chat_id, value, open_id, None,
                                         message_id))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在处理…', True
        if (value or {}).get('panel'):
            try:
                self._events.put_nowait((chat_id, value, open_id, None, None))
            except queue.Full:
                return '当前消息较多，请稍后重试', False
            return '正在打开…', True
        bulk = (value or {}).get('approval_bulk')
        if bulk in ('allow', 'deny'):
            if not chat_id:
                return '⚠ 无效的审批按钮', False
            ids = list(self._chat_pending.get(chat_id) or [])
            if not ids:
                return '没有待审批请求，可能已处理或已过期', False
            allow_all = bulk == 'allow'
            done = failed = 0
            for req_id in ids:
                try:
                    self.api.resolve_permission(req_id, allow_all)
                    done += 1
                except Exception as exc:
                    print(f'[feishu] bulk approval failed for {req_id}: {exc}')
                    failed += 1
                self._forget_pending(req_id)
            self._render_approvals(chat_id)
            verb = '允许' if allow_all else '拒绝'
            note = f'已{verb} {done} 条'
            if failed:
                note += f'，{failed} 条失败（用 /perms 查看）'
            return note, failed == 0
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
            self.api.resolve_permission(req_id, allow, scope=scope)
        except Exception as exc:
            print(f'[feishu] approval failed: {exc}')
            return '⚠ 操作未能完成，请用 /perms 查看最新审批状态', False
        self._forget_pending(req_id)
        verb = '允许' if allow else '拒绝'
        session_note = '，本会话同类自动通过' if (allow and scope == 'session') else ''
        self._render_approvals(chat_id)
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
            if response.status_code == 404:
                raise FileNotFoundError(session_id)
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith('data:'):
                    continue
                payload = json.loads(line[5:].strip())
                if 'seq' in payload:
                    latest = max(latest, int(payload['seq']))
                    events.append(payload)
        return events, latest

    def _attachment_limit(self) -> int:
        try:
            return max(1, int(os.getenv('FEISHU_MAX_ATTACHMENT_MB', '20'))) * 1024 * 1024
        except ValueError:
            return 20 * 1024 * 1024

    def _unsupported_message(self, chat_id: str, kind: str) -> None:
        self._reply(chat_id, f'暂不支持 {kind} 消息；目前可发送文字、图片、文件、音频、视频。'
                             '音频/视频会保存但暂不自动转写或理解。')

    def _handle_attachment(self, chat_id: str, payload: dict,
                           sender_id: str | None, chat_type: str | None,
                           message_id: str | None) -> None:
        """把飞书图片/文件落到工作区，再让 agent 用现有工具读取。"""
        if self.allowed_users is not None and sender_id not in self.allowed_users:
            return
        kind = payload.get('kind')
        if kind == 'post':
            self._handle_post(chat_id, payload, sender_id, chat_type, message_id)
            return
        key = payload.get('key')
        if not key:
            self._reply(chat_id, '附件缺少 key，无法下载。')
            return
        inbox = self._inbox_dir(chat_id)
        if inbox is None:
            return
        try:
            path = self._save_attachment(
                inbox, kind, key, message_id or '', payload.get('name'),
                resource_kind=payload.get('resource_kind'))
        except Exception as exc:
            print(f'[feishu] attachment failed: {exc}')
            self._reply(chat_id, f'[附件] {exc}')
            return
        if kind == 'image':
            hint = ('[用户发来图片]' + chr(10) + f'path: {path}' + chr(10)
                    + '请先用 view_image 查看图片内容，再回答用户的问题。')
        elif kind == 'audio':
            hint = (f'[用户发来音频] path: {path}' + chr(10)
                    + '音频已保存；当前网关不自动转写，请告知用户需要语音识别时再配置 ASR。')
        elif kind == 'media':
            hint = (f'[用户发来视频/媒体文件] path: {path}' + chr(10)
                    + '文件已保存；当前网关不自动做视频理解，可先说明已收到文件。')
        else:
            hint = (f'[用户发来文件] name: {payload.get("name") or path.name}'
                    + chr(10) + f'path: {path}' + chr(10)
                    + '请按需用 read_document 读取（PDF/Word/Excel/PPT/图片），'
                      '若提示扫描 PDF 没有文本层，再调用 render_document 让视觉模型读取页面；'
                      '或直接用 read 读取文本文件。')
        self.on_message(chat_id, hint, sender_id=sender_id,
                        chat_type=chat_type, message_id=message_id)

    def _inbox_dir(self, chat_id: str) -> Path | None:
        session_id = self.bindings.get(chat_id)
        if not session_id:
            session_id = self.api.create_session()
            with self._bindings_lock:
                self.bindings[chat_id] = session_id
                self._save_bindings()
        try:
            workspace = Path(self.api.get_workspace(session_id))
        except Exception as exc:
            self._reply(chat_id, f'[附件] 无法定位会话工作区：{exc}')
            return None
        inbox = workspace / '.cache' / 'feishu-inbox'
        try:
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._reply(chat_id, f'[附件] 保存失败：{exc}')
            return None
        return inbox

    def _save_attachment(self, inbox: Path, kind: str, key: str,
                         message_id: str, name: str | None = None,
                         resource_kind: str | None = None) -> Path:
        download = getattr(self.transport, 'download_message_resource', None)
        if download is None or not message_id:
            raise RuntimeError('当前传输层不支持下载附件')
        data = download(message_id, key, resource_kind or
                         ('image' if kind == 'image' else 'file'))
        limit = self._attachment_limit()
        if len(data) > limit:
            raise ValueError(f'文件超过 {limit // (1024 * 1024)}MB，已拒绝')
        filename = _attachment_filename(kind, key, name, data)
        path = inbox / f'{uuid.uuid4().hex[:8]}_{filename}'
        path.write_bytes(data)
        return path

    def _handle_post(self, chat_id: str, payload: dict, sender_id: str | None,
                     chat_type: str | None, message_id: str | None) -> None:
        """富文本消息：保留文字，把内嵌图片存到工作区后交给 agent。"""
        text = str(payload.get('text') or '').strip()
        images = [key for key in (payload.get('images') or []) if key][:6]
        inbox = self._inbox_dir(chat_id)
        if inbox is None:
            return
        saved = []
        for index, key in enumerate(images, 1):
            try:
                path = self._save_attachment(inbox, 'image', key, message_id or '')
            except Exception as exc:
                saved.append(f'图片 {index}: 下载失败（{exc}）')
                continue
            saved.append(f'图片 {index}: {path}')
        lines = ['[用户发来富文本消息]']
        if text:
            lines.append(text)
        if saved:
            lines.append(chr(10).join(saved))
            lines.append('请用 view_image 查看上述图片后再回答。')
        hint = chr(10).join(lines)
        self.on_message(chat_id, hint, sender_id=sender_id,
                        chat_type=chat_type, message_id=message_id)

    def _rebind_new_session(self, chat_id: str) -> str:
        session_id = self.api.create_session()
        with self._bindings_lock:
            self.bindings[chat_id] = session_id
            self._save_bindings()
        self._reply(chat_id, '原会话已不存在，已新建会话继续。')
        return session_id

    def _watch_missing(self, session_id: str) -> None:
        """绑定指向的会话已不存在：解除绑定并提示一次（下条消息会新建会话）。"""
        with self._bindings_lock:
            chats = [chat for chat, bound in self.bindings.items()
                     if bound == session_id]
            for chat in chats:
                self.bindings.pop(chat, None)
            if chats:
                self._save_bindings()
        if not chats:
            return
        print(f'[feishu] session {session_id[:8]} missing; unbound {len(chats)} chat(s)')
        for chat in chats:
            self._reply(chat, '原会话已不存在（可能被删除或换了数据目录），'
                              '已解除绑定；直接发消息会新建会话。')

    def _watch_notify(self, session_id: str, kind: str, payload: dict) -> None:
        chat_id = self._chat_for_session(session_id)
        if chat_id is None or self.transport is None:
            raise RuntimeError('no bound chat for session')
        if kind == 'stalled':
            minutes = max(1, round(float(payload.get('elapsed_s') or 0) / 60))
            text = f'⏳ 回合仍在运行（约 {minutes} 分钟），可能在等待审批或卡住。'
        elif kind == 'approval_timeout':
            # 与「用户点了拒绝」区分：这里是审批通道没人应答，操作已按拒绝处理。
            waited = payload.get('timeout_s')
            waited_text = f'{waited:g}s' if isinstance(waited, (int, float)) else '超时'
            tool = approval_view.safe_text(str(payload.get('tool') or '工具'))
            text = f'⚠️ 审批超时（{waited_text} 无人应答）：{tool} 已按拒绝处理'
            reason = approval_view.safe_text(str(payload.get('reason') or '')).strip()
            if reason:
                text += f'\n原因：{reason[:120]}'
        else:
            status = str(payload.get('status') or 'completed')
            if status == 'completed':
                text = '✅ 回合完成'
            elif status == 'cancelled':
                text = '⏹ 回合已取消'
            else:
                text = '⚠️ 回合失败（' + str(payload.get('error_type') or 'error') + '）'
        try:
            self.transport.send_text(chat_id, text)
        except Exception:
            self._local_alert('watch', '飞书回合通知失败', text)
            # 仍向上抛：TurnWatcher 依赖异常判定本轮未送达，下轮重试。
            raise

    def announce_startup(self) -> int:
        """服务启动时给已绑定的聊天发一次常驻控制台入口。"""
        with self._bindings_lock:
            chats = list(self.bindings)
        for chat_id in chats:
            self._send_panel(chat_id, intro='服务已就绪；下面的按钮随时可用。')
        return len(chats)

    def start_watcher(self) -> bool:
        return self.watcher.start()

    def stop_watcher(self, timeout: float = 5.0) -> bool:
        return self.watcher.stop(timeout=timeout)

    def start_permission_poller(self) -> threading.Thread:
        """审批轮询（3s）+ 心跳摘要（60s）+ 定时结果（15s），共用一个后台线程。

        心跳/结果都不需要卡点：网关离线时摘要留在 API 的 outbox 里，重启后
        按当前绑定补发；发送与 ack 之间的重复窗口见 heartbeat / cron_delivery。
        """
        next_heartbeat = 0.0   # 启动后第一轮就检查一次（补发离线期间的摘要）
        next_results = 0.0

        def loop() -> None:
            nonlocal next_heartbeat, next_results
            while not self._poll_stop.wait(PERMISSION_POLL_INTERVAL):
                self.poll_permissions()
                now = time.monotonic()
                if now >= next_results:
                    next_results = now + CRON_RESULT_POLL_INTERVAL
                    try:
                        self.poll_cron_results()
                    except Exception as exc:
                        print(f'[feishu] cron result delivery failed: {exc}')
                if now >= next_heartbeat:
                    next_heartbeat = now + HEARTBEAT_POLL_INTERVAL
                    try:
                        self.poll_heartbeat()
                    except Exception as exc:
                        print(f'[feishu] heartbeat delivery failed: {exc}')

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def poll_heartbeat(self) -> int:
        """把未确认的心跳摘要推给所有已绑定聊天，全部成功后才 ack。

        至少一次：任何一次发送失败都保留 pending，下一轮重试（可能重复推给已经
        成功的聊天）；没有绑定聊天时也不 ack，等用户绑定后补发。
        """
        if self.transport is None:
            return 0
        try:
            pending = self.api.pending_heartbeat()
        except Exception:
            return 0
        if not pending:
            return 0
        with self._bindings_lock:
            chats = list(self.bindings)
        if not chats:
            return 0
        delivered = 0
        for item in pending:
            occurrence = str(item.get('occurrence_id') or '')
            text = str(item.get('text') or '').strip()
            if not occurrence or not text:
                continue
            try:
                for chat_id in chats:
                    self.transport.send_text(chat_id, text)
            except Exception as exc:
                print(f'[feishu] heartbeat send failed: {exc}')
                self._local_alert('heartbeat', '飞书心跳推送失败',
                                  f'摘要未能送达：{type(exc).__name__}: {exc}')
                continue
            try:
                self.api.ack_heartbeat(occurrence)
            except Exception as exc:
                # 已发出但没记上：下一轮会重复推送一次（接受这个窗口）
                print(f'[feishu] heartbeat ack failed: {exc}')
                continue
            delivered += 1
        return delivered

    def poll_cron_results(self) -> int:
        """把定时任务结果推到各自的来源聊天，发送成功才 ack。

        只发当前仍绑定的聊天：不能凭 outbox 里一个任意的 chat_id 就发消息
        （任务创建入口不校验绑定关系）。目标暂时未绑定时保留 pending，等
        绑定恢复后再补发；outbox 自身有上限，不会无限堆积。
        """
        if self.transport is None:
            return 0
        try:
            pending = self.api.pending_cron_results()
        except Exception:
            return 0
        if not pending:
            return 0
        with self._bindings_lock:
            chats = set(self.bindings)
        delivered = 0
        for item in pending:
            item_id = str(item.get('id') or '')
            chat_id = str(item.get('chat_id') or '')
            text = str(item.get('text') or '').strip()
            if not item_id or not text or chat_id not in chats:
                continue
            try:
                self.transport.send_text(chat_id, text)
            except Exception as exc:
                print(f'[feishu] cron result send failed: {exc}')
                self._local_alert('cron', '飞书定时结果推送失败',
                                  f'任务结果未能送达：{type(exc).__name__}: {exc}')
                continue
            try:
                self.api.ack_cron_result(item_id)
            except Exception as exc:
                # 已发出但没记上：下一轮会重复推送一次（接受这个窗口）
                print(f'[feishu] cron result ack failed: {exc}')
                continue
            delivered += 1
        return delivered

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
                 chat_type: str | None = None,
                 message_id: str | None = None) -> None:
        """把飞书事件投递到分发队列（回调线程立即返回）。"""
        norm = (unicodedata.normalize('NFKC', text).strip().lower()
                if isinstance(text, str) else '')
        is_approval = (norm.split()[:1] == ['/approve'] or
                       (re.fullmatch(r'(y|n)(\d{0,2})', norm) is not None
                        and bool(self._chat_pending.get(chat_id))))
        events = self._approval_events if is_approval else self._events
        try:
            events.put_nowait((chat_id, text, sender_id, chat_type, message_id))
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
                chat_id, text, sender_id, chat_type, message_id = item
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
                                if text.get('kind') in ('image', 'file', 'post'):
                                    self._handle_attachment(chat_id, text, sender_id,
                                                            chat_type, message_id)
                                elif text.get('unsupported'):
                                    self._unsupported_message(chat_id, text['unsupported'])
                                elif text.get('panel'):
                                    self._handle_panel_action(chat_id, text['panel'], sender_id)
                                elif text.get('cron_action'):
                                    self._handle_cron_action(chat_id, text, sender_id,
                                                             message_id)
                                elif text.get('grant_action'):
                                    self._handle_grant_action(chat_id, text, sender_id,
                                                              message_id)
                                elif text.get('domain_action'):
                                    self._handle_domain_action(chat_id, text, sender_id,
                                                               message_id)
                                elif text.get('task_action'):
                                    self._handle_task_action(chat_id, text, sender_id,
                                                             message_id)
                                elif text.get('retry'):
                                    self._retry_last(chat_id)
                                elif text.get('workspace_view'):
                                    self._handle_workspace_action(chat_id, text, sender_id)
                                else:
                                    self._handle_session_action(chat_id, text, sender_id)
                            else:
                                self.on_message(chat_id, text, sender_id=sender_id,
                                                chat_type=chat_type,
                                                message_id=message_id)
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

    def _local_alert(self, key: str, title: str, body: str) -> bool:
        """飞书发送失败时弹一条本机通知；同一事件键在窗口内只弹一次。

        远端故障通常是持续的，轮询每轮都失败；不按 key 去重会一分钟一条。
        返回是否真的弹出（测试与排障用）。通知失败只影响通知本身。
        """
        if self.notifier is None:
            return False
        if (os.getenv(LOCAL_ALERT_ENV) or '').strip().casefold() in (
                '0', 'false', 'no', 'off'):
            return False
        now = time.monotonic()
        with self._alert_lock:
            last = self._alerted.get(key)
            if last is not None and now - last < LOCAL_ALERT_WINDOW:
                return False
            self._alerted[key] = now
        try:
            self.notifier(title, body[:LOCAL_ALERT_BODY], level='error')
        except Exception as exc:
            print(f'[feishu] local alert failed: {exc}')
            return False
        return True

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
        try:
            return self.transport.send_text(chat_id, text)
        except Exception as exc:
            self._local_alert('reply', '飞书回复发送失败',
                              f'文字消息未能送达：{type(exc).__name__}: {exc}')
            raise

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
                if message.message_type == 'post':
                    text, images = parse_post_content(message.content)
                    if images:
                        payload = {'kind': 'post', 'text': text, 'images': images}
                        self.dispatch(message.chat_id, payload,
                                      sender_id=getattr(
                                          getattr(getattr(data.event, 'sender', None),
                                                  'sender_id', None), 'open_id', None),
                                      chat_type=getattr(message, 'chat_type', None),
                                      message_id=getattr(message, 'message_id', None))
                    elif text:
                        self.dispatch(message.chat_id, text,
                                      sender_id=getattr(
                                          getattr(getattr(data.event, 'sender', None),
                                                  'sender_id', None), 'open_id', None),
                                      chat_type=getattr(message, 'chat_type', None),
                                      message_id=getattr(message, 'message_id', None))
                    else:
                        self.dispatch(message.chat_id, {'unsupported': 'post'},
                                      chat_type=None,
                                      message_id=getattr(message, 'message_id', None))
                    return
                if message.message_type not in ('text', 'image', 'file', 'audio', 'media'):
                    self.dispatch(message.chat_id,
                                  {'unsupported': message.message_type},
                                  sender_id=None, chat_type=None,
                                  message_id=getattr(message, 'message_id', None))
                    return
                content = json.loads(message.content or '{}')
                if message.message_type in ('image', 'file', 'audio', 'media'):
                    is_image = message.message_type == 'image'
                    payload = {
                        'kind': message.message_type,
                        'resource_kind': 'image' if is_image else 'file',
                        'key': content.get('image_key') if is_image
                        else content.get('file_key'),
                        'name': content.get('file_name') or '',
                    }
                    self.dispatch(message.chat_id, payload,
                                  sender_id=getattr(
                                      getattr(getattr(data.event, 'sender', None),
                                              'sender_id', None), 'open_id', None),
                                  chat_type=getattr(message, 'chat_type', None),
                                  message_id=getattr(message, 'message_id', None))
                    return
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
                              sender_id=sender_id, chat_type=chat_type,
                              message_id=getattr(message, 'message_id', None))
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
        try:
            self.announce_startup()
        except Exception as exc:
            print(f'[feishu] startup panel failed: {exc}')
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

    import console

    console.harden_stdio()

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
    options = dict(api_base=base_url, token=token, app_id=app_id,
                   app_secret=app_secret, allowed_users=allowed_users)
    if os.getenv('FEISHU_ALLOW_MULTI', '').strip().lower() in ('1', 'true', 'yes'):
        print('[feishu] FEISHU_ALLOW_MULTI=1：已跳过单实例检查，'
              '并行实例可能重复回复/重复推送。')
        return _run_gateway(**options)
    try:
        with exclusive_file_lock(INSTANCE_LOCK_PATH, timeout=0):
            return _run_gateway(**options)
    except FileLockTimeout:
        print(f'[feishu] 已经有一个网关实例在运行（{INSTANCE_LOCK_PATH}）。')
        print('  两个网关会把同一条消息回两次、把同一批推送发两遍。请先关掉旧进程。')
        print('  确实要并行（例如不同应用凭证）时设置 FEISHU_ALLOW_MULTI=1。')
        return 2


def _run_gateway(*, api_base: str, token: str, app_id: str, app_secret: str,
                 allowed_users) -> int:
    bypass_proxy_for_feishu()
    api = GatewayClient(api_base, token)
    transport = LarkTransport(app_id, app_secret)
    gateway = FeishuGateway(api, transport=transport,
                            allowed_users=allowed_users,
                            notifier=notify.notify)
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
