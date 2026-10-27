"""只读的主动建议（M2）——默认关闭。

定位
----
现在唯一的主动行为是「每日只读心跳」（``heartbeat.py``），它明确写着**不跑模型**，
只做确定性巡检。所以「agent 自己想到该做什么」这件事并不存在。

Codex 会主动提议今天从哪开始；dots 有 proactive research。这里做最小版本。

三条硬约束（照 dots 的「主动性只读」）
-------------------------------------
1. **工具面裁剪成只读白名单**，不是靠提示词说「请不要写」。提示词会被说服，
   白名单不会。
2. **建议不是行动**：产出进 outbox，用户点了才进正常回合（带正常权限）。
3. **默认关闭**：主动性最容易做成品味灾难（每天推三条没用的建议）。先有真实
   使用数据再开。

防骚扰
------
连续忽略 3 次自动降频到每周一次——这是 ``should_run`` 的职责，不该让用户手动关。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import settings

#: 只读白名单：主动回合能用的**全部**工具。
#: 刻意不含 write/edit/apply_patch/delete_paths/web_fetch 与一切写操作。
#: bash 整个排除：它既能 ls 也能 curl，裁不出安全的子集。
READ_ONLY_TOOLS = frozenset({
    'read', 'grep', 'glob', 'ls', 'recall', 'todo_read',
    'list_tasks', 'get_task', 'list_crons',
    'find_sessions', 'read_document', 'view_image',
})

#: 状态文件名（放在 .cache 下，与会话目录分开）。
STATE_NAME = 'proactive-state.json'

#: 连续忽略多少次后降频。
IGNORE_LIMIT = 3

#: 降频后的间隔（秒）：一周。
SLOW_INTERVAL_S = 7 * 86400

#: 正常间隔（秒）：一天。
NORMAL_INTERVAL_S = 86400


def enabled() -> bool:
    """默认关。只有显式设 ``HARNESS_PROACTIVE=1`` 才启用。"""
    return settings.flag('HARNESS_PROACTIVE', False)


def max_suggestions() -> int:
    raw = os.getenv('HARNESS_PROACTIVE_MAX', '').strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(value, 5))


def _state_path(cache_dir) -> Path:
    return Path(cache_dir) / STATE_NAME


def _read_state(cache_dir) -> dict:
    path = _state_path(cache_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(cache_dir, data: dict) -> None:
    path = _state_path(cache_dir)
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def should_run(cache_dir, now: float | None = None) -> tuple[bool, str]:
    """今天该不该生成建议。返回 (是否, 原因)。

    忽略次数决定节奏：连续忽略满 ``IGNORE_LIMIT`` 次降到每周一次。这是自动降频，
    不是惩罚——用户没兴趣就不该继续打扰。
    """
    if not enabled():
        return False, 'disabled'
    stamp = time.time() if now is None else now
    state = _read_state(cache_dir)
    last = state.get('last_run') or 0
    if not isinstance(last, (int, float)):
        last = 0
    ignored = state.get('ignored_streak') or 0
    if not isinstance(ignored, int):
        ignored = 0
    interval = SLOW_INTERVAL_S if ignored >= IGNORE_LIMIT else NORMAL_INTERVAL_S
    if last and stamp - last < interval:
        remaining = int(interval - (stamp - last))
        return False, f'next run in {remaining}s'
    return True, ''


def record_run(cache_dir, count: int, now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    state = _read_state(cache_dir)
    state['last_run'] = stamp
    state['last_count'] = max(0, int(count))
    _write_state(cache_dir, state)


def record_ignored(cache_dir) -> int:
    """记一次「用户没采纳」，返回当前连续忽略次数。"""
    state = _read_state(cache_dir)
    streak = state.get('ignored_streak') or 0
    streak = int(streak) + 1 if isinstance(streak, int) else 1
    state['ignored_streak'] = streak
    _write_state(cache_dir, state)
    return streak


def record_accepted(cache_dir) -> None:
    """用户采纳了一条：忽略计数清零（说明这功能对他有用）。"""
    state = _read_state(cache_dir)
    state['ignored_streak'] = 0
    state['accepted_total'] = int(state.get('accepted_total') or 0) + 1
    _write_state(cache_dir, state)


def scan_sources(*, workdir, sessions_dir) -> dict:
    """收集建议的**原始素材**。纯本地、零模型调用、只读。

    这些既喂给模型，也是「你能看到什么」的可审计清单。
    """
    material: dict = {'workspace': str(workdir), 'todos': 0,
                      'recent_sessions': []}
    try:
        import tool_todos
        todos = tool_todos.read_todos(scope=None, session_store=None)
        material['todos'] = len(todos) if isinstance(todos, list) else 0
    except Exception:
        pass
    try:
        import session_continue
        picked = session_continue.recent_session(sessions_dir)
        if picked:
            material['recent_sessions'].append(
                {'title': picked.get('title'),
                 'updated': picked.get('updated')})
    except Exception:
        pass
    return material


def prompt(material: dict, *, limit: int = 3) -> str:
    """生成给模型的只读建议提示。

    即使工具面已经裁了，提示里也再说一次：少一次无效尝试总归更好。
    """
    recent = material.get('recent_sessions') or []
    recent_text = '、'.join(
        str(item.get('title') or '') for item in recent) or '（无）'
    return (
        "You are reviewing the user's local state to suggest what to do next.\n"
        'You have READ-ONLY tools. Do not attempt to change anything.\n\n'
        f"Workspace: {material.get('workspace')}\n"
        f"Open todos: {material.get('todos')}\n"
        f"Recent sessions: {recent_text}\n\n"
        f'Suggest at most {limit} concrete next actions. For each: one line, '
        'actionable, specific to what you can see. Skip anything you cannot '
        'justify from the state above. If nothing is worth suggesting, output '
        'exactly: NONE'
    )


def parse_suggestions(text: str, *, limit: int = 3) -> list[str]:
    """把模型输出解析成建议列表。'NONE' 或空 → 空列表。"""
    body = str(text or '').strip()
    if not body or body.upper().startswith('NONE'):
        return []
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去掉列表符号与序号：模型经常自作主张加 '- ' 或 '1. '
        stripped = False
        for prefix in ('- ', '* ', '• '):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                stripped = True
                break
        if not stripped:
            head, sep, rest = line.partition('. ')
            if sep and head.isdigit():
                line = rest.strip()
        if line:
            out.append(line)
        if len(out) >= limit:
            break
    return out
