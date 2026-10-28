"""记忆 schema v2：字段归一、渲染、相似度。

这一层的存在理由是「字段规则只写一次」：抽取写入、合并校验、索引重建、
召回打分、`/memories` 展示五处都要用同一套规则，散着写必然漂移。
"""
from __future__ import annotations

import pytest

import memory_schema as ms

# ---------------------------------------------------------------- 类型归一

@pytest.mark.parametrize('legacy,canonical', [
    ('user', 'preference'),
    ('feedback', 'correction'),
    ('project', 'fact'),
    ('reference', 'context'),
])
def test_legacy_types_map(legacy, canonical):
    assert ms.normalize_type(legacy) == canonical
    assert ms.canonical_type(legacy) == canonical


@pytest.mark.parametrize('mem_type', ms.TYPES)
def test_canonical_types_pass_through(mem_type):
    assert ms.canonical_type(mem_type) == mem_type


def test_unknown_type_is_lenient_on_read():
    """读取路径宽容：一个字段写坏不该让整批记忆不可读。"""
    assert ms.normalize_type('nonsense') == 'fact'


def test_unknown_type_is_strict_on_write():
    """合并会删旧文件，格式错误的输出必须整批拒绝。"""
    with pytest.raises(ValueError, match='invalid memory type'):
        ms.canonical_type('nonsense', strict=True)


def test_empty_type_defaults():
    assert ms.canonical_type('') == 'fact'


# ---------------------------------------------------------------- 关键词

def test_keywords_from_string():
    assert ms.normalize_keywords('a, b;c、d') == ['a', 'b', 'c', 'd']


def test_keywords_dedupe_case_insensitive():
    assert ms.normalize_keywords(['Tea', 'tea', 'TEA']) == ['Tea']


def test_keywords_are_capped():
    words = [f'w{i}' for i in range(50)]
    assert len(ms.normalize_keywords(words)) == ms.MAX_KEYWORDS


def test_keyword_length_capped():
    out = ms.normalize_keywords(['x' * 100])
    assert len(out[0]) == ms.MAX_KEYWORD_CHARS


def test_keywords_empty_inputs():
    assert ms.normalize_keywords(None) == []
    assert ms.normalize_keywords('') == []
    assert ms.normalize_keywords([]) == []


def test_derive_keywords_finds_condition_words():
    """条件偏好的关键词在条件词上——这是它能被召回的前提。"""
    words = ms.derive_keywords('when sleepy: prefers tea')
    assert any('sleepy' in w for w in words)


# ---------------------------------------------------------------- 数值/日期

@pytest.mark.parametrize('raw,expect', [
    (0.5, 0.5), ('0.8', 0.8), (2, 1.0), (-1, 0.0), ('abc', ms.DEFAULT_CONFIDENCE),
    (None, ms.DEFAULT_CONFIDENCE), (float('nan'), ms.DEFAULT_CONFIDENCE),
])
def test_confidence_is_clamped(raw, expect):
    assert ms.normalize_confidence(raw) == expect


def test_date_normalization():
    assert ms.normalize_date('2026-10-28') == '2026-10-28'
    assert ms.normalize_date('2026-10-28T10:00:00') == '2026-10-28'
    assert ms.normalize_date('not a date') == ''
    assert ms.normalize_date('') == ''


def test_scope_normalization():
    assert ms.normalize_scope('session') == 'session'
    assert ms.normalize_scope('GLOBAL') == 'global'
    assert ms.normalize_scope('bogus') == ms.DEFAULT_SCOPE


# ---------------------------------------------------------------- 渲染往返

@pytest.mark.parametrize('description', [
    'when sleepy: prefers tea',        # 冒号（条件偏好的标准写法）
    'plain description',
    'has #hash',
    'colon at end:',
    '  padded  ',
    'true',                            # 会被 YAML 当布尔
])
def test_render_and_parse_roundtrip(description):
    import memory
    text = memory._render_memory('x', 'preference', description, 'body')
    meta, body = memory._parse_frontmatter(text)
    entry = ms.normalize_entry(meta, body, 'x.md')
    assert entry['type'] == 'preference'
    assert entry['last_update_date'], 'frontmatter must have survived'
    assert description.split() == entry['description'].split()


def test_render_quotes_only_when_needed():
    plain = ms.render_frontmatter({'name': 'n', 'description': 'simple',
                                   'type': 'fact'})
    assert 'description: simple' in plain
    tricky = ms.render_frontmatter({'name': 'n', 'description': 'a: b',
                                    'type': 'fact'})
    assert 'description: "a: b"' in tricky


def test_new_fields_are_written():
    text = ms.render_frontmatter({
        'name': 'n', 'description': 'd', 'type': 'preference',
        'scope': 'session', 'keywords': ['a', 'b'], 'confidence': 0.9,
    })
    assert 'scope: session' in text
    assert 'keywords: a,b' in text
    assert 'confidence: 0.90' in text


def test_minimal_entry_gets_defaults():
    text = ms.render_frontmatter({'name': 'n', 'description': 'd'})
    assert 'type: fact' in text
    assert 'scope: global' in text
    assert 'confidence: 0.50' in text


# ---------------------------------------------------------------- 读取归一

def test_normalize_entry_tolerates_empty_meta():
    entry = ms.normalize_entry({}, 'body text', 'file-name.md')
    assert entry['name'] == 'file-name'
    assert entry['type'] == 'fact'
    assert entry['filename'] == 'file-name.md'


def test_normalize_entry_derives_keywords_when_missing():
    entry = ms.normalize_entry({'name': 'deploy', 'description': 'make ship-prod'},
                               '', 'deploy.md')
    assert entry['keywords'], 'should not be empty'


# ---------------------------------------------------------------- 相似度

def test_identical_entries_are_similar():
    entry = {'name': 'deploy', 'description': 'deploy via make',
             'keywords': ['deploy', 'make']}
    assert ms.similarity(entry, entry) > 0.9


def test_unrelated_entries_are_not_similar():
    left = {'name': 'deploy', 'description': 'deploy via make',
            'keywords': ['deploy', 'make']}
    right = {'name': 'seat', 'description': 'prefers aisle',
             'keywords': ['aisle', 'seat']}
    assert ms.similarity(left, right) < 0.2


def test_same_name_is_strong_signal():
    """名字相同基本就是同一条：模型起名比措辞稳定。"""
    left = {'name': 'deploy', 'description': 'a', 'keywords': ['x']}
    right = {'name': 'deploy', 'description': 'b', 'keywords': ['y']}
    assert ms.similarity(left, right) >= 0.15


def test_similarity_is_bounded():
    left = {'name': 'n', 'description': 'd', 'keywords': ['a']}
    assert 0.0 <= ms.similarity(left, left) <= 1.0
    assert 0.0 <= ms.similarity({}, {}) <= 1.0


def test_similarity_without_keywords():
    left = {'name': 'a', 'description': 'deploy make ship'}
    right = {'name': 'b', 'description': 'deploy make ship'}
    assert ms.similarity(left, right) > 0
