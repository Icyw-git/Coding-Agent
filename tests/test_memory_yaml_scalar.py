"""记忆写入的 frontmatter 必须能被原样读回来。

`memory_schema._yaml_scalar` 的 docstring 记着一次踩坑：描述里带 `: ` 会让 YAML 报
"mapping values are not allowed here"，**整个 frontmatter 变成空映射**，于是
type/scope/keywords 全丢、type 回落成 `fact`。

当时只堵了 `: `。实测还有六种写法同样会把 frontmatter 写坏：

    '- 像列表'     YAML 当成序列项 → 整段丢失
    '-'            同上
    '[a]'          YAML 当成 flow 序列 → 描述变成 ['a']
    '{a}'          同上（flow 映射）
    '? 键'         复杂键指示符 → 整段丢失
    ',逗号'        同上
    'a\nb'        折行 → 整段丢失
    'a\tb'        同上

真实数据里已经有一条中招的记录（`.cache/memories/feedback-resume-revision-pending.md`：
描述里带 `878× baseline: …` 之类的结构，读回来 type 是 `fact`、description 是空串，
而文件里两者都在）。
"""

import pytest

import memory_schema

BROKEN = ['- 像列表', '-', '[a]', '{a}', '? 键', ',逗号']
OK = ['-x', 'x]', '?x', 'x,', 'normal text', '2026-01-01']


@pytest.mark.parametrize('text', BROKEN)
def test_value_survives_a_round_trip(text):
    document = memory_schema.render_document({
        'name': 'probe', 'description': text, 'type': 'preference',
        'body': 'body', 'scope': 'global', 'keywords': ['k'],
    })
    meta, _ = __import__('memory')._parse_frontmatter(document)
    assert meta.get('description') == text, f'{text!r} 写坏了：{meta}'
    assert meta.get('type') == 'preference', f'{text!r} 让 type 丢了：{meta}'


def test_description_is_single_lined():
    """frontmatter 的标量字段是单行的：换行/制表符折成空格，不是写坏。

    这是 `render_frontmatter` 里 `one_line()` 的既有约定，也正因为有它，
    `_yaml_scalar` 不必处理控制字符。
    """
    document = memory_schema.render_document({
        'name': 'probe', 'description': 'a\nb\tc', 'type': 'preference',
        'body': 'body', 'scope': 'global', 'keywords': ['k'],
    })
    assert 'description: a b c' in document, document


def test_no_needless_quoting():
    """普通值不该被加引号：diff 要干净、文件要能直接读。"""
    for text in OK:
        assert memory_schema._yaml_scalar(text) == text, text
