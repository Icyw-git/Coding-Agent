"""自我改进的改动台账与回滚（B2）。

为什么必须 append-only
----------------------
这是"自我改进"的记录本身。如果它也能被改写，那"改了什么"就不可追溯，
整个自我改进机制失去意义——和会话事件流、审计同一原则。

为什么必须能回滚
----------------
RSI 的失败模式里最危险的不是"改错了"，而是"改错了但看起来变好了"。
能一键回到改动前，是把这类风险限制在可控范围的唯一办法。

存储
----
``<cache>/self-improvements.jsonl``：每次改动一行，只追加。
``snapshots/``：改动前的文件快照，按 change id 分目录。

只覆盖 Ring 1（skills / AGENTS.md 这类需审批的扩展面）。Ring 0（策略/审批/
tests）由 ``verifier_guard`` 硬拒，不进这里——它不该有"被改过"的记录。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

LEDGER_NAME = 'self-improvements.jsonl'
SNAPSHOT_DIR = 'snapshots'


def _ledger(cache_dir) -> Path:
    return Path(cache_dir) / LEDGER_NAME


def _snapshots(cache_dir) -> Path:
    return Path(cache_dir) / SNAPSHOT_DIR


def record(cache_dir, *, paths, summary: str, metric: dict | None = None,
           decision: str = 'pending', reason: str = '', workdir=None) -> str:
    """登记一次改动，返回 change id。

    ``paths`` 是本次动过的文件（相对 workdir）。登记前把它们**先快照**，
    这样即使进程崩在回滚之前，原件也还在磁盘上。

    快照失败不阻止登记：宁可没有回滚点，也不能因为快照问题让改动"没记录"。
    """
    change_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    root = Path(workdir) if workdir else Path.cwd()
    snapshot_root = _snapshots(cache_dir) / change_id
    saved = []
    for rel in paths:
        source = (root / rel).resolve()
        if not source.is_file():
            continue
        target = snapshot_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
            saved.append(rel)
        except OSError:
            pass

    entry = {
        'change_id': change_id,
        'ts': time.time(),
        'paths': list(paths),
        'snapshotted': saved,
        'summary': summary,
        'metric': metric or {},
        'decision': decision,
        'reason': reason,
        'workdir': str(root),
    }
    path = _ledger(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return change_id


def update_decision(cache_dir, change_id: str, decision: str,
                    reason: str = '', mechanism: str = '') -> bool:
    """追加判定结果（keep / observe / discard）。

    台账本身不改写。``reason`` 与 ``mechanism`` 是**人类可读文本，不进入任何判定**
    ——这是 OWASP ASI06（Memory & Context Poisoning）的直接防线：一旦 agent 自己
    写的"我改进了"能反向影响下一轮决策，就形成了正反馈回路
    （agent 写乐观总结 → 总结成为下一轮输入 → agent 读自己的乐观并在此基础上
    继续发挥）。
    """
    entry = {
        'change_id': change_id,
        'ts': time.time(),
        'decision_update': decision,
        'reason': reason,
        'mechanism': ' '.join(str(mechanism or '').split())[:200],
    }
    path = _ledger(cache_dir)
    if not path.is_file():
        return False
    # 只有已知的 change 才能更新：否则一次笔误会在台账里留下一笔
    # 指向不存在记录的"判定"，排查时像幽灵。
    known = {row.get('change_id') for row in entries(cache_dir, limit=10_000)}
    if change_id not in known:
        return False
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return True


def entries(cache_dir, limit: int = 100) -> list[dict]:
    """读台账，按 change_id 合并判定更新，返回最近的记录（旧的在前）。"""
    path = _ledger(cache_dir)
    if not path.is_file():
        return []
    records: dict[str, dict] = {}
    order: list[str] = []
    try:
        with path.open('r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                change_id = str(row.get('change_id') or '')
                if not change_id:
                    continue
                if 'decision_update' in row:
                    if change_id in records:
                        records[change_id]['decision'] = row['decision_update']
                        if row.get('reason'):
                            records[change_id]['reason'] = row['reason']
                        if row.get('mechanism'):
                            records[change_id]['mechanism'] = row['mechanism']
                    continue
                if change_id not in records:
                    order.append(change_id)
                records[change_id] = row
    except OSError:
        return []
    return [records[cid] for cid in order[-limit:]]


def rollback(cache_dir, change_id: str) -> list[str]:
    """把这次改动碰过的文件恢复成快照版本。返回恢复的路径。

    只恢复**快照里有的**文件：新建的文件没有快照（改动前不存在），删掉它们
    由调用方决定——自动删有风险（可能是别的进程刚写的）。
    """
    row = next((item for item in entries(cache_dir, limit=10_000)
                if item.get('change_id') == change_id), None)
    if row is None:
        return []
    root = Path(row.get('workdir') or Path.cwd())
    snapshot_root = _snapshots(cache_dir) / change_id
    restored = []
    for rel in row.get('snapshotted') or []:
        source = snapshot_root / rel
        if not source.is_file():
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
            restored.append(rel)
        except OSError:
            continue
    return restored


def created_paths(cache_dir, change_id: str) -> list[str]:
    """这次改动里**新增**的路径（没有快照的那些）。回滚时由人决定要不要删。"""
    row = next((item for item in entries(cache_dir, limit=10_000)
                if item.get('change_id') == change_id), None)
    if row is None:
        return []
    saved = set(row.get('snapshotted') or [])
    return [rel for rel in (row.get('paths') or []) if rel not in saved]


def format_ledger(cache_dir, limit: int = 12) -> str:
    """`/improvements` 的输出。"""
    rows = entries(cache_dir, limit=limit)
    if not rows:
        return ('（还没有自我改进记录）\n'
                '技能与 AGENTS.md 的改动会在这里留痕；策略/审批/测试不在此列。')
    lines = [f'自我改进记录（最近 {len(rows)} 次）']
    for row in reversed(rows):
        stamp = time.strftime('%m-%d %H:%M', time.localtime(row.get('ts') or 0))
        decision = row.get('decision') or 'pending'
        mark = {'keep': '✓', 'observe': '◐', 'discard': '✗',
                'pending': '…'}.get(decision, decision)
        lines.append(f"  {mark} {stamp}  {row.get('summary') or '(无说明)'}")
        if row.get('mechanism'):
            lines.append(f'      机制：{row["mechanism"]}')
        metric = row.get('metric') or {}
        if metric:
            parts = ' '.join(f'{k}={v}' for k, v in metric.items())
            lines.append(f'      指标：{parts}')
        if row.get('reason'):
            lines.append(f'      理由：{row["reason"]}')
        lines.append(f'      回滚：/improvements rollback {row.get("change_id")}')
    return '\n'.join(lines)
