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
from dataclasses import dataclass
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


#: 三档主动性，默认 ``off``。
#:
#: 借鉴 super-agent 的 accept mode：**默认不是"不评估"，而是"评估了但只记录"**。
#: 我们的 ``observe`` 就是那一档——按节奏生成一次建议，但只写进观察台账，
#: 不打扰人；``auto`` 才把建议摆到眼前。看几天台账再决定要不要升到 auto。
MODE_ENV = 'HARNESS_PROACTIVE_MODE'
MODE_OFF = 'off'
MODE_OBSERVE = 'observe'
MODE_AUTO = 'auto'
MODES = (MODE_OFF, MODE_OBSERVE, MODE_AUTO)

#: 观察台账最多保留多少条（只记录也不能无限长）。
OBSERVATION_LIMIT = 50
OBSERVATION_NAME = 'proactive-observations.jsonl'


def mode() -> str:
    """当前主动性档位。

    ``HARNESS_PROACTIVE_MODE`` 优先；没设时，老的 ``HARNESS_PROACTIVE=1`` 仍然
    表示 ``auto``——已经有部署只设过那个变量，加模式时不能把它的含义悄悄改掉。
    写了不认识的值一律退回 ``off``（最安全的那一档），不猜。
    """
    raw = (os.getenv(MODE_ENV) or '').strip().casefold()
    if raw in MODES:
        return raw
    if raw:
        return MODE_OFF
    return MODE_AUTO if settings.flag('HARNESS_PROACTIVE', False) else MODE_OFF


def enabled() -> bool:
    """是否启用自动建议（observe 与 auto 都算启用，off 不算）。"""
    return mode() != MODE_OFF


def observation_path(cache_dir) -> Path:
    return Path(cache_dir) / OBSERVATION_NAME


def record_observation(cache_dir, suggestions, *, workspace: str = '',
                       limit: int = 0) -> dict:
    """observe 档：把"它本来会建议什么"写进台账。返回写入的那一行。

    台账的意义就是**事后能看见**：不然 observe 和 off 对用户毫无区别。
    超过 ``OBSERVATION_LIMIT`` 时丢最老的。
    """
    path = observation_path(cache_dir)
    row = {
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'workspace': str(workspace or ''),
        'limit': int(limit or 0),
        'suggestions': [str(item) for item in (suggestions or [])],
    }
    rows: list[str] = []
    if path.is_file():
        try:
            rows = [line for line in path.read_text(encoding='utf-8').splitlines()
                    if line.strip()]
        except OSError:
            rows = []
    rows.append(json.dumps(row, ensure_ascii=False))
    rows = rows[-OBSERVATION_LIMIT:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(chr(10).join(rows) + chr(10), encoding='utf-8')
    except OSError:
        return row      # 台账写不进去不该影响别的事
    return row


def recent_observations(cache_dir, limit: int = 5) -> list[dict]:
    """读回最近几次观察（``/suggest --last`` 用）。损坏的行直接跳过。"""
    path = observation_path(cache_dir)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    for line in lines[-max(1, limit):]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


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
        # 首次运行时 .cache 可能还不存在；不建目录的话这里会静默失败，
        # 节奏状态永远写不进去（自动档就会每次都触发）。
        path.parent.mkdir(parents=True, exist_ok=True)
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


@dataclass(frozen=True)
class SuggestionRun:
    """一次建议回合的结果。``error`` 非空表示入口那边的回合没跑成。"""

    material: dict
    suggestions: list[str]
    error: str = ''


def read_only_schemas() -> list[dict]:
    """喂给模型的工具面：**就是**白名单本身，不是它的超集。

    提示词挡不住工具，白名单才能。这里按 ``READ_ONLY_TOOLS`` 过滤 ``tools.TOOLS``，
    而不是反过来往白名单里补名字——多一个写工具就等于把"只读"变成口号。
    """
    import tools

    wanted = READ_ONLY_TOOLS
    return [schema for schema in tools.TOOLS
            if str(schema.get('function', {}).get('name') or '') in wanted]


def run_turn(*, workdir, sessions_dir, cache_dir, ask, limit: int | None = None
             ) -> SuggestionRun:
    """跑一次建议回合。``ask(text) -> str`` 由入口注入（TUI / REPL 各自执行回合）。

    建议不是行动：这里只收集文字，不调用任何写工具，也不改任何文件。真正动手要
    用户在下一条消息里明说（"做第 2 条"），那时走的是正常的审批路径。
    """
    count = max_suggestions() if limit is None else max(1, int(limit))
    material = scan_sources(workdir=workdir, sessions_dir=sessions_dir)
    try:
        raw = ask(prompt(material, limit=count))
    except Exception as exc:      # 入口的回合可能抛（网络/取消/中断）
        return SuggestionRun(material, [], f'{type(exc).__name__}: {exc}')
    suggestions = parse_suggestions(str(raw or ''), limit=count)
    # 跑过就记：自动档的间隔判断靠它，手动跑同样算"今天已经建议过"。
    record_run(cache_dir, len(suggestions))
    return SuggestionRun(material, suggestions)


def prompt(material: dict, *, limit: int = 3) -> str:
    """生成给模型的只读建议提示。

    即使工具面已经裁了，提示里也再说一次：少一次无效尝试总归更好。
    建议会直接显示给用户，所以要求用中文、一条一行。
    """
    recent = material.get('recent_sessions') or []
    recent_text = '、'.join(
        str(item.get('title') or '') for item in recent) or '（无）'
    return (
        '你看看我本机的状态，给几条今天值得做的事。\n'
        '你只有只读工具，不要尝试改动任何东西。\n\n'
        f"工作区：{material.get('workspace')}\n"
        f"未完成的待办：{material.get('todos')}\n"
        f"最近的会话：{recent_text}\n\n"
        f'最多给 {limit} 条，一条一行，用中文，要具体到能直接动手；'
        '每条都要能从上面看到的状态里说清理由，没把握的不要写。'
        '如果确实没有值得做的事，只输出：NONE'
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


def record_answer(cache_dir, answer) -> int:
    """记一次建议回合的结果，返回解析到的条数。

    两个入口（TUI 的 `/suggest` 与行式 REPL）共用这一处：间隔判断、条数统计
    都从这里走，避免一边记一边忘。
    """
    count = len(parse_suggestions(str(answer or '')))
    record_run(cache_dir, count)
    return count
