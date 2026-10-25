"""飞书端到端验收辅助：本地预检 + 卡片结构校验 + 可选真机发卡。

用法：
  python scripts/feishu_acceptance.py                # 本地预检（不联网、不发消息）
  python scripts/feishu_acceptance.py --api          # 额外检查本地 API 与 token
  python scripts/feishu_acceptance.py --send-cards oc_xxx
                                                     # 把每类卡片真机发一遍

为什么需要它：真机验收只能由人点手机完成，但“卡片 JSON 是否合法、按钮 value 是否
齐全、卡片是否超过飞书 30KB 上限、API 是否可达”这些都可以在本地先排除掉，避免
把结构问题当成“飞书不渲染”。脚本只读配置与本地状态，`--send-cards` 才发消息，
且一次只发给指定聊天。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

MAX_CARD_BYTES = 30 * 1024   # 飞书卡片 content 上限
KNOWN_TAGS = {
    'action', 'button', 'column', 'column_set', 'collapsible_panel', 'div',
    'hr', 'img', 'lark_md', 'markdown', 'note', 'plain_text', 'standard_icon',
}


def _walk(node):
    """递归找出所有带 tag 的元素（schema 1.0 与 2.0 通用）。"""
    if isinstance(node, dict):
        if isinstance(node.get('tag'), str):
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _elements_of(card: dict) -> list:
    body = card.get('body')
    if isinstance(body, dict) and isinstance(body.get('elements'), list):
        return body['elements']
    elements = card.get('elements')
    return elements if isinstance(elements, list) else []


def validate_card(name: str, card: dict, *, max_bytes: int = MAX_CARD_BYTES,
                  require_buttons: bool = True) -> list[str]:
    """返回问题列表；空列表表示结构上没发现问题（不代表飞书一定渲染正确）。"""
    problems: list[str] = []
    try:
        blob = json.dumps(card, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return [f'{name}: 无法序列化成 JSON：{exc}']
    size = len(blob.encode('utf-8'))
    if size > max_bytes:
        problems.append(f'{name}: {size} 字节，超过飞书 {max_bytes} 字节上限')
    if not _elements_of(card):
        problems.append(f'{name}: 没有 elements（schema 2.0 里是 body.elements）')
    buttons = 0
    for element in _walk(_elements_of(card)):
        tag = element['tag']
        if tag not in KNOWN_TAGS:
            problems.append(f'{name}: 未知元素 tag {tag!r}')
        if tag == 'button':
            buttons += 1
            label = (element.get('text') or {}).get('content')
            value = element.get('value')
            if not isinstance(label, str) or not label.strip():
                problems.append(f'{name}: 按钮没有文本')
            elif len(label) > 60:
                problems.append(f'{name}: 按钮文本过长（{len(label)} 字）')
            if not isinstance(value, dict) or not value:
                # schema 2.0 的按钮把回调参数放在 behaviors[].value 里
                value = next((item.get('value') for item in
                              (element.get('behaviors') or [])
                              if isinstance(item.get('value'), dict)
                              and item.get('value')), None)
            if not isinstance(value, dict) or not value:
                problems.append(f'{name}: 按钮 {label!r} 没有 value')
        if tag == 'div':
            content = (element.get('text') or {}).get('content')
            if not isinstance(content, str) or not content.strip():
                problems.append(f'{name}: div 内容为空')
        if tag in ('markdown', 'lark_md'):
            content = element.get('content')
            if content is None:
                content = (element.get('text') or {}).get('content')
            if not isinstance(content, str) or not content.strip():
                problems.append(f'{name}: {tag} 内容为空')
    if require_buttons and not buttons:
        problems.append(f'{name}: 没有任何按钮（手机端只能打字）')
    return problems


def build_cards(*, jobs: list[dict] | None = None,
                session_id: str = 'sess1234', chat_id: str = 'oc_demo',
                token: str = 'tok123') -> dict[str, dict]:
    """构造全部卡片样本；覆盖完成/失败/进行中与授权/删除确认。"""
    import feishu_approval_card
    import feishu_cron_card
    import feishu_panel
    import feishu_sessions
    import feishu_step_card
    import feishu_task_card

    # 样本故意超过一页（PAGE_SIZE=3），用来验收排序与翻页按钮。
    jobs = jobs if jobs is not None else [
        {'id': 'heartbeat', 'cron': '0 9 * * *', 'prompt': '每日只读摘要',
         'recurring': True, 'allow_side_effects': False, 'deliver_to': chat_id,
         'last_fired': '2026-10-25 09:00'},
        {'id': 'cron_abc123', 'cron': '30 18 * * 5', 'prompt': '整理本周变更并不写入文件',
         'recurring': True, 'allow_side_effects': True, 'deliver_to': chat_id,
         'last_fired': '2026-10-24 18:30'},
        {'id': 'cron_drink', 'cron': '0 15 * * *', 'prompt': '提醒我喝水',
         'recurring': True, 'allow_side_effects': False, 'deliver_to': chat_id,
         'last_fired': '2026-10-23 15:00'},
        {'id': 'cron_once', 'cron': '0 20 30 10 *', 'prompt': '今晚一次性提醒：交房租',
         'recurring': False, 'allow_side_effects': False, 'deliver_to': chat_id,
         'last_fired': ''},
    ]
    rows = [{'id': session_id, 'title': '演示会话：飞书验收',
             'workspace': 'D:/harness-agent', 'updated': time.time() - 120,
             'preview': '演示预览'},
            {'id': 'sess5678', 'title': '另一个会话', 'workspace': 'D:/tmp/ws',
             'updated': time.time() - 7200, 'preview': '尚无任务'}]
    readable = [{'name': 'harness-agent', 'path': 'D:/harness-agent',
                 'current': True, 'sessions': 3,
                 'last_active': time.time() - 300},
                {'name': 'tmp-ws', 'path': 'D:/tmp/ws', 'saved': True,
                 'exists': False}]
    return {
        '控制台': feishu_panel.control_card(
            session_id=session_id, workspace='D:/harness-agent',
            model='demo-model', intro='服务已就绪；下面的按钮随时可用。',
            pending=2, crons=len(jobs)),
        '回复卡·完成': feishu_step_card.step_card(
            answer='**验收用回答**\n\n- 第一条\n- 第二条：`code`',
            status='completed', total_ms=12_400,
            tool_batches=[{'tools': ['bash', 'bash', 'read']}]),
        '回复卡·失败': feishu_step_card.step_card(
            answer='命令执行失败：permission denied', status='failed',
            total_ms=3_100, note='失败原因已记录到会话事件'),
        '回复卡·进行中': feishu_step_card.step_card(
            status='running', total_ms=2_000,
            tool_batches=[{'tools': ['bash']}],
            note='运行中 · 正在执行 bash · 00:02', actions='stop'),
        '定时任务': feishu_cron_card.cron_card(jobs, chat_id=chat_id, token=token),
        '定时任务·删除确认': feishu_cron_card.cron_delete_card(jobs[0], token=token),
        '会话列表': feishu_sessions.list_card(rows, token, current_id=session_id),
        '工作区选择': feishu_sessions.workspace_card(readable, token),
        '审批·合并卡': feishu_approval_card.approval_card([
            {'req_id': 'req_demo_1', 'tool': 'bash', 'reason': '需要执行命令',
             'args': {'command': 'git status --short'}},
            {'req_id': 'req_demo_2', 'tool': 'write', 'reason': '需要写入文件',
             'args': {'path': 'src/app.py', 'content': 'print(1)'}},
            {'req_id': 'req_demo_3', 'tool': 'bash', 'reason': '需要执行命令',
             'args': {'command': 'npm test'}},
        ]),
        '后台任务': feishu_task_card.tasks_card([
            {'id': 'bg_1', 'status': 'running', 'command': 'npm test',
             'duration_ms': 12_000},
            {'id': 'bg_2', 'status': 'succeeded', 'exit_code': 0,
             'command': 'git status', 'duration_ms': 800},
        ]),
        '任务日志': feishu_task_card.logs_card(
            {'id': 'bg_2', 'status': 'succeeded', 'exit_code': 0},
            'On branch main\nYour branch is up to date.\n\nnothing to commit'),
    }


def check_env(environ=None) -> list[str]:
    """配置缺失项；只报缺什么，不打印密钥。"""
    env = os.environ if environ is None else environ
    problems = []
    for key in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'HARNESS_API_TOKEN'):
        if not str(env.get(key, '')).strip():
            problems.append(f'缺少 {key}')
    if not str(env.get('FEISHU_ALLOWED_USERS', '')).strip():
        allow_any = str(env.get('FEISHU_ALLOW_ANY_USER', '')).strip().lower()
        if allow_any not in ('1', 'true', 'yes'):
            problems.append('缺少 FEISHU_ALLOWED_USERS（或显式 FEISHU_ALLOW_ANY_USER=1）')
    return problems


def check_api(base_url: str, token: str, *, timeout: float = 5.0) -> list[str]:
    """本地 API 是否可达、token 是否有效；不触发任何回合。"""
    import httpx

    problems = []
    # 与网关一致：回环地址不走系统代理，否则 Windows 代理会给出 502/404 假象。
    with httpx.Client(timeout=timeout, trust_env=False,
                      headers={'Authorization': f'Bearer {token}'}) as client:
        for path, label in (('/v1/sessions', '会话列表'),
                            ('/v1/crons', '定时任务'),
                            ('/v1/heartbeat', '心跳 outbox')):
            try:
                response = client.get(f'{base_url.rstrip("/")}{path}')
            except Exception as exc:
                problems.append(f'{label} 不可达：{exc}')
                continue
            if response.status_code == 401:
                problems.append(f'{label}：HARNESS_API_TOKEN 不匹配')
            elif response.status_code >= 400:
                problems.append(f'{label}：HTTP {response.status_code}')
    return problems


def check_instance_lock(lock_path: Path) -> str:
    """网关是否在运行：拿得到锁=没运行；拿不到=正在运行。"""
    from file_lock import FileLockTimeout, exclusive_file_lock

    try:
        with exclusive_file_lock(lock_path, timeout=0):
            return '未运行（锁空闲）'
    except FileLockTimeout:
        return '正在运行（单实例锁已被持有）'


def bound_chats() -> list[str]:
    """已绑定会话的聊天（读网关的 bindings 文件，只读）。"""
    import feishu_gateway

    path = Path(feishu_gateway.BINDINGS_PATH)
    if not path.exists():
        return []
    try:
        return sorted(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return []


def send_cards(chat_id: str, cards: dict[str, dict]) -> list[str]:
    """把每类卡片真机发一遍；返回失败项（飞书返回的错误码会原样带上）。"""
    app_id = os.getenv('FEISHU_APP_ID', '')
    app_secret = os.getenv('FEISHU_APP_SECRET', '')
    from feishu_gateway import LarkTransport, bypass_proxy_for_feishu

    bypass_proxy_for_feishu()   # 系统代理没开时也要能连通境内飞书域名
    transport = LarkTransport(app_id, app_secret)
    failures = []
    for name, card in cards.items():
        try:
            message_id = transport.send_session_card(chat_id, card)
            print(f'  [sent] {name}: {message_id}')
        except Exception as exc:
            failures.append(f'{name}: {exc}')
            print(f'  [fail] {name}: {exc}')
    return failures


CHECKLIST = [
    ('私聊发一句“你好”', '先看到过程卡（运行中→完成），且回复引用你的原消息'),
    ('点完成卡的「⚙️ 控制台」', '打开控制台卡片，会话/工作区/模型正确'),
    ('点「定时任务」', '列出任务；只读任务显示「允许写操作」，可写任务显示「收回写权限」'),
    ('点「允许写操作」', '提示可写 + 列表刷新；再点「收回写权限」可恢复只读'),
    ('点「删除」→「取消」', '只出现确认卡，任务仍在'),
    ('发一张图片', '回复里提到 view_image，答案能描述图片内容'),
    ('发一个 PDF / DOCX', '回复用 read_document 读出内容；扫描件应提示改用 render_document'),
    ('发一条带标题和图片的富文本', '文字与图片都被处理，不出现“暂不支持 post”'),
    ('发一段语音', '明确回复“已保存、不自动转写”，不假装听懂'),
    ('说“每天早上 9 点提醒我喝水”', '创建只读任务，回复说明结果会推回本聊天'),
    ('等一次 cron 到点（或改成 1 分钟后）', '完成任务的结果推回本聊天，且只推一次'),
    ('触发一个需要审批的工具', '收到审批卡片；点「允许」执行、点「拒绝」不执行'),
    ('审批卡片放置到超时', '收到“审批超时、已按拒绝处理”的通知'),
    ('在 TUI 里跑一个长回合，然后手机发消息', '手机收到忙碌/排队反馈，不会悄悄丢消息'),
    ('手机发“停一下”或点「停止当前回合」', '回合取消，TUI 与手机状态一致'),
    ('再启动一个网关进程', '第二个进程打印“已经有一个网关实例在运行”并退出'),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='飞书端到端验收辅助')
    parser.add_argument('--api', action='store_true',
                        help='检查本地 API 与 token（只读 GET）')
    parser.add_argument('--send-cards', metavar='CHAT_ID',
                        help="把每类卡片发到指定 chat_id（真实发消息）；"
                             "'bound' = 已绑定的聊天")
    parser.add_argument('--only', default='',
                        help='只发这些卡片（逗号分隔，如 “回复卡·进行中”）')
    parser.add_argument('--base-url', default=None,
                        help='覆盖 HARNESS_API_URL（例如临时实例 http://127.0.0.1:8100）')
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，打印 ⚙️ 之类的字符会直接抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass

    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')

    problems: list[str] = []
    env_problems = check_env()
    problems.extend(env_problems)
    for item in env_problems:
        print(f'  [missing] {item}')

    if not env_problems:
        print('  [ok] 必要配置齐全')

    cards = build_cards()
    print(f'\n卡片结构校验（{len(cards)} 类）:')
    for name, card in cards.items():
        # 回复卡本身不需要按钮（完成后由网关补「控制台」入口），其余卡片必须可点。
        card_problems = validate_card(
            name, card, require_buttons=not name.startswith('回复卡'))
        if card_problems:
            problems.extend(card_problems)
            for item in card_problems:
                print(f'  [fail] {item}')
        else:
            size = len(json.dumps(card, ensure_ascii=False).encode('utf-8'))
            print(f'  [ok] {name}（{size} 字节）')

    if args.api:
        base_url = (args.base_url or os.getenv('HARNESS_API_URL')
                    or 'http://127.0.0.1:8000')
        token = os.getenv('HARNESS_API_TOKEN', '')
        print(f'\n本地 API 检查（{base_url}）:')
        if not token:
            problems.append('缺少 HARNESS_API_TOKEN')
            print('  [missing] HARNESS_API_TOKEN')
        else:
            api_problems = check_api(base_url, token)
            problems.extend(api_problems)
            for item in api_problems:
                print(f'  [fail] {item}')
            if not api_problems:
                print('  [ok] 会话/定时任务/心跳接口都可达')

    import paths
    lock_state = check_instance_lock(Path(paths.cache_path('locks/feishu-gateway.lock')))
    print(f'\n网关进程: {lock_state}')

    if args.send_cards:
        targets = bound_chats() if args.send_cards == 'bound' else [args.send_cards]
        if not targets:
            problems.append('没有已绑定的聊天：先在飞书里给机器人发一条消息')
            print('  [missing] 没有已绑定的聊天')
        selected = [name.strip() for name in args.only.split(',') if name.strip()]
        if selected:
            missing = [name for name in selected if name not in cards]
            for name in missing:
                problems.append(f'没有这张卡片：{name}')
            cards = {name: cards[name] for name in selected if name in cards}
        for chat_id in targets:
            print(f'\n真机发卡（chat_id={chat_id[:12]}…）:')
            problems.extend(send_cards(chat_id, cards))

    print('\n手机端逐项验收（做完一条勾一条）:')
    for index, (action, expected) in enumerate(CHECKLIST, 1):
        print(f'  {index:2d}. {action}\n      期望：{expected}')

    if problems:
        print(f'\n结论：发现 {len(problems)} 个本地问题，先修再上手机。')
    else:
        print('\n结论：本地预检全部通过，可以按上面清单做真机验收。')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
