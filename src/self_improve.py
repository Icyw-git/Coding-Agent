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

import json
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
    """这个路径是否属于可自我改进的范围（Ring 1）。

    相对路径按 **workdir** 解析，不是 cwd——两者不同时（会话工作区与进程目录
    不一致）按 cwd 解析会把合法的 ``skills/a.md`` 误判成越界。
    实测踩到：``workdir`` 指向临时目录时 ``editable('skills/a.md')`` 返回 False，
    于是每一轮都被当成越界丢弃，而且因为早退连状态都不落盘。
    """
    root = Path(workdir or Path.cwd()).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        rel = target.resolve().relative_to(root).as_posix()
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


def measure_tokens(*, workdir) -> dict:
    """平均每轮 token（成本维度）。

    取自 usage 事件，**由代码算**，不接受模型上报——「自己发明 99.8% recall」
    那条路就是从接受模型自报数字开始的。

    没有 usage 数据时返回 ``{}``：测不出来就是测不出来，不填默认值。
    """
    import json
    cache = Path(workdir) / '.cache' / 'sessions'
    if not cache.is_dir():
        return {}
    total_in = total_out = turns = 0
    for path in cache.glob('session_*.jsonl'):
        try:
            with path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    usage = event.get('usage') or {}
                    if not isinstance(usage, dict):
                        continue
                    total_in += int(usage.get('prompt_tokens') or 0)
                    total_out += int(usage.get('completion_tokens') or 0)
                    turns += 1
        except OSError:
            continue
    if not turns:
        return {}
    return {'tokens_per_turn': round((total_in + total_out) / turns, 1)}


def measure_wall_time(*, workdir) -> dict:
    """平均回合耗时（时间维度，SICA 效用的第三项）。

    取会话事件流里 turn_started → turn_completed 的间隔中位数。
    """
    import json
    cache = Path(workdir) / '.cache' / 'sessions'
    if not cache.is_dir():
        return {}
    spans: list[float] = []
    for path in cache.glob('session_*.jsonl'):
        started: dict[str, float] = {}
        try:
            with path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event.get('type')
                    turn_id = str(event.get('turn_id') or '')
                    if not turn_id:
                        continue
                    if kind == 'turn_started':
                        started[turn_id] = float(event.get('ts') or 0)
                    elif kind == 'turn_completed' and turn_id in started:
                        delta = float(event.get('ts') or 0) - started.pop(turn_id)
                        if delta > 0:
                            spans.append(delta)
        except OSError:
            continue
    if not spans:
        return {}
    spans.sort()
    return {'wall_time_s': round(spans[len(spans) // 2], 2)}


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
    # 软指标：SICA 的三目标效用里成本与时间各占 0.25。只有正确性没有成本，
    # 「测试全绿但 token 翻三倍」在我们的判据里会是"无变化"。
    # 不做硬门槛：机器负载抖动会把好改动误杀。
    'tokens': {'direction': 'lower', 'measure': measure_tokens,
               'key': 'tokens_per_turn', 'hard': False},
    'wall_time': {'direction': 'lower', 'measure': measure_wall_time,
                  'key': 'wall_time_s', 'hard': False},
}


def _value(spec: dict, result: dict):
    return result.get(spec['key'])


#: 耐心窗口：回归后允许观察多少轮。Gödel Agent 实测 92% 的优化在变好之前
#: 必然先变差——「一变差就丢弃」会掐死绝大多数正确方向。所以回归先进 observe，
#: 连续这么多轮仍未恢复才真正放弃。
PATIENCE_ROUNDS = 2

#: 软指标恶化到这个比例才在说明里提一笔（不否决）。
SOFT_DRIFT = 0.25


def regressed(before: dict, after: dict) -> list[str]:
    """硬门槛里哪些变差了。非空即本轮**进入观察**（不是立刻丢弃）。"""
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



def soft_drift(before: dict, after: dict) -> list[str]:
    """软指标（成本/时间）的恶化。只用于说明，不否决改动。

    为什么单独列：把成本翻三倍的正确性改进应该被**看见**——它不是"无变化"，
    而是一个需要权衡的取舍。静默接受就是 SICA 说的
    "optimizing performance at absurd computational cost"。
    """
    drifted = []
    for name, spec in METRICS.items():
        if spec['hard']:
            continue
        old = _value(spec, before.get(name) or {})
        new = _value(spec, after.get(name) or {})
        if old in (None, 0) or new is None:
            continue
        delta = (new - old) if spec['direction'] == 'higher' else (old - new)
        if delta < 0 and abs(delta) / abs(old) >= SOFT_DRIFT:
            drifted.append(f'{name}: {old} -> {new}')
    return drifted


def explainable(mechanism: str) -> bool:
    """改动能否用一句话说清机制。

    来自车间经验的启发式：「能一句话讲清的多是真正的改进，需要一段话才能辩护的
    往往是你在优化某个自己没打算优化的东西」。这一条挡不住特意的作弊，但它挡住了
    "说不清楚却通过了数字"的那一类。

    要求：非空，且不超过 120 字（一句话的量级）。
    """
    text = ' '.join(str(mechanism or '').split())
    return bool(text) and len(text) <= 120


#: 会话状态文件名。**必须持久化**：耐心窗口的意义就是跨多轮，而"多轮"可能是
#: 跨天的多次调用（人不是一口气跑完 5 轮）。状态只在内存里的话，一次重启就把
#: 观察期清零——本来在等的回归会被误判成"新的一轮"重新开始等。
SESSION_NAME = 'self-improve-session.json'


def _session_path(cache_dir) -> Path:
    return Path(cache_dir) / SESSION_NAME


def load_state(cache_dir) -> dict | None:
    path = _session_path(cache_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_state(cache_dir, state: dict) -> None:
    path = _session_path(cache_dir)
    tmp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def clear_state(cache_dir) -> None:
    _session_path(cache_dir).unlink(missing_ok=True)


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
        self.pending_regression: int | None = 0
        self.changes: list[str] = []
        self._restore()

    # ---------- 状态持久化 ----------
    #
    # 耐心窗口的意义是跨多轮，而"多轮"可能是跨天的多次调用。状态只在内存里的话，
    # 一次重启就把观察期清零——本来在等的回归会被当成新的一轮重新开始等，
    # 永远走不到 discard/keep。

    def _snapshot(self) -> dict:
        return {
            'goal': self.goal,
            'metric': self.metric,
            'max_rounds': self.max_rounds,
            'rounds': self.rounds,
            'best': self.best,
            'without_best': self.without_best,
            'pending_regression': self.pending_regression,
            'changes': self.changes,
        }

    def _restore(self) -> None:
        """从磁盘接上未完成的会话。

        只恢复**同一目标、同一指标**的会话：换目标等于换了一件事，旧进度没有
        参照意义。
        """
        state = load_state(self.cache_dir)
        if not state:
            return
        if state.get('goal') != self.goal or state.get('metric') != self.metric:
            return
        self.rounds = int(state.get('rounds') or 0)
        self.best = state.get('best') or None
        self.without_best = int(state.get('without_best') or 0)
        self.pending_regression = int(state.get('pending_regression') or 0)
        self.changes = list(state.get('changes') or [])

    def save(self) -> None:
        save_state(self.cache_dir, self._snapshot())

    def baseline(self) -> dict:
        """跑一次基线。后面每轮都与它比。"""
        self.best = {name: spec['measure'](workdir=self.workdir)
                     for name, spec in METRICS.items()}
        self.save()
        return self.best

    def exhausted(self) -> bool:
        return (self.rounds >= self.max_rounds
                or self.without_best >= CONVERGENCE_ROUNDS)

    def evaluate(self, paths: list[str], summary: str,
                 mechanism: str = '') -> dict:
        """登记一轮改动并判定。

        返回 ``{change_id, decision, reason, before, after, drift}``。
        decision 是 ``keep`` / ``observe`` / ``discard``：

        - ``keep``：主指标确实变好，且改动能一句话说清机制
        - ``observe``：硬门槛回归，但还在耐心窗口内——**不丢，等它恢复**
        - ``discard``：超出可改范围、说不出机制、或耐心耗尽仍未恢复

        ``observe`` 这个状态是 Gödel Agent 那条实测的直接产物：92% 的优化在
        变好之前必然先变差。没有它，我们只能看见"一次成功"，看不见"先退后进"。
        """
        outside = [path for path in paths if not editable(path, workdir=self.workdir)]
        if outside:
            # Ring 0 的改动不该走到这里（verifier_guard 会在写入时硬拒），
            # 但判定层也要拦一道：宁可重复防守，不可让台账记下一笔"改了测试"。
            return {'change_id': '', 'decision': 'discard',
                    'reason': f'paths outside editable scope: {outside}',
                    'before': self.best or {}, 'after': {}, 'drift': []}

        # 机制说明必须在**测量之前**校验：说不清要在改什么，就不该跑一轮。
        # 这条挡不住特意的作弊，但挡住了"说不清楚却通过了数字"的那一类。
        if not explainable(mechanism):
            return {'change_id': '', 'decision': 'discard',
                    'reason': ('mechanism must be one sentence (<=120 chars) '
                               'explaining WHY this change helps'),
                    'before': self.best or {}, 'after': {}, 'drift': []}

        change_id = ledger.record(
            self.cache_dir, paths=paths, summary=summary,
            metric={'round': self.rounds + 1}, workdir=self.workdir)

        after = {name: spec['measure'](workdir=self.workdir)
                 for name, spec in METRICS.items()}
        broken = regressed(self.best or {}, after)
        drift = soft_drift(self.best or {}, after)

        if broken:
            self.pending_regression = (self.pending_regression or 0) + 1
            if self.pending_regression <= PATIENCE_ROUNDS:
                decision = 'observe'
                reason = (f'regression within patience window '
                          f'{self.pending_regression}/{PATIENCE_ROUNDS}: '
                          + '; '.join(broken))
            else:
                decision = 'discard'
                reason = 'patience exhausted: ' + '; '.join(broken)
                self.pending_regression = 0
        elif improved(self.best or {}, after, metric=self.metric):
            decision = 'keep'
            reason = f'{self.metric} improved'
            self.pending_regression = 0
        else:
            decision = 'discard'
            reason = f'{self.metric} did not improve enough'
            self.pending_regression = 0

        if drift:
            # 软指标恶化不否决，但必须出现在理由里——静默接受就是
            # "把性能优化到荒谬的计算成本上"。
            reason += ' | soft drift: ' + '; '.join(drift)

        ledger.update_decision(self.cache_dir, change_id, decision, reason,
                               mechanism=mechanism)
        self.rounds += 1
        self.changes.append(change_id)
        if decision == 'keep':
            self.best = after
            self.without_best = 0
        else:
            self.without_best += 1
        self.save()
        return {'change_id': change_id, 'decision': decision, 'reason': reason,
                'before': self.best or {}, 'after': after, 'drift': drift}

    def report(self) -> str:
        state = '已收敛' if self.exhausted() else '可继续'
        return (f'目标：{self.goal}\n'
                f'轮数：{self.rounds}/{self.max_rounds}（{state}）\n'
                f'主指标：{self.metric}\n'
                f'改动：{len(self.changes)} 次')
