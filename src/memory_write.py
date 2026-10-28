"""写入策略：ADD / UPDATE / SUPERSEDE。

为什么需要
----------
`remember` 原来按文件名覆盖，抽取路径按模型给的名字写。两条路都可能产生同一个
偏好的多个版本：措辞稍有不同就是两个文件，召回时两条都在（可能互相矛盾），
合并时又要靠模型重新判定。

PAHF 的解法是写入前先做 near-duplicate 检测，决定 update 还是 add。这里用确定性
规则实现同一件事，不引入向量（本地零往返是硬约束）。

三态
----
- ``ADD``      没有相近的 → 新建
- ``UPDATE``   相近（≥0.8）→ 写进已有文件，保留文件名与 name，关键词取并集
- ``SUPERSEDE``中等相近（0.5~0.8）且新的是 ``correction`` → 新建并让旧的退出召回

为什么 SUPERSEDE 只对 ``correction`` 开放
----------------------------------------
中等相似度的两条信息**可能都是有效的**（「喜欢可乐」与「困了想喝茶」）。只有
「用户明确纠正」这一种情况能确信旧的是错的。把 UPDATE 之外的相似一律当成覆盖，
会静默吃掉仍然有效的偏好。

阈值来自 PAHF 的 update-vs-add 思路；具体数值是我们自己的取舍：0.8 足够高，
避免把"同主题不同结论"合并掉。
"""
from __future__ import annotations

import memory_schema

#: 高于此值认为"同一条信息的新版本"。
UPDATE_THRESHOLD = 0.8
#: 高于此值、且新的是 correction，认为"新的修正了旧的"。
SUPERSEDE_THRESHOLD = 0.5

ADD = 'add'
UPDATE = 'update'
SUPERSEDE = 'supersede'


def decide(new: dict, existing: list[dict],
           supersedes: str = '') -> tuple[str, dict | None]:
    """决定怎么写入。返回 ``(action, target)``；target 为被命中的已有条目。

    ``supersedes`` 给了就**直接采信**：那是抽取模型看着现有记忆清单点名的
    "我修正了这一条"，比字面相似度可靠得多——一条修正和它纠正的错误内容，措辞
    本来就会变，字面相似度往往不高（实测：`deploy by running script.sh` 与其修正
    只到 0.44）。
    """
    if supersedes:
        wanted = str(supersedes).strip().casefold()
        for item in existing:
            if wanted in (str(item.get('name') or '').casefold(),
                          str(item.get('filename') or '').casefold()):
                return SUPERSEDE, item
        # 点名了但找不到：退化为新建，不把整次写入报错。
        # 旧条目可能刚被合并掉（名字变了），这不是写入方的错误。

    best: dict | None = None
    best_score = 0.0
    for item in existing:
        score = memory_schema.similarity(new, item)
        if score > best_score:
            best, best_score = item, score
    if best is None:
        return ADD, None

    # 同名就是同一个文件（name → filename），必须走 UPDATE。
    # 否则 ADD 分支会直接覆写那个文件，把旧的关键词/置信度/来源丢掉——
    # 比 UPDATE 更糟，因为它看起来像"新建成功"。
    if _same_name(new, best):
        return UPDATE, best
    # 名字不同时要求描述完全一致才合并：短模板的二元组重叠会把相似度推虚高。
    if best_score >= UPDATE_THRESHOLD and _same_description(new, best):
        return UPDATE, best
    if best_score >= SUPERSEDE_THRESHOLD and new.get('type') == 'correction':
        return SUPERSEDE, best
    return ADD, None


def _same_name(left: dict, right: dict) -> bool:
    left_name = str(left.get('name') or '').strip().casefold()
    right_name = str(right.get('name') or '').strip().casefold()
    return bool(left_name) and left_name == right_name


def _same_description(left: dict, right: dict) -> bool:
    """描述（去空白后）是否完全相同。

    为什么需要这条硬证据：短中文模板的二元组重叠会把 Jaccard 推得很高——
    「甲方主题 的部署说明」与「乙方主题 的部署说明」实测能到 0.8+。
    误合并是**不可逆的**（信息被吞掉），而漏合并只是留一条重复，合并机制会处理。
    """
    left_desc = ' '.join(str(left.get('description') or '').split()).casefold()
    right_desc = ' '.join(str(right.get('description') or '').split()).casefold()
    return bool(left_desc) and left_desc == right_desc


def merge(new: dict, old: dict) -> dict:
    """把新信息并进旧条目（UPDATE 用）。返回完整条目（含 body）。

    保留旧的 ``name``/``filename``：文件名是引用标识，改了就变成两条。其余字段
    取"更新的那个"，关键词取并集——旧关键词可能仍然有效，丢掉会让原本能召回的
    场景失效。
    """
    keywords = memory_schema.normalize_keywords(
        list(old.get('keywords') or []) + list(new.get('keywords') or []))
    return {
        'filename': old.get('filename') or new.get('filename'),
        'name': old.get('name') or new.get('name'),
        'description': new.get('description') or old.get('description'),
        'type': new.get('type') or old.get('type'),
        'scope': new.get('scope') or old.get('scope'),
        'keywords': keywords,
        'confidence': max(memory_schema.normalize_confidence(old.get('confidence')),
                          memory_schema.normalize_confidence(new.get('confidence'))),
        'last_update_date': new.get('last_update_date'),
        'supersedes': old.get('supersedes') or '',
        'superseded_by': '',
        'source_session': new.get('source_session') or old.get('source_session'),
        'source_quote': new.get('source_quote') or old.get('source_quote'),
        'body': new.get('body') or old.get('body') or '',
    }
