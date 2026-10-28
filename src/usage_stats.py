"""使用统计：把散在事件流里的 usage / 工具 / 审批聚合成能看的东西。

为什么需要
----------
token、工具调用、耗时、审批、记忆写入**都已经落在会话事件流里**，但没有任何
聚合视图。用户问"我这周花了多少"、"哪个工具用得最多"、"记忆召回到底命中没有"，
只能自己 grep jsonl。

更重要的用途：**这是判断"记忆/RSI 到底有没有用"的最小前提**。没有使用统计，
就只能不断加机制、永远无法判断。

数据来源（全部已有，不新增埋点）
--------------------------------
| 维度 | 事件 | 字段 |
|---|---|---|
| token | `usage` | prompt / completion / cached / total |
| 请求耗时 | `usage` 与 `model_request_completed` | request_ms / ttfb_ms |
| 工具 | `tool_profile` | tools[].name, duration_ms, status |
| 回合 | `turn_started` / `turn_completed` | ts |
| 审批与记忆 | 审计事件（同一事件流） | approval_auto、policy_denied、memory_* |

设计约束
--------
- **只读**：不动任何状态，不做缓存（统计是低频操作，一致性比速度重要）。
- **有预算**：最多扫 N 个会话文件、单文件最多 M 字节前缀之外的都不读——
  统计不该因为历史很长而卡住。
- **口径写清**：缓存命中率 = cached / prompt，分母是**输入 token**，不是总数。
  口径含糊的统计比没有统计更容易误导。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: 默认统计窗口（天）。
DEFAULT_DAYS = 7
#: 最多扫多少个会话文件（按 mtime 取最近的）。
MAX_FILES = 200
#: 单文件最多读多少字节（超长会话取尾部——近期数据更有价值）。
MAX_FILE_BYTES = 8 * 1024 * 1024


#: 关心的事件类型。在 **bytes** 层做预筛：会话里绝大多数行是 message /
#: tool_started，它们的正文里几乎不出现这些键，所以预筛很干净——误命中最多
#: 多解码一行，类型仍由后续判断确认。实测 22 个会话全解码 1095ms。
_INTERESTING = (
    b'"usage"', b'"tool_profile"', b'"turn_completed"',
    b'"approval_auto"', b'"policy_denied"',
    b'"memory_written"', b'"memory_forgotten"', b'"memory_redacted"',
    b'"proactive_suggested"',
)


def _iter_events(path: Path):
    """逐个产出关心的事件；坏行跳过。只读文件尾部（超长时）。"""
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > MAX_FILE_BYTES:
                handle.seek(size - MAX_FILE_BYTES)
                handle.readline()          # 丢掉半行
            for raw in handle:
                if not any(marker in raw for marker in _INTERESTING):
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue
    except OSError:
        return


def _recent_files(sessions_dir, *, days: int, limit: int) -> list[Path]:
    directory = Path(sessions_dir)
    if not directory.is_dir():
        return []
    cutoff = time.time() - days * 86400
    rows = []
    for path in directory.glob('session_*.jsonl'):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        rows.append((stat.st_mtime, path))
    rows.sort(reverse=True)
    return [path for _mtime, path in rows[:limit]]


def _pct(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None


def collect(sessions_dir, *, days: int = DEFAULT_DAYS,
            limit: int = MAX_FILES) -> dict:
    """聚合最近 ``days`` 天的使用情况。"""
    files = _recent_files(sessions_dir, days=days, limit=limit)
    tokens = {'prompt': 0, 'completion': 0, 'cached': 0, 'total': 0, 'reasoning': 0}
    requests = 0
    request_ms: list[float] = []
    tools: dict[str, dict] = {}
    tool_batches = 0
    sessions = 0
    turns = 0
    approvals = {'auto': 0, 'denied': 0}
    memory = {'written': 0, 'forgotten': 0, 'redacted': 0, 'suggested': 0}
    models: dict[str, int] = {}

    for path in files:
        # 活跃会话的判定：**有任何被预筛保留的事件**就算。
        # 不能依赖 turn_started——它不在关心列表里，会被字节预筛挡掉。
        active = False
        for event in _iter_events(path):
            active = True
            kind = event.get('type') or ''
            if kind == 'usage':
                requests += 1
                for key in tokens:
                    value = event.get(key)
                    if isinstance(value, (int, float)):
                        tokens[key] += int(value)
                ms = event.get('request_ms')
                if isinstance(ms, (int, float)) and ms > 0:
                    request_ms.append(float(ms))
                model = str(event.get('model') or '').strip()
                if model:
                    models[model] = models.get(model, 0) + 1
            elif kind == 'tool_profile':
                tool_batches += 1
                for item in event.get('tools') or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get('name') or '').strip()
                    if not name:
                        continue
                    row = tools.setdefault(name, {'calls': 0, 'failed': 0,
                                                  'total_ms': 0.0})
                    row['calls'] += 1
                    if event.get('status') == 'error':
                        row['failed'] += 1
                    duration = item.get('duration_ms')
                    if isinstance(duration, (int, float)):
                        row['total_ms'] += float(duration)
            elif kind == 'turn_completed':
                turns += 1
            elif kind == 'approval_auto':
                approvals['auto'] += 1
            elif kind == 'policy_denied':
                approvals['denied'] += 1
            elif kind == 'memory_written':
                memory['written'] += 1
            elif kind == 'memory_forgotten':
                memory['forgotten'] += 1
            elif kind == 'memory_redacted':
                memory['redacted'] += 1
            elif kind == 'proactive_suggested':
                memory['suggested'] += 1
        if active:
            sessions += 1

    request_ms.sort()

    def _quantile(values: list[float], q: float):
        if not values:
            return None
        index = min(len(values) - 1, int(len(values) * q))
        return round(values[index], 1)

    for row in tools.values():
        row['total_ms'] = round(row['total_ms'], 1)
        row['avg_ms'] = round(row['total_ms'] / row['calls'], 2) if row['calls'] else 0

    return {
        'window_days': days,
        'sessions': sessions,
        'turns': turns,
        'requests': requests,
        'tokens': tokens,
        # 口径：缓存命中率的分母是**输入** token，不是总数
        'cache_hit_rate': _pct(tokens['cached'], tokens['prompt']),
        'request_ms_p50': _quantile(request_ms, 0.5),
        'request_ms_p90': _quantile(request_ms, 0.9),
        'tools': dict(sorted(tools.items(), key=lambda kv: -kv[1]['calls'])),
        'tool_batches': tool_batches,
        'approvals': approvals,
        'memory': memory,
        'models': dict(sorted(models.items(), key=lambda kv: -kv[1])),
    }


def format_report(stats: dict, *, top_tools: int = 8) -> str:
    """`/usage` 的文本输出。"""
    # 只看请求和回合会漏掉「只跑了本地命令」的情况——那也有会话、也有工具调用，
    # 报「没有数据」会让人以为统计坏了。
    if not any(stats.get(key) for key in
               ('requests', 'turns', 'sessions', 'tools', 'tool_batches')):
        return (f"（最近 {stats.get('window_days')} 天没有可统计的会话）\n"
                '跑几轮对话后再看。')

    tokens = stats['tokens']

    def _k(value: int) -> str:
        return f'{value / 1000:.1f}k' if value >= 1000 else str(value)

    lines = [f"使用统计（最近 {stats['window_days']} 天）",
             f"  会话 {stats['sessions']} · 回合 {stats['turns']} · "
             f"模型请求 {stats['requests']}"]

    if tokens['total']:
        lines.append(f"  token：输入 {_k(tokens['prompt'])}（其中缓存 "
                     f"{_k(tokens['cached'])}）· 输出 {_k(tokens['completion'])} · "
                     f"合计 {_k(tokens['total'])}")
        hit = stats.get('cache_hit_rate')
        if hit is not None:
            lines.append(f"  缓存命中率：{hit:.0%}（缓存 / 输入）")

    if stats.get('request_ms_p50') is not None:
        lines.append(f"  请求耗时：p50 {stats['request_ms_p50']:.0f}ms · "
                     f"p90 {stats['request_ms_p90']:.0f}ms")

    if stats.get('models'):
        top = '、'.join(f'{name}×{count}'
                        for name, count in list(stats['models'].items())[:3])
        lines.append(f'  模型：{top}')

    tools = stats.get('tools') or {}
    if tools:
        lines.append(f'  工具调用（{stats.get("tool_batches", 0)} 批）：')
        for name, row in list(tools.items())[:top_tools]:
            fail = f" · 失败 {row['failed']}" if row['failed'] else ''
            lines.append(f"    {name:22} {row['calls']:>4} 次  "
                         f"均 {row['avg_ms']:.0f}ms{fail}")

    approvals = stats.get('approvals') or {}
    if approvals.get('auto') or approvals.get('denied'):
        lines.append(f"  审批：自动放行 {approvals.get('auto', 0)} · "
                     f"拒绝 {approvals.get('denied', 0)}")

    memory = stats.get('memory') or {}
    if any(memory.values()):
        lines.append(f"  记忆：写入 {memory.get('written', 0)} · "
                     f"遗忘 {memory.get('forgotten', 0)} · "
                     f"脱敏 {memory.get('redacted', 0)}")
        if memory.get('suggested'):
            lines.append(f"  主动建议：{memory['suggested']} 次")

    if not tools and not tokens['total']:
        lines.append('  提示：这期间没有模型请求，可能是纯本地操作。')
    return '\n'.join(lines)
