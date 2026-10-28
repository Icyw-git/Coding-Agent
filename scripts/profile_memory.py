"""记忆与自我改进路径的 profile：时间 + 内存 + 规模曲线。

用法::

    python scripts/profile_memory.py            # 默认规模
    python scripts/profile_memory.py 200 2000   # 指定 [条数] [长会话消息数]
    python scripts/profile_memory.py --json     # 机器可读

回答四个问题
------------
1. 记忆相关操作**各自多快**（召回、注入、写入、老化、判官信号）。
2. **增长曲线**：记忆从 10 条涨到 N 条时，召回与索引重建怎么变。
3. **内存占用**：常驻内存里被记忆缓存占了多少。
4. **注入 token 成本**：召回块相对全量注入省了多少。

为什么单独一个脚本而不是塞进 pytest
------------------------------------
pytest 跑的是"行为对不对"，不是"快不快、占多少"。把性能断言写进单测会让测试
在慢机器上误报，而完全不做又会让回归悄悄发生。脚本给出数字，人看趋势。

与既有 bench 的关系
-------------------
``bench_history`` / ``profile_turn_pipeline`` 管回合主链路；这个管本轮新增的
记忆与自改进路径（schema v2 字段、写入策略、durability、低信号剪枝、判官信号）。
两者不重叠。
"""
from __future__ import annotations

import gc
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import harness  # noqa: F401,E402  (导入期 load_dotenv + 配置)
import memory  # noqa: E402
import memory_aging  # noqa: E402
import memory_durability  # noqa: E402
import memory_feedback  # noqa: E402
import memory_schema  # noqa: E402

#: 造数据用的模板：混合类型、混合主题，长度接近真实记忆。
TEMPLATES = [
    ('preference', 'prefers brief answers without preamble'),
    ('preference', 'when sleepy: prefers tea over coffee'),
    ('preference', '回答要简短，不要开场白'),
    ('correction', 'commit messages use imperative mood, not past tense'),
    ('fact', 'production deploys go through make ship-prod'),
    ('fact', 'staging deploys use make ship-staging'),
    ('fact', 'run tests with python -m pytest -m not-slow'),
    ('fact', '部署走 make ship-prod，回滚用 make rollback'),
    ('context', 'src holds runtime modules, tests mirrors them'),
    ('context', 'API keys rotate every 90 days via secure_credentials'),
    ('context', '密钥每 90 天通过 secure_credentials 轮换'),
    ('fact', 'the workspace cache lives under .cache/memories'),
]

QUERIES = [
    'production 部署怎么发？',
    '跑一下测试',
    '回答简短点',
    '我困了想喝点东西',
    'commit message 怎么写',
    '密钥多久轮换一次',
    'src 目录放什么',
]


def _timeit(func, repeat: int = 20) -> tuple[float, float]:
    """返回 (中位数 ms, 单次最大 ms)。跑前先 gc，避免上一段的碎片计入。"""
    gc.collect()
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return samples[len(samples) // 2], samples[-1]


def _seed(memory_dir: Path, count: int) -> None:
    """写入 count 条记忆。用写盘路径（走真实脱敏/去重），不直接写文件。"""
    for index in range(count):
        mem_type, description = TEMPLATES[index % len(TEMPLATES)]
        # 加序号避免写入策略把它们合并掉（描述相同会被判 UPDATE）
        memory.write_memory_file(f'note-{index}', mem_type,
                                 f'{description} #{index}', 'body line one\nbody line two',
                                 keywords=f'{description.split()[0]},k{index}')


def profile_basic(memory_dir: Path) -> dict:
    """单次操作耗时（小规模，看绝对开销）。"""
    out: dict[str, dict] = {}

    out['write'] = _times(lambda: memory.write_memory_file(
        f'probe-{time.perf_counter_ns()}', 'fact', 'probe description', 'body'))

    out['list'] = _times(memory.list_memory_files)

    out['recall_local'] = _times(
        lambda: memory.local_relevant_memories(
            [{'role': 'user', 'content': 'production 部署怎么发？'}]))

    out['inject'] = _times(
        lambda: memory.inject_memories(
            [{'role': 'user', 'content': 'hi'}], '<relevant_memories>x</relevant_memories>'))

    out['schema_similarity'] = _times(lambda: memory_schema.similarity(
        {'name': 'a', 'description': 'deploy via make', 'keywords': ['deploy', 'make']},
        {'name': 'b', 'description': 'deploy via make', 'keywords': ['deploy', 'make']}))

    out['durability_classify'] = _times(
        lambda: memory_durability.classify({'description': '这次旅行不想托运行李'}))

    out['feedback_collect'] = _times(lambda: memory_feedback.collect(_dialogue()))

    out['low_signal_scan'] = _times(
        lambda: memory_aging.low_signal_entries(memory_dir, memory.list_memory_files()))

    out['age_scan'] = _times(
        lambda: memory_aging.stale_entries(memory_dir, memory.list_memory_files()))
    return out


def _times(func) -> dict:
    median, worst = _timeit(func)
    return {'median_ms': round(median, 3), 'max_ms': round(worst, 3)}


def _dialogue() -> list[dict]:
    return [
        {'role': 'user', 'content': '帮我把报告整理一下'},
        {'role': 'assistant', 'content': '好的',
         'tool_calls': [{'function': {'name': 'write',
                                      'arguments': '{"path":"r.md","content":"x"}'}}]},
        {'role': 'tool', 'content': 'Wrote r.md'},
        {'role': 'user', 'content': '不对，我要的是按周分组'},
    ]


def profile_scaling(sizes: list[int]) -> list[dict]:
    """增长曲线：记忆条数对召回/索引重建的影响。"""
    rows = []
    for count in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / 'memories'
            memory_dir.mkdir()
            saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
            harness.MEMORY_DIR = memory_dir
            harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
            memory._cache_clear()
            try:
                _seed(memory_dir, count)
                q = [{'role': 'user', 'content': 'production 部署怎么发？'}]
                recall_ms, recall_worst = _timeit(
                    lambda query=q: memory.local_relevant_memories(query))
                # 热召回：缓存填满后再测，把磁盘成本与算法成本分开。
                # 两者差一个数量级：冷读是磁盘绑定的，热读才是真实算法开销。
                memory.list_memory_files()
                recall_warm, _ = _timeit(
                    lambda query=q: memory.local_relevant_memories(query), repeat=10)
                rebuild_ms, _ = _timeit(memory._rebuild_index, repeat=5)
                list_ms, _ = _timeit(memory.list_memory_files)
                block = memory.load_memories(
                    [{'role': 'user', 'content': 'production 部署怎么发？'}])
                rows.append({
                    'entries': count,
                    'recall_ms': round(recall_ms, 3),
                    'recall_max_ms': round(recall_worst, 3),
                    'recall_warm_ms': round(recall_warm, 3),
                    'rebuild_index_ms': round(rebuild_ms, 3),
                    'list_ms': round(list_ms, 3),
                    'injected_chars': len(block),
                })
            finally:
                harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
                memory._cache_clear()
    return rows


def profile_write_scaling(sizes: list[int]) -> list[dict]:
    """写入成本的规模曲线。

    如实报告一个已知特征：``write_memory_file`` 每次都会重建整个 MEMORY.md 索引，
    所以单次写入是 O(n)、批量写入是 O(n²)。缓存修好之后 500 条时单次约 20ms，
    对个人使用（几十条）没有影响；上到几百条时需要改成增量更新。
    """
    rows = []
    for count in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / 'memories'
            memory_dir.mkdir()
            saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
            harness.MEMORY_DIR = memory_dir
            harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
            memory._cache_clear()
            try:
                _seed(memory_dir, count)
                median, worst = _timeit(
                    lambda: memory.write_memory_file(
                        f'probe-{time.perf_counter_ns()}', 'fact',
                        'probe description', 'body'),
                    repeat=5)
                rows.append({'entries': count,
                             'write_ms': round(median, 2),
                             'write_max_ms': round(worst, 2)})
            finally:
                harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
                memory._cache_clear()
    return rows


def profile_injection_savings(sizes: list[int]) -> list[dict]:
    """注入成本：召回块 vs 全量注入。"""
    rows = []
    for count in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / 'memories'
            memory_dir.mkdir()
            saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
            harness.MEMORY_DIR = memory_dir
            harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
            memory._cache_clear()
            try:
                _seed(memory_dir, count)
                block = memory.load_memories(
                    [{'role': 'user', 'content': 'production 部署怎么发？'}])
                full = '\n\n'.join(
                    memory.read_memory_file(item['filename']) or ''
                    for item in memory.list_memory_files())
                rows.append({
                    'entries': count,
                    'recalled_chars': len(block),
                    'full_chars': len(full),
                    'saved_pct': (round((1 - len(block) / len(full)) * 100)
                                  if full else None),
                })
            finally:
                harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
                memory._cache_clear()
    return rows


def profile_memory_footprint(count: int) -> dict:
    """常驻内存：解析缓存占多少（tracemalloc 只算 Python 对象）。"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / 'memories'
        memory_dir.mkdir()
        saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
        harness.MEMORY_DIR = memory_dir
        harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
        memory._cache_clear()
        try:
            _seed(memory_dir, count)
            gc.collect()
            tracemalloc.start()
            base = tracemalloc.get_traced_memory()[0]
            memory._cache_clear()
            for _ in range(3):
                memory.list_memory_files()      # 填充 _FRONTMATTER_CACHE
            peak, _ = tracemalloc.get_traced_memory()
            gc.collect()
            settled = tracemalloc.get_traced_memory()[0]
            tracemalloc.stop()
            return {
                'entries': count,
                'cache_kb': round((settled - base) / 1024, 1),
                'peak_kb': round((peak - base) / 1024, 1),
                'per_entry_bytes': (round((settled - base) / max(1, count))
                                    if count else 0),
            }
        finally:
            harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
            memory._cache_clear()


#: 召回质量的具名样本：名字/描述与 QUERY_EXPECT 对应。
QUALITY_STORE = [
    ('deploy-prod', 'fact', 'production deploys go through make ship-prod'),
    ('deploy-staging', 'fact', 'staging deploys use make ship-staging'),
    ('test-cmd', 'fact', 'run tests with python -m pytest -m not-slow'),
    ('pref-brief', 'preference', 'prefers brief answers without preamble'),
    ('pref-tabs', 'preference', 'prefers tabs over spaces in code'),
    ('sleepy-tea', 'preference', 'when sleepy: prefers tea over coffee'),
    ('repo-layout', 'context', 'src holds runtime modules, tests mirrors them'),
]

#: 查询 → 期望命中的条目名。
QUERY_EXPECT = {
    'production 部署怎么发？': 'deploy-prod',
    'staging 怎么发': 'deploy-staging',
    '跑一下测试': 'test-cmd',
    '回答简短点': 'pref-brief',
    '代码缩进用 tab 还是空格': 'pref-tabs',
    '我困了想喝点东西': 'sleepy-tea',
    'src 目录放什么': 'repo-layout',
}


def profile_recall_quality() -> dict:
    """召回质量：能不能召回对的、能不能排第一。

    只报数字不断言——质量门在 ``tests/test_memory_benchmark.py``（那里会用
    术语表覆盖内的样本卡住回归）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / 'memories'
        memory_dir.mkdir()
        saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
        harness.MEMORY_DIR = memory_dir
        harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
        memory._cache_clear()
        try:
            for name, mem_type, description in QUALITY_STORE:
                front = [
                    '---',
                    f'name: {name}',
                    f'description: {description}',
                    f'type: {mem_type}',
                    'confidence: 0.70',
                    'last_update_date: 2026-10-28',
                    '---',
                    '',
                    description,
                    '',
                ]
                (memory_dir / f'{name}.md').write_text(chr(10).join(front), encoding='utf-8')
            memory._cache_clear()
            recalled = first = 0
            details = []
            for query, want in QUERY_EXPECT.items():
                got = [name.replace('.md', '') for name in
                       memory.local_relevant_memories(
                           [{'role': 'user', 'content': query}], max_items=3)]
                hit = want in got
                recalled += hit
                first += bool(got) and got[0] == want
                details.append({'query': query, 'want': want, 'got': got})
            return {'queries': len(QUERY_EXPECT), 'recalled': recalled,
                    'first_hit': first, 'details': details}
        finally:
            harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
            memory._cache_clear()


def main() -> int:
    as_json = '--json' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        basic_count = 30
        scaling = [10, 50, 200, 500]
    else:
        basic_count = int(args[0])
        scaling = [10, 50, 200, basic_count] if basic_count >= 200 else [10, 50, basic_count]
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / 'memories'
        memory_dir.mkdir()
        saved = (harness.MEMORY_DIR, harness.MEMORY_INDEX)
        harness.MEMORY_DIR = memory_dir
        harness.MEMORY_INDEX = memory_dir / 'MEMORY.md'
        memory._cache_clear()
        try:
            _seed(memory_dir, basic_count)
            basic = profile_basic(memory_dir)
            quality = profile_recall_quality()
        finally:
            harness.MEMORY_DIR, harness.MEMORY_INDEX = saved
            memory._cache_clear()

    report = {
        'env': {'python': sys.version.split()[0], 'entries': basic_count},
        'basic_ms': basic,
        'recall_quality': quality,
        'scaling': profile_scaling(scaling),
        'injection': profile_injection_savings(scaling),
        'write_scaling': profile_write_scaling(scaling),
        'footprint': profile_memory_footprint(basic_count),
    }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"== 单次操作（{basic_count} 条记忆）==")
    for name, value in report['basic_ms'].items():
        print(f"  {name:22} {value['median_ms']:>8.3f} ms   峰值 {value['max_ms']:>7.3f} ms")
    q = report['recall_quality']
    print(f"\n== 召回质量 ==  {q['recalled']}/{q['queries']} 召回，"
          f"{q['first_hit']}/{q['queries']} 首位命中")
    print('\n== 增长曲线 ==')
    print(f"  {'条数':>6} {'召回冷ms':>9} {'召回热ms':>9} {'重建ms':>8} {'注入字符':>9}")
    for row in report['scaling']:
        print(f"  {row['entries']:>6} {row['recall_ms']:>9.3f} "
              f"{row.get('recall_warm_ms', 0):>9.3f} "
              f"{row['rebuild_index_ms']:>8.3f} {row['injected_chars']:>9}")
    print('\n== 注入成本 ==')
    print(f"  {'条数':>6} {'召回字符':>10} {'全量字符':>10} {'省下':>7}")
    for row in report['injection']:
        saved = f"{row['saved_pct']}%" if row['saved_pct'] is not None else '—'
        print(f"  {row['entries']:>6} {row['recalled_chars']:>10} "
              f"{row['full_chars']:>10} {saved:>7}")
    print()
    print('== 写入成本（每次重建索引，O(n)）==')
    print(f"  {'条数':>6} {'单次 ms':>9} {'峰值 ms':>9}")
    for row in report['write_scaling']:
        print(f"  {row['entries']:>6} {row['write_ms']:>9.2f} {row['write_max_ms']:>9.2f}")
    f = report['footprint']
    print(f"\n== 常驻内存 ==  {f['entries']} 条 -> 解析缓存 "
          f"{f['cache_kb']} KB（峰值 {f['peak_kb']} KB，"
          f"约 {f['per_entry_bytes']} B/条）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
