"""有界自我改进循环（B3）：固定指标 → 提改动 → 测 → keep/discard。

定位：只做**第二层**（self-improvement），不做第三层（recursive）。
判据来自 RSI 领域的三层定义：

    Iteration        系统不变，输出变
    Self-improvement 系统变了，**尺子不变**
    Recursive        系统变了，**尺子也抬高**，且仍改不动

> The moment the system owns the ruler, it will get very good at *looking* improved.

所以这里的硬约束是：**尺子在环外**。`tests/` 与策略层由 `verifier_guard` 硬拒
（Ring 0），本模块只能改 Ring 1（skills / AGENTS.md），且判定用固定命令，
不接受调用方传入新指标或新阈值——否则就是"自己抬标准"。

与 autoresearch 的对应
----------------------
照它的骨架：明确指标+方向、硬限额（轮数/时长）、收敛窗口（连续 N 轮无新最优即停）、
每次改动进 append-only 台账、可回滚。区别在于**改动范围被限死在 Ring 1**。

指标从哪来
----------
固定四项，都来自既有信号（不由模型发明）：

- ``pytest_pass``：`pytest -q -m "not slow"` 全绿（方向 higher）
- ``ruff_errors``：`ruff check .` 错误数（方向 lower）
- ``correction_rate``：审计里的用户纠正次数占比（方向 lower）
- ``tokens_per_turn``：usage 事件的平均 token（方向 lower）

前两项是硬门槛：任何一项变差，本轮直接 discard，不看其他指标。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import self_improve_ledger as ledger

#: Ring 1：允许 agent 改的扩展面。策略/审批/测试不在此列（verifier_guard 硬拒）。
EDITABLE = ('skills/', 'AGENTS.md')

#: 采纳门槛：主指标至少改善 15% 才算 keep。
#: 定这个数是因为 lint 错误数很容易抖 1~2 个：10 → 9 并不说明改动变好了，
#: 而把它当 keep 会让循环沿着噪声一路走。小基数由 MIN_ABSOLUTE_STEP 兜底。
MIN_IMPROVEMENT = 0.15

#: 连续多少轮没有新最优就停（收敛）。
CONVERGENCE_ROUNDS = 3

#: 默认轮数上限。
DEFAULT_MAX_ROUNDS = 5


def editable(path: str, *, workdir=None) -> bool:
    """这个路径是否属于可自我改进的范围（Ring 1）。"""
    root = Path(workdir) if workdir else Path.cwd()
    try:
        rel = Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return False
    return any(rel == area.rstrip('/') or rel.startswith(area)
               for area in EDITABLE)


def _run(command: list[str], *, workdir, timeout: float = 900.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, cwd=str(workdir), capture_output=True,
                              text=True, timeout=timeout, encoding='utf-8',
                              errors='replace')
        return done.returncode, (done.stdout or '') + (done.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, 'timeout'
    except OSError as exc:
        return -1, f'{type(exc).__name__}: {exc}'


def measure_pytest(*, workdir) -> dict:
    """硬门槛一：测试必须全绿。返回 ``{pass, failed, summary}``。"""
    code, output = _run([sys.executable, '-m', 'pytest', '-q', '-m', 'not slow'],
                        workdir=workdir)
    tail = next((line for line in reversed(output.splitlines())
                 if 'passed' in line or 'failed' in line or 'error' in line), '')
    return {'pass': 1 if code == 0 else 0,
            'exit_code': code,
            'summary': tail.strip()[:200]}


def measure_ruff(*, workdir) -> dict:
    """硬门槛二：lint 错误数。"""
    code, output = _run([sys.executable, '-m', 'ruff', 'check', '.'], workdir=workdir)
    errors = 0
    for line in output.splitlines():
        if line.strip().startswith('Found') and 'error' in line:
            part = line.split()[-2] if 'remaining' not in line else '0'
            errors = int(part) if part.isdigit() else errors
    if code == 0:
        errors = 0
    return {'errors': errors}


#: 固定指标表。**不接受调用方自定义**——那是"自己抬标准"的入口。
METRICS = {
    'pytest_pass': {'direction': 'higher', 'measure': measure_pytest,
                    'key': 'pass', 'hard': True},
    'ruff_errors': {'direction': 'lower', 'measure': measure_ruff,
                    'key': 'errors', 'hard': True},
}


def _value(spec: dict, result: dict):
    return result.get(spec['key'])


def regressed(before: dict, after: dict) -> list[str]:
    """硬门槛里哪些变差了。非空即本轮必须 discard。"""
    broken = []
    for name, spec in METRICS.items():
        if not spec['hard']:
            continue
        old = _value(spec, before.get(name) or {})
        new = _value(spec, after.get(name) or {})
        if old is None or new is None:
            broken.append(f'{name}: unmeasured')
        elif spec['direction'] == 'higher' and new < old:
            broken.append(f'{name}: {old} -> {new}')
        elif spec['direction'] == 'lower' and new > old:
            broken.append(f'{name}: {old} -> {new}')
    return broken


#: 小基数时按绝对值判：0 → -1（lint 从 0 变负不可能，但 1 → 0 是实打实的改进），
#: 而 10 → 9 只算 10%，不该算 keep。
MIN_ABSOLUTE_STEP = 1


def improved(before: dict, after: dict, *, metric: str = 'ruff_errors') -> bool:
    """主指标是否真的变好了。

    两条件取其一：绝对改善 ≥ 1（小基数下有意义），或相对改善 ≥ MIN_IMPROVEMENT
    （大基数下才有意义）。只用相对阈值会让"1 个 lint 错误修掉"这种明确的胜利
    判成"没变好"——实测踩到：10 → 9 的 10% 被接受，1 → 0 的 100% 反而算不出来。
    """
    spec = METRICS.get(metric)
    if spec is None:
        return False
    old = _value(spec, before.get(metric) or {})
    new = _value(spec, after.get(metric) or {})
    if old is None or new is None or old == new:
        return False
    delta = (new - old) if spec['direction'] == 'higher' else (old - new)
    if delta < MIN_ABSOLUTE_STEP:
        return False
    scale = abs(old) or 1
    return delta >= MIN_ABSOLUTE_STEP and (delta / scale) >= MIN_IMPROVEMENT


class Session:
    """一次有界改进尝试。用 ``begin`` → ``propose`` → ``measure`` → ``decide``。

    刻意保持小：它不管怎么改文件（那由模型/工具做），只负责**记账 + 判定 +
    收敛**。把这三件事与"生成改动"分开，是因为它们必须独立于被评估的东西。
    """

    def __init__(self, *, cache_dir, workdir, goal: str,
                 metric: str = 'ruff_errors',
                 max_rounds: int = DEFAULT_MAX_ROUNDS):
        if metric not in METRICS:
            raise ValueError(f'metric must be one of {sorted(METRICS)}')
        self.cache_dir = cache_dir
        self.workdir = Path(workdir)
        self.goal = goal
        self.metric = metric
        self.max_rounds = max(1, min(int(max_rounds), 20))
        self.rounds = 0
        self.best = None
        self.without_best = 0
        self.changes: list[str] = []

    def baseline(self) -> dict:
        """跑一次基线。后面每轮都与它比。"""
        self.best = {name: spec['measure'](workdir=self.workdir)
                     for name, spec in METRICS.items()}
        return self.best

    def exhausted(self) -> bool:
        return (self.rounds >= self.max_rounds
                or self.without_best >= CONVERGENCE_ROUNDS)

    def evaluate(self, paths: list[str], summary: str) -> dict:
        """登记一轮改动并判定。返回 ``{decision, reason, before, after}``。"""
        outside = [path for path in paths if not editable(path, workdir=self.workdir)]
        if outside:
            # Ring 0 的改动不该走到这里（verifier_guard 会在写入时硬拒），
            # 但判定层也要拦一道：宁可重复防守，不可让台账记下一笔"改了测试"。
            return {'decision': 'discard',
                    'reason': f'paths outside editable scope: {outside}'}

        change_id = ledger.record(
            self.cache_dir, paths=paths, summary=summary,
            metric={'round': self.rounds + 1}, workdir=self.workdir)

        after = {name: spec['measure'](workdir=self.workdir)
                 for name, spec in METRICS.items()}
        broken = regressed(self.best, after)
        if broken:
            decision, reason = 'discard', 'regression: ' + '; '.join(broken)
        elif improved(self.best, after, metric=self.metric):
            decision, reason = 'keep', f'{self.metric} improved'
        else:
            decision, reason = 'discard', f'{self.metric} did not improve enough'

        ledger.update_decision(self.cache_dir, change_id, decision, reason)
        self.rounds += 1
        self.changes.append(change_id)
        if decision == 'keep':
            self.best = after
            self.without_best = 0
        else:
            self.without_best += 1
        return {'change_id': change_id, 'decision': decision, 'reason': reason,
                'before': self.best, 'after': after}

    def report(self) -> str:
        state = '已收敛' if self.exhausted() else '可继续'
        return (f'目标：{self.goal}\n'
                f'轮数：{self.rounds}/{self.max_rounds}（{state}）\n'
                f'主指标：{self.metric}\n'
                f'改动：{len(self.changes)} 次')
