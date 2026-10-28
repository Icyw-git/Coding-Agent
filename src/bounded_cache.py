"""有界缓存的统一淘汰策略。

为什么需要这个模块
------------------
同一个缺陷在四个地方各写了一遍：缓存满了就 `clear()`。而**整体清空在条目数
接近上限时会变成「每次插入都清空」，缓存命中率归零**——

- `memory._FRONTMATTER_CACHE`：实测 500 条记忆时，索引重建 **6786ms → 21ms**（修正后）
- `agent_runtime._summary_cache`：超过 512 条就清空
- `net_grants._CACHE`：上限只有 8，键含 mtime，授权文件一改就换键
- `sandbox._ENV_BLOCK_CACHE`：上限 4，环境变化时同样抖动

写成一份而不是四处各修一次，是因为**这个反模式会不断被重新引入**——它看起来
像"有上限了所以安全"，实际上限越小时危害越大。

正确做法
--------
淘汰最旧的一半：
- 摊还成本 O(1)：不是每次插入都扫描
- 保留近期命中：新条目一定留下，旧的一半让位
- 上限越小时效果越明显（上限 8 时，清空意味着频繁全失效）

调用方负责持锁：这些缓存分布在不同模块，锁的粒度由各模块自己决定（有的用
threading.Lock，有的靠单线程假设）。
"""
from __future__ import annotations

from collections.abc import MutableMapping


def put(mapping: MutableMapping, key, value, max_size: int) -> None:
    """插入一条并维持上限。**淘汰最旧的一半，绝不整体清空。**

    ``max_size`` 至少按 2 处理：上限为 1 时"淘汰一半"等于清空，那就退化成
    原来的问题。上限 1 的缓存本身也没有意义。
    """
    limit = max(2, int(max_size))
    if key not in mapping and len(mapping) >= limit:
        for stale in list(mapping)[: limit // 2]:
            mapping.pop(stale, None)
    mapping[key] = value


def evicted_size(mapping: MutableMapping, max_size: int) -> int:
    """这次插入会淘汰几条（0 或一半）。诊断/测试用。"""
    limit = max(2, int(max_size))
    if len(mapping) < limit:
        return 0
    return limit // 2
