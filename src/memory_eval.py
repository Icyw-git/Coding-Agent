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
    "(b) memory notes that were retrieved and shown to the assistant.\n\n"
    "Decide, for EACH note independently, whether it was worth showing.\n"
    "A note is RELEVANT only if BOTH hold:\n"
    "  (1) It concerns the same subject as the message (not just a shared word).\n"
    "  (2) It would change or constrain the answer — a preference to follow, a "
    "constraint to respect, or a fact the answer depends on.\n"
    "It is NOT relevant if: the overlap is only a common word; it is about a "
    "different project/entity with a similar name; knowing it would not alter "
    "what the assistant says; or it only restates the user's request.\n\n"
    'Return ONLY this JSON object: {{"relevant": [indices], '
    '"irrelevant": [indices], "abstain": [indices], '
    '"missing": "short phrase, or empty string", '
    '"confidence": "high" or "low"}}\n'
    "Every index must appear in exactly one of relevant/irrelevant/abstain. "
    "Use abstain when the message or the note is too vague to judge — do not guess; "
    "a guessed verdict is worse than a missing one.\n\n"
    "USER MESSAGE:\n{message}\n\nNOTES:\n{notes}"
)

QUALITY_PROMPT = (
    "You are auditing stored assistant memories. Decide, for EACH note "
    "independently, whether it is worth keeping long-term.\n\n"
    "KEEP only if BOTH hold:\n"
    "  (1) It is durable: still true and useful next month, not tied to one task.\n"
    "  (2) It is specific enough to act on (names the thing, the rule, or the "
    "condition).\n"
    "KEEP examples: a standing preference; a correction the user made; a stable "
    "fact about the environment or workflow.\n"
    "DROP examples: a one-off detail from a single task; a restatement of the "
    "current request; a vague impression ('likes good code'); something that will "
    "be stale next month (a version number, a temporary path).\n"
    "Use abstain when there is not enough context to tell — do not guess.\n\n"
    'Return ONLY this JSON object: {{"keep": [indices], "drop": [indices], '
    '"abstain": [indices], "confidence": "high" or "low"}}\n'
    "Every index must appear in exactly one list.\n\n"
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


def _partition(data: dict, count: int) -> dict:
    """把模型给的三组索引收敛成互斥集合。

    优先级 keep/relevant > drop/irrelevant > abstain：模型偶尔会把同一索引写进
    多个列表，重复计数会让精确率虚高。abstain 不参与计分（见 summarize）。
    """
    positive = _indices(data.get('relevant') or data.get('keep'), count)
    negative = [i for i in _indices(data.get('irrelevant') or data.get('drop'), count)
                if i not in positive]
    abstain = [i for i in _indices(data.get('abstain'), count)
               if i not in positive and i not in negative]
    return {'positive': positive, 'negative': negative, 'abstain': abstain}


def _confidence(value) -> str:
    text = str(value or '').strip().casefold()
    return 'high' if text.startswith('high') else 'low'


def judge_recall(message: str, entries: list[dict], *, complete=None) -> dict:
    """判"这一轮召回的记忆有没有用"。

    返回 ``{relevant, irrelevant, abstain, missing, confidence, judged}``。
    ``abstain`` 是刻意的出口：判据说"证据不足就别猜"，因为奖励作弊与误判都
    集中在模糊样本上——把它们单独计数，比强行二分类诚实。
    """
    entries = entries[:MAX_JUDGED]
    if not message.strip() or not entries:
        return {'relevant': [], 'irrelevant': [], 'abstain': [],
                'missing': '', 'confidence': 'low', 'judged': 0}
    call = complete or _default_complete
    reply = call(RECALL_PROMPT.format(message=message.strip(),
                                      notes=_notes_block(entries)))
    data = _extract_json(reply) or {}
    parts = _partition(data, len(entries))
    return {
        'relevant': parts['positive'],
        'irrelevant': parts['negative'],
        'abstain': parts['abstain'],
        'missing': ' '.join(str(data.get('missing') or '').split())[:120],
        'confidence': _confidence(data.get('confidence')),
        'judged': len(entries),
    }


def judge_quality(entries: list[dict], *, complete=None) -> dict:
    """判"库里的记忆该不该长期保留"。

    返回 ``{keep, drop, abstain, confidence, judged}``。
    """
    entries = entries[:MAX_JUDGED]
    if not entries:
        return {'keep': [], 'drop': [], 'abstain': [],
                'confidence': 'low', 'judged': 0}
    call = complete or _default_complete
    reply = call(QUALITY_PROMPT.format(notes=_notes_block(entries)))
    data = _extract_json(reply) or {}
    parts = _partition(data, len(entries))
    return {
        'keep': parts['positive'],
        'drop': parts['negative'],
        'abstain': parts['abstain'],
        'confidence': _confidence(data.get('confidence')),
        'judged': len(entries),
    }


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


def cohen_kappa(human: list, judge: list) -> float | None:
    """两个标注序列的 Cohen's κ（机会校正一致率）。

    为什么不用原始一致率：大规模评测显示原始一致率会把判别能力**高估 33~41
    个百分点**，而且它对标签分布很敏感（一方全判"相关"就能拿到高分）。
    κ 扣掉机会一致，才能说明"判官真的在判"。

    返回 None 表示无法计算（样本为空，或双方都只使用了一个类别——此时 κ 无定义）。
    """
    if len(human) != len(judge) or not human:
        return None
    pairs = list(zip(human, judge, strict=True))
    total = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / total
    labels = {value for pair in pairs for value in pair}
    if len(labels) < 2:
        return None
    expected = 0.0
    for label in labels:
        p_human = sum(1 for a, _b in pairs if a == label) / total
        p_judge = sum(1 for _a, b in pairs if b == label) / total
        expected += p_human * p_judge
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1.0 - expected), 3)


def kappa_band(value: float | None) -> str:
    """κ 的通俗解释（Landis & Koch 的常用分档）。"""
    if value is None:
        return '无法计算（样本不足或只有一个类别）'
    if value < 0:
        return '不如随机（判官不可用）'
    if value < 0.21:
        return '几乎不一致（判官不可用）'
    if value < 0.41:
        return '轻微一致（只能看趋势）'
    if value < 0.61:
        return '中等一致（可参考）'
    if value < 0.81:
        return '高度一致（可作验收依据）'
    return '几乎完全一致'


def validate_against_labels(human: list, judge: list) -> dict:
    """把"判官 vs 人工标签"的一致率算成可解读的报告。

    这不是运行时逻辑，是**校准流程**：维护一组人工标注的样本，用当前提示词
    跑一遍，看 κ 落在哪一档。低于"中等一致"就不该把评测数字当结论。
    """
    kappa = cohen_kappa(human, judge)
    return {
        'samples': len(human),
        'raw_agreement': (round(sum(1 for a, b in zip(human, judge, strict=True) if a == b)
                                / len(human), 3) if human else None),
        'kappa': kappa,
        'band': kappa_band(kappa),
        'usable': kappa is not None and kappa >= 0.41,
    }


def summarize(cache_dir, limit: int = 200) -> dict:
    """汇总最近若干次评测。

    弃权（abstain）**从分母里剔除**而不是算作判错：判据说"证据不足"时，
    记成错误会惩罚正确的谨慎。但弃权率本身要报出来——弃权太多说明样本质量差
    或判据不适合这批数据，指标就不该被当成结论。
    """
    rows = load_records(cache_dir, limit=limit)
    judged = [row for row in rows if row.get('judged')]
    if not judged:
        return {'samples': 0}

    reco = [row for row in judged if row.get('kind') == 'recall']
    qual = [row for row in judged if row.get('kind') == 'quality']

    def _count(rows_, key):
        return sum(len(row.get(key) or []) for row in rows_)

    def _total(rows_):
        return sum(int(row.get('judged') or 0) for row in rows_)

    def _rate(numerator, denominator):
        return round(numerator / denominator, 3) if denominator else None

    reco_total = _total(reco)
    qual_total = _total(qual)
    missing = sum(1 for row in reco if row.get('missing'))
    low_conf = sum(1 for row in judged if row.get('confidence') == 'low')

    return {
        'samples': len(judged),
        'recall_samples': len(reco),
        'recall_precision': _rate(_count(reco, 'relevant'),
                                  reco_total - _count(reco, 'abstain')),
        'missing_rate': _rate(missing, len(reco)),
        'recall_abstain_rate': _rate(_count(reco, 'abstain'), reco_total),
        'quality_samples': len(qual),
        'keep_rate': _rate(_count(qual, 'keep'),
                           qual_total - _count(qual, 'abstain')),
        'quality_abstain_rate': _rate(_count(qual, 'abstain'), qual_total),
        'low_confidence_rate': _rate(low_conf, len(judged)),
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

    def _pct(value):
        return f"{value:.0%}" if value is not None else '—'

    lines.append(f"  弃权率：召回 {_pct(stats.get('recall_abstain_rate'))}"
                 f" / 沉淀 {_pct(stats.get('quality_abstain_rate'))}"
                 f" · 低置信 {_pct(stats.get('low_confidence_rate'))}")
    if (stats.get('low_confidence_rate') or 0) > 0.5:
        lines.append('  低置信占比超过一半：这批数字只能当趋势看，别当结论。')
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
