"""记忆类型的四处表必须一致，且注释不能指向不存在的常量。

`tool_memory.py` 的注释原本写着「与自动抽取的 `MEMORY_TYPES` 不同」——
但 `MEMORY_TYPES` 这个常量**在全仓库都不存在**（规范化表叫 `memory_schema.TYPES`）。
下一任读到这里会去找一个没有的东西。

四处里只有两处是该连起来的：给模型的 enum 与校验用的集合。它们一旦不一致，
模型按 schema 传了一个类型、工具却回 "type must be one of …"。
"""

import memory_schema
import tool_memory
import tool_schemas


def test_validation_uses_the_canonical_table():
    assert tool_memory.TYPES is memory_schema.TYPES


def test_schema_enum_matches_the_canonical_table():
    """模型看到 enum 就是它能传的值；它不能比校验集合窄。"""
    schema = next(item for item in tool_schemas.TOOL_SCHEMAS
                  if item['function']['name'] == 'remember')
    enum = schema['function']['parameters']['properties']['type']['enum']
    assert tuple(enum) == tuple(memory_schema.TYPES)


def test_no_comment_points_at_the_removed_constant():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / 'src' / 'tool_memory.py').read_text(encoding='utf-8')
    assert 'MEMORY_TYPES' not in source, (
        'MEMORY_TYPES 不存在；规范化表是 memory_schema.TYPES')
