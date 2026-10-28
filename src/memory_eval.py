"""记忆质量的异步 LLM-as-judge 评测。

为什么需要
----------
`tests/test_memory_benchmark.py` 是**离线**的：固定查询 + 固定记忆库，只能守住
"术语表覆盖内的召回率不退化"。它测不到真实使用里的两件事：

1. **召回相关性**：这一轮召回的记忆，对这一轮的问题真的有用吗？
2. **沉淀质量**：写进库里的记忆，是持久偏好还是一次性的噪音？

这两个只能看真实数据。PAHF 的评测框架把它们分成 injection（usage quality）与
distillation（capture quality），这里各做一项。

为什么必须异步
--------------
判一次要一次模型调用（1~3 秒）。放在回合里会直接拖慢对话——记忆是背景设施，
不能占用主路径。所以全部在回合结束后由 housekeeping 线程跑，结果只落盘，
不参与任何决策。

成本控制（三条硬约束）
----------------------
- **默认关闭**（`HARNESS_MEMORY_EVAL=1` 才开）：多数人不该为此付费。
- **采样**：每 N 轮才判一次（`HARNESS_MEMORY_EVAL_EVERY`，默认 10）。
- **单次上限**：一次只判 1 个回合 / 至多 8 条记忆，输出解析失败就丢弃。

判错了会怎样
------------
最坏结果是统计数字难看。评测结果**只写 jsonl + 展示**，不改记忆、不改召回、
不触发任何自动动作——这是刻意的：让"评测"能反过来动系统，等于给它一条
自我强化的路径（见 docs/AGENT_LEARNING_AND_RSI.md 的 RSI 讨论）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

#: 采样间隔默认值。10 轮判一次：一天几十轮对话也就几次调用。
DEFAULT_EVERY = 10
#: 单次最多判多少条记忆。
MAX_JUDGED = 8
#: 判一次的超时（秒）。判不完就放弃，不留悬挂线程。
TIMEOUT_S = 45.0
#: 报告文件（放在 .cache 下，与会话分开）。
REPORT_NAME = 'memory-eval.jsonl'

ENV = 'HARNESS_MEMORY_EVAL'
ENV_EVERY = 'HARNESS_MEMORY_EVAL_EVERY'


def enabled() -> bool:
    return os.getenv(ENV, '0').strip().lower() in ('1', 'true', 'yes', 'on')


def every() -> int:
    raw = os.getenv(ENV_EVERY, '').strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_EVERY
    return max(1, value)


def _path(cache_dir) -> Path:
    return Path(cache_dir) / REPORT_NAME


def should_sample(turn_no: int, *, every_n: int | None = None) -> bool:
    """按轮次决定这一轮要不要判。

    用轮次而不是概率：可复现，测试也能确定地命中。
    """
    if not enabled():
        return False
    step = every_n if every_n is not None else every()
    return turn_no > 0 and turn_no % step == 0


# ---------------------------------------------------------------- 提示词


RECALL_PROMPT = (
    "You are auditing a memory system. Below are (a) the user's latest message and "
    "(b) the memory notes that were retrieved and shown to the assistant.\n\n"
    "Judge only what is given. Return a JSON object:\n"
    '{{"relevant": [indices of notes that genuinely help answer the message], '
    '"missing": "one short phrase for the kind of note that was needed but '
    'absent, or empty string if nothing was missing"}}\n\n'
    "A note is relevant only if it would change or constrain the answer. "
    "Vague topical overlap is NOT relevant. Reply with ONLY the JSON object.\n\n"
    "USER MESSAGE:\n{message}\n\nNOTES:\n{notes}"
)

QUALITY_PROMPT = (
    "You are auditing stored assistant memories. For each numbered note decide if "
    "it is worth keeping long-term.\n\n"
    "Keep it if it is a durable preference, a correction the user made, or a stable "
    "fact that will still be true next month. Reject it if it is a one-off detail "
    "tied to a single task, a restatement of the user's current request, or too "
    "vague to act on.\n\n"
    'Return a JSON object: {{"keep": [indices], "drop": [indices]}} with every '
    "index appearing in exactly one list. Reply with ONLY the JSON object.\n\n"
    "NOTES:\n{notes}"
)


def _notes_block(entries: list[dict]) -> str:
    lines = []
    for index, item in enumerate(entries):
        desc = str(item.get('description') or '').strip()
        kind = str(item.get('type') or '?')
        lines.append(f'{index}. [{kind}] {desc}')
    return '\n'.join(lines)


def _extract_json(text: str):
    """从回复里取第一个 JSON 对象。模型常带代码围栏或解释。"""
    match = re.search(r'\{.*\}', str(text or ''), re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _indices(value, limit: int) -> list[int]:
    """把模型给的索引列表收敛成合法下标（去重、越界丢弃）。"""
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            continue
        if 0 <= item < limit and item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------- 判两项


def judge_recall(message: str, entries: list[dict], *, complete=None) -> dict:
    """判"这一轮召回的记忆有没有用"。返回 ``{relevant, missing, judged}``。"""
    entries = entries[:MAX_JUDGED]
    if not message.strip() or not entries:
        return {'relevant': [], 'missing': '', 'judged': 0}
    call = complete or _default_complete
    reply = call(RECALL_PROMPT.format(message=message.strip(),
                                      notes=_notes_block(entries)))
    data = _extract_json(reply) or {}
    return {
        'relevant': _indices(data.get('relevant'), len(entries)),
        'missing': ' '.join(str(data.get('missing') or '').split())[:120],
        'judged': len(entries),
    }


def judge_quality(entries: list[dict], *, complete=None) -> dict:
    """判"库里的记忆该不该长期保留"。返回 ``{keep, drop, judged}``。"""
    entries = entries[:MAX_JUDGED]
    if not entries:
        return {'keep': [], 'drop': [], 'judged': 0}
    call = complete or _default_complete
    reply = call(QUALITY_PROMPT.format(notes=_notes_block(entries)))
    data = _extract_json(reply) or {}
    keep = _indices(data.get('keep'), len(entries))
    drop = [i for i in _indices(data.get('drop'), len(entries)) if i not in keep]
    return {'keep': keep, 'drop': drop, 'judged': len(entries)}


def _default_complete(prompt: str) -> str:
    """走 memory 角色（与抽取同一档，避免占用主模型预算）。"""
    import llm_route
    response = llm_route.completion(
        'memory', messages=[{'role': 'user', 'content': prompt}],
        temperature=0, max_tokens=2000)
    message = response.choices[0].message
    return (message.content or '') or (getattr(message, 'reasoning_content', '') or '')


# ---------------------------------------------------------------- 记录与统计


def record(cache_dir, payload: dict) -> None:
    """追加一条评测结果。失败不抛——评测不该影响任何主流程。"""
    entry = {'ts': time.time(), **payload}
    path = _path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def load_records(cache_dir, limit: int = 200) -> list[dict]:
    path = _path(cache_dir)
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with path.open('r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def summarize(cache_dir, limit: int = 200) -> dict:
    """汇总最近若干次评测：召回精确率、缺失率、沉淀保留率。"""
    rows = load_records(cache_dir, limit=limit)
    judged = [row for row in rows if row.get('judged')]
    if not judged:
        return {'samples': 0}

    reco = [row for row in judged if row.get('kind') == 'recall']
    qual = [row for row in judged if row.get('kind') == 'quality']

    hits = sum(len(row.get('relevant') or []) for row in reco)
    shown = sum(int(row.get('judged') or 0) for row in reco)
    missing = sum(1 for row in reco if row.get('missing'))

    keep = sum(len(row.get('keep') or []) for row in qual)
    quality_total = sum(int(row.get('judged') or 0) for row in qual)

    return {
        'samples': len(judged),
        'recall_samples': len(reco),
        'recall_precision': round(hits / shown, 3) if shown else None,
        'missing_rate': round(missing / len(reco), 3) if reco else None,
        'quality_samples': len(qual),
        'keep_rate': round(keep / quality_total, 3) if quality_total else None,
    }


def format_summary(cache_dir) -> str:
    """`/memories quality` 的输出。"""
    stats = summarize(cache_dir)
    if not stats.get('samples'):
        state = '开' if enabled() else '关'
        return (f'（还没有评测数据；LLM 评测当前{state}'
                f'，用 HARNESS_MEMORY_EVAL=1 开启）')

    lines = [f'记忆评测（最近 {stats["samples"]} 次）']
    if stats.get('recall_precision') is not None:
        lines.append(f"  召回精确率：{stats['recall_precision']:.0%}"
                     f"（{stats['recall_samples']} 次采样）")
        lines.append(f"  召回缺失率：{stats['missing_rate']:.0%}")
    if stats.get('keep_rate') is not None:
        lines.append(f"  沉淀保留率：{stats['keep_rate']:.0%}"
                     f"（{stats['quality_samples']} 次采样）")
    lines.append('')
    lines.append('这是模型自己的判断，只作参考；不会自动修改记忆。')
    return '\n'.join(lines)


# ---------------------------------------------------------------- 异步入口

#: 正在跑的评测线程（测试用 wait_for_eval 等待）。
_threads: list[threading.Thread] = []
_lock = threading.Lock()


def run_async(*, cache_dir, turn_no: int, message: str,
              recalled: list[dict], library: list[dict] | None = None,
              complete=None) -> threading.Thread | None:
    """回合结束后异步判一次。返回线程句柄（未采样/未启用时返回 None）。

    只在 sampling 命中时跑，且始终是 daemon 线程：进程退出不等它。
    """
    if not should_sample(turn_no):
        return None
    if complete is None:
        complete = _default_complete

    def worker():
        if recalled:
            try:
                result = judge_recall(message, recalled, complete=complete)
                record(cache_dir, {'kind': 'recall', 'turn': turn_no, **result})
            except Exception as exc:
                # 判失败就这一轮没数据。打印而不是静默：吞掉异常会把模板
                # 花括号写错这类编程错误藏起来（已在实测中发生过）。
                print(f'  \033[90m[memory-eval] recall judge skipped: '
                      f'{type(exc).__name__}\033[0m')
        if library:
            try:
                result = judge_quality(library, complete=complete)
                record(cache_dir, {'kind': 'quality', 'turn': turn_no, **result})
            except Exception as exc:
                print(f'  \033[90m[memory-eval] quality judge skipped: '
                      f'{type(exc).__name__}\033[0m')

    thread = threading.Thread(target=worker, name=f'memory-eval-{turn_no}',
                              daemon=True)
    with _lock:
        _threads[:] = [item for item in _threads if item.is_alive()]
        _threads.append(thread)
    thread.start()
    return thread


def wait_for_eval(timeout: float = TIMEOUT_S) -> None:
    """等当前所有评测线程结束（测试与优雅退出用）。"""
    deadline = time.time() + timeout
    with _lock:
        pending = list(_threads)
    for thread in pending:
        thread.join(timeout=max(0.0, deadline - time.time()))
