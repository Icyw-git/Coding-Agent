"""消融实验：关掉每个机制，看它到底带来了什么。

用法::

    python scripts/ablate_memory.py
    python scripts/ablate_memory.py --json

为什么做消融
------------
profile 只能回答"快不快、占多少"，回答不了"有没有用"。消融是**关掉一个机制、
其余不变**，用同一批输入比结果——差值就是这个机制的净贡献。

五组对照
--------
| 组 | 关掉什么 | 看什么 |
|---|---|---|
| 召回扩展 | 中英术语表 | 中文查询对英文记忆的命中率 |
| 写入去重（A4） | UPDATE/SUPERSEDE 判定 | 同一偏好的多个措辞会留下几条 |
| 两层记忆（A5） | durability 判定 | 一次性上下文有多少漏进长期层 |
| 低信号剪枝（A6） | 低信号淘汰 | 90 天后库里剩多少噪音 |
| 耐心窗口（RSI） | observe 状态 | 先退后进的轨迹有多少被判成改进 |

**这只是机制级消融，不是用户价值消融。** 它证明"关掉这个机制，行为会变成 X"，
不证明"用户因此受益"。后者需要真实使用数据或已校准的 LLM 判官
（`scripts/calibrate_memory_judge.py`，κ ≥ 0.41 才算数）。这个区别很重要——
把机制级结果说成用户价值，就是本文档一直在防的那种自欺。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import os  # noqa: E402

import harness  # noqa: F401,E402
import memory  # noqa: E402
import memory_aging  # noqa: E402
import memory_durability  # noqa: E402
import memory_write  # noqa: E402
import self_improve  # noqa: E402


class Store:
    """临时记忆库上下文：把 harness 的 MEMORY_DIR 指到沙箱。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / 'memories'
        self.dir.mkdir()
        self._saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
        harness.MEMORY_DIR = self.dir
        harness.MEMORY_INDEX = self.dir / 'MEMORY.md'
        memory._cache_clear()
        return self

    def __exit__(self, *exc):
        harness.MEMORY_DIR, harness.MEMORY_INDEX = self._saved
        memory._cache_clear()
        self._tmp.cleanup()
        return False

    def entries(self) -> list[dict]:
        return memory.list_memory_files()

    def live_files(self) -> int:
        return len([p for p in self.dir.glob('*.md') if p.name != 'MEMORY.md'])


# ---------------------------------------------------------------- 1. 召回扩展

#: 英文记忆 + 中文查询（跨语言，术语表就是为了这个）。
CROSS_LINGUAL = [
    ('deploy-prod', 'fact', 'production deploys go through make ship-prod',
     'production 部署怎么发？'),
    ('test-cmd', 'fact', 'run tests with python -m pytest -m not-slow', '跑一下测试'),
    ('pref-brief', 'preference', 'prefers brief answers without preamble', '回答简短点'),
    ('pref-chinese', 'preference', 'prefers replies in Chinese', '用中文回复我'),
    ('sleepy-tea', 'preference', 'when sleepy: prefers tea over coffee', '我困了想喝点什么'),
    ('api-key', 'context', 'API keys rotate every 90 days', '密钥多久轮换一次'),
    ('repo-layout', 'context', 'src holds runtime modules', 'src 目录放什么'),
    ('commit-style', 'correction', 'commit messages use imperative mood', 'commit message 怎么写'),
]


def ablation_recall(*, glossary: bool) -> dict:
    """关掉中英术语表，看中文查询还能召回到几条英文记忆。"""
    original = memory._expand_tokens
    if not glossary:
        memory._expand_tokens = lambda tokens: set(tokens)
    try:
        with Store() as store:
            for name, mem_type, description, _query in CROSS_LINGUAL:
                (store.dir / f'{name}.md').write_text(
                    '\n'.join([
                        '---',
                        f'name: {name}',
                        f'description: {description}',
                        f'type: {mem_type}',
                        'confidence: 0.70',
                        'last_update_date: 2026-10-28',
                        '---', '', description, '',
                    ]), encoding='utf-8')
            memory._cache_clear()
            hits = first = 0
            for name, _t, _d, query in CROSS_LINGUAL:
                got = [x.replace('.md', '') for x in
                       memory.local_relevant_memories(
                           [{'role': 'user', 'content': query}], max_items=3)]
                hits += name in got
                first += bool(got) and got[0] == name
            return {'queries': len(CROSS_LINGUAL), 'recalled': hits,
                    'first_hit': first}
    finally:
        memory._expand_tokens = original


# ---------------------------------------------------------------- 2. 写入去重（A4）

#: 同一偏好的五种措辞——真实使用里模型每次的说法都不一样。
PARAPHRASES = [
    ('deploy', 'fact', 'production deploys go through make ship-prod'),
    ('deploy-v2', 'fact', 'production deploys go through make ship-prod'),
    ('deploy-note', 'fact', 'production deploys go through make ship-prod'),
    ('deploy-again', 'fact', 'production deploys go through make ship-prod'),
    ('deploy-final', 'fact', 'production deploys go through make ship-prod'),
]


def ablation_dedup(*, policy: bool) -> dict:
    """关掉写入判定，同一偏好的 N 种措辞会留下几条。"""
    original = memory_write.decide
    if not policy:
        memory_write.decide = lambda new, existing, supersedes='': (
            memory_write.ADD, None)
    try:
        with Store() as store:
            for name, mem_type, description in PARAPHRASES:
                memory.write_memory_file(name, mem_type, description, 'body')
            return {'wrote': len(PARAPHRASES), 'files': store.live_files(),
                    'recallable': len(store.entries())}
    finally:
        memory_write.decide = original


# ---------------------------------------------------------------- 3. 两层记忆（A5）

#: 一半一次性上下文，一半长期偏好。
MIXED_SCOPE = [
    ('trip', 'context', '这次旅行不想托运行李'),
    ('meeting', 'context', '本次会议先不讨论预算'),
    ('tempdir', 'context', '暂时用这个临时目录'),
    ('pref-pnpm', 'preference', '以后都用 pnpm 装依赖'),
    ('pref-tabs', 'preference', '总是用 tab 缩进'),
    ('pref-brief', 'preference', '平时回答简短一点'),
]


def ablation_durability(*, classify: bool) -> dict:
    """关掉 durability 判定，一次性上下文有多少漏进长期层。"""
    original = memory_durability.apply
    if not classify:
        memory_durability.apply = lambda entry: (
            entry if entry.get('scope') else {**entry, 'scope': 'global'})
    try:
        with Store() as store:
            for name, mem_type, description in MIXED_SCOPE:
                memory.write_memory_file(name, mem_type, description, 'body')
            entries = store.entries()
            leaked = [item['name'] for item in entries
                      if item['scope'] == 'global'
                      and item['name'] in {'trip', 'meeting', 'tempdir'}]
            return {'wrote': len(MIXED_SCOPE), 'one_offs': 3,
                    'leaked_to_global': len(leaked), 'leaked': leaked}
    finally:
        memory_durability.apply = original


# ---------------------------------------------------------------- 4. 低信号剪枝（A6）

def _seed_noise(store: Store, count: int) -> None:
    """造 count 条"模糊、低置信、从未召回"的噪音记忆。

    mtime 拨到 45 天前：``age_days`` 取 mtime 与召回记录的较大者，今天刚写的
    文件 age=0 两个分支都不剪；拨到 120 天又会被**时间老化**（90 天）先抓走，
    看不出低信号剪枝本身的贡献。45 天恰好落在两者之间：低信号（30 天）命中、
    时间老化（90 天）不命中。
    """
    old = time.time() - 45 * 86400
    for index in range(count):
        path = store.dir / f'noise{index}.md'
        path.write_text(
            '\n'.join([
                '---',
                f'name: noise{index}',
                f'description: vague note {index}',
                'type: fact',
                'scope: global',
                'confidence: 0.10',
                'last_update_date: 2026-01-01',
                '---', '', 'vague body', '',
            ]), encoding='utf-8')
        os.utime(path, (old, old))
    memory._cache_clear()


def ablation_pruning(*, prune: bool) -> dict:
    """关掉低信号剪枝，90 天后库里还剩多少噪音。"""
    original = memory_aging.low_signal_entries
    if not prune:
        memory_aging.low_signal_entries = lambda *_a, **_k: []
    try:
        with Store() as store:
            _seed_noise(store, 20)
            memory.write_memory_file('pref-real', 'preference',
                                     '以后都用 pnpm 装依赖', 'body')
            moved = memory.age_memories(days=90)
            return {'seeded_noise': 20, 'pruned': len(moved),
                    'remaining': store.live_files()}
    finally:
        memory_aging.low_signal_entries = original


# ---------------------------------------------------------------- 5. 耐心窗口（RSI）

def _simulate_improvement(*, patience: int) -> dict:
    """模拟一条「先退后进」的轨迹，看它能不能被保留。

    Gödel Agent 实测：92% 的优化在变好之前必然先变差。这里把那条轨迹最小化：
    基线 5 → 回归到 8 → 恢复到 2（真改进）。
    """
    # 回归持续 3 轮才转好。CONVERGENCE_ROUNDS 也是 3——所以在没有耐心窗口时，
    # 会话在"还没变好"的阶段就已经收敛，永远走不到那个真正的改进。
    sequence = [
        {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 5}},
        {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 8}},
        {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 8}},
        {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 8}},
        {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 2}},
    ]
    originals = {name: spec['measure'] for name, spec in self_improve.METRICS.items()}
    original_patience = self_improve.PATIENCE_ROUNDS
    state = {'i': 0}

    def make(name):
        def measure(*, workdir):
            return sequence[min(state['i'], len(sequence) - 1)].get(name, {})
        return measure

    try:
        for name in self_improve.METRICS:
            self_improve.METRICS[name]['measure'] = make(name)
        self_improve.PATIENCE_ROUNDS = patience
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'skills').mkdir()
            (Path(tmp) / 'skills' / 'a.md').write_text('v1', encoding='utf-8')
            session = self_improve.Session(cache_dir=tmp, workdir=tmp,
                                           goal='降 lint', max_rounds=5)
            state['i'] = 0
            session.baseline()
            decisions = []
            for index in (1, 2, 3, 4):
                # 收敛即停：真实的循环不会在 exhausted 之后继续跑。
                if session.exhausted():
                    break
                state['i'] = index
                result = session.evaluate(['skills/a.md'], summary='s',
                                           mechanism='合并重复段落')
                decisions.append(result['decision'])
            return {'decisions': decisions,
                    'rounds_run': len(decisions),
                    'kept': decisions.count('keep'),
                    'survived': 'keep' in decisions}
    finally:
        for name, measure in originals.items():
            self_improve.METRICS[name]['measure'] = measure
        self_improve.PATIENCE_ROUNDS = original_patience


# ---------------------------------------------------------------- 主流程

def main() -> int:
    as_json = '--json' in sys.argv
    started = time.perf_counter()

    groups = [
        {
            'name': '召回扩展（中英术语表）',
            'question': '中文查询能召回几条英文记忆',
            'off': ablation_recall(glossary=False),
            'on': ablation_recall(glossary=True),
            'metric': 'recalled',
            'total': len(CROSS_LINGUAL),
        },
        {
            'name': '写入去重（A4）',
            'question': '同一偏好的 5 种措辞留下几个文件',
            'off': ablation_dedup(policy=False),
            'on': ablation_dedup(policy=True),
            'metric': 'files',
            'total': len(PARAPHRASES),
            'lower_is_better': True,
        },
        {
            'name': '两层记忆（A5）',
            'question': '一次性上下文有几条漏进长期层',
            'off': ablation_durability(classify=False),
            'on': ablation_durability(classify=True),
            'metric': 'leaked_to_global',
            'total': 3,
            'lower_is_better': True,
        },
        {
            'name': '低信号剪枝（A6）',
            'question': '90 天后库里还剩多少文件（含 20 条噪音）',
            'off': ablation_pruning(prune=False),
            'on': ablation_pruning(prune=True),
            'metric': 'remaining',
            'total': 21,
            'lower_is_better': True,
        },
        {
            'name': '耐心窗口（RSI）',
            'question': '先退后进的轨迹能否被判为改进',
            'off': _simulate_improvement(patience=0),
            'on': _simulate_improvement(patience=2),
            'metric': 'kept',
            'total': 1,
        },
    ]

    if as_json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
        return 0

    print('== 消融实验：关掉机制 vs 开启机制 ==\n')
    for group in groups:
        off_value = group['off'][group['metric']]
        on_value = group['on'][group['metric']]
        better = 'lower' if group.get('lower_is_better') else 'higher'
        arrow = ''
        if off_value != on_value:
            improved = (on_value < off_value) if better == 'lower' else (on_value > off_value)
            arrow = '  [OK] 机制有效' if improved else '  [!!] 反向'
        print(f"{group['name']}")
        print(f"  问：{group['question']}")
        print(f"  关掉 → {off_value}/{group['total']}")
        print(f"  开启 → {on_value}/{group['total']}{arrow}")
        print()

    print('注：这是**机制级**消融（关掉一个机制、同一批输入、比结果），')
    print('    不是用户价值消融。要证明"用户因此受益"，需要已校准的 LLM 判官')
    print('    （scripts/calibrate_memory_judge.py，κ ≥ 0.41）或真实使用数据。')
    print(f'\n耗时 {time.perf_counter() - started:.1f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
