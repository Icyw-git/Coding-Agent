"""有界缓存的淘汰策略。

这个模块存在的理由是同一个反模式在四个地方各写了一遍（满了就 `clear()`），
而**整体清空在条目数接近上限时会变成「每次插入都清空」**——上限越小危害越大
（`net_grants._CACHE` 上限只有 8）。
"""
from __future__ import annotations

import bounded_cache as bc


def test_put_keeps_recent_and_evicts_oldest_half():
    cache: dict = {}
    for index in range(10):
        bc.put(cache, index, index, 4)
    # 上限 4：第 5 次插入淘汰前一半，最近的留下
    assert len(cache) <= 4
    assert 9 in cache          # 最新的必须在
    assert 0 not in cache      # 最旧的被淘汰


def test_never_clears_whole_cache():
    """核心断言：超限时缓存里必须有内容残留。

    整体清空会让命中率归零；实测 500 条记忆时索引重建因此要 6.8 秒。
    """
    cache: dict = {}
    for index in range(50):
        bc.put(cache, index, index, 8)
        assert cache, f'cache was emptied at insert {index}'
    assert len(cache) <= 8


def test_overwrite_existing_key_does_not_evict():
    """更新已有的键不该触发淘汰——它不增加条目数。"""
    cache = {'a': 1, 'b': 2}
    bc.put(cache, 'a', 100, 2)
    assert cache == {'a': 100, 'b': 2}


def test_tiny_limit_still_behaves():
    """上限 1 时「淘汰一半」等于清空，所以按 2 处理。"""
    cache: dict = {}
    for index in range(5):
        bc.put(cache, index, index, 1)
        assert cache
    assert len(cache) <= 2


def test_limit_below_two_is_clamped():
    cache: dict = {}
    bc.put(cache, 'a', 1, 0)
    bc.put(cache, 'b', 2, -5)
    assert cache


def test_evicted_size_reports_plan():
    assert bc.evicted_size({}, 8) == 0
    assert bc.evicted_size({i: i for i in range(8)}, 8) == 4
    assert bc.evicted_size({i: i for i in range(2)}, 1) == 1


def test_all_call_sites_use_it():
    """四个已知调用点都必须走 bounded_cache，不能回到 clear()。"""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / 'src'
    for name in ('memory.py', 'agent_runtime.py', 'net_grants.py', 'sandbox.py'):
        text = (src / name).read_text(encoding='utf-8')
        assert 'bounded_cache.put' in text, f'{name} should use bounded_cache.put'
