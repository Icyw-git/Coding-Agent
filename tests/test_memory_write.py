"""写入策略（A4）：ADD / UPDATE / SUPERSEDE。

同一个偏好的多个措辞版本在召回时会互相矛盾，合并时又要靠模型重新判定。
写入前确定性去重比事后补救便宜。
"""
from __future__ import annotations

import pytest

import memory
import memory_write
from memory_schema import derive_keywords


def _entry(name, description, mem_type='preference', **extra):
    return {'name': name, 'description': description, 'type': mem_type,
            'keywords': derive_keywords(description), **extra}


# ---------------------------------------------------------------- decide

def test_add_when_nothing_similar():
    new = _entry('deploy', 'deploy via make ship-prod')
    action, target = memory_write.decide(new, [_entry('seat', 'prefers aisle')])
    assert action == memory_write.ADD
    assert target is None


def test_add_when_store_empty():
    assert memory_write.decide(_entry('a', 'b'), []) == (memory_write.ADD, None)


def test_update_when_description_identical():
    """同一个偏好被起了两个名字：描述相同就该合并，否则召回两条可能矛盾。"""
    new = _entry('deploy-v2', 'deploy via make ship-prod')
    old = _entry('deploy', 'deploy via make ship-prod')
    action, target = memory_write.decide(new, [old])
    assert action == memory_write.UPDATE
    assert target is old


def test_update_when_name_identical():
    new = _entry('deploy', 'deploy via make')
    old = _entry('deploy', 'deploy by hand')
    assert memory_write.decide(new, [old])[0] == memory_write.UPDATE


def test_no_merge_on_shared_template_words():
    """短中文模板的二元组重叠会把相似度虚高，必须靠主题证据拦住。

    误合并是**不可逆**的（信息被吞掉）；漏合并只是留一条重复，后续合并会处理。
    """
    new = _entry('task-b', '乙方主题 的部署说明')
    old = _entry('task-a', '甲方主题 的部署说明')
    assert memory_write.decide(new, [old])[0] == memory_write.ADD


def test_no_merge_when_names_and_descriptions_differ():
    new = _entry('n1', 'item number 1')
    old = _entry('n0', 'item number 0')
    assert memory_write.decide(new, [old])[0] == memory_write.ADD


def test_correction_supersedes_explicitly_named_entry():
    """显式点名比相似度可靠：修正和它纠正的内容措辞本来就不同。"""
    new = _entry('deploy-fix', 'deploy via make ship-prod', 'correction')
    old = _entry('deploy-script', 'deploy by running script.sh', 'fact')
    action, target = memory_write.decide(new, [old], supersedes='deploy-script')
    assert action == memory_write.SUPERSEDE
    assert target is old


def test_supersedes_accepts_filename():
    old = _entry('deploy-script', 'x')
    old['filename'] = 'deploy-script.md'
    new = _entry('fix', 'y', 'correction')
    assert memory_write.decide(new, [old], supersedes='deploy-script.md')[0] \
        == memory_write.SUPERSEDE


def test_unknown_supersedes_falls_back_to_add():
    """点名了但找不到：旧条目可能刚被合并掉，不该让整次写入失败。"""
    new = _entry('fix', 'y', 'correction')
    action, _target = memory_write.decide(new, [_entry('other', 'z')],
                                          supersedes='ghost')
    assert action == memory_write.ADD


def test_non_correction_does_not_supersede_by_similarity():
    """中等相似但类型不是 correction：两条可能都有效，不能覆盖。"""
    new = _entry('deploy-2', 'deploy via make ship-prod', 'preference')
    old = _entry('deploy', 'deploy via make ship-prod now', 'preference')
    action, _target = memory_write.decide(new, [old])
    assert action in (memory_write.ADD, memory_write.UPDATE)


# ---------------------------------------------------------------- merge

def test_merge_keeps_identity_and_unions_keywords():
    new = _entry('deploy-v2', 'deploy via make', keywords=['deploy', 'make'])
    old = _entry('deploy', 'deploy via make', keywords=['deploy', 'ship-prod'])
    merged = memory_write.merge(new, old)
    assert merged['name'] == 'deploy'          # 保留旧名（文件名是引用标识）
    assert set(merged['keywords']) == {'deploy', 'make', 'ship-prod'}


def test_merge_takes_newer_description_and_body():
    new = {**_entry('a', 'new desc'), 'body': 'new body'}
    old = {**_entry('a', 'old desc'), 'body': 'old body'}
    merged = memory_write.merge(new, old)
    assert merged['description'] == 'new desc'
    assert merged['body'] == 'new body'


def test_merge_keeps_higher_confidence():
    new = {**_entry('a', 'd'), 'confidence': 0.3}
    old = {**_entry('a', 'd'), 'confidence': 0.9}
    assert memory_write.merge(new, old)['confidence'] == 0.9


# ---------------------------------------------------------------- 接入 memory

@pytest.fixture
def home(tmp_path, monkeypatch):
    import llm_route
    h = llm_route.harness()
    d = tmp_path / 'mem'
    d.mkdir()
    monkeypatch.setattr(h, 'MEMORY_DIR', d, raising=False)
    monkeypatch.setattr(h, 'MEMORY_INDEX', d / 'MEMORY.md', raising=False)
    memory._cache_clear()
    return d


def test_write_reports_action(home):
    _path, action = memory.write_memory_file('a', 'fact', 'first', 'body')
    assert action == memory_write.ADD


def test_write_updates_in_place(home):
    memory.write_memory_file('deploy', 'fact', 'deploy via make', 'v1')
    path, action = memory.write_memory_file('deploy-v2', 'fact',
                                            'deploy via make', 'v2')
    assert action == memory_write.UPDATE
    # 合并进旧文件，不产生第二个文件
    files = sorted(p.name for p in home.glob('*.md') if p.name != 'MEMORY.md')
    assert files == ['deploy.md']
    assert path.name == 'deploy.md'


def test_write_supersede_archives_old(home):
    memory.write_memory_file('deploy-script', 'fact',
                             'deploy by running script.sh', 'old')
    _path, action = memory.write_memory_file(
        'deploy-fix', 'correction', 'deploy via make ship-prod', 'new',
        supersedes='deploy-script')
    assert action == memory_write.SUPERSEDE
    live = [item['name'] for item in memory.list_memory_files()]
    assert live == ['deploy-fix']
    # 没有删除：旧文件在 .archive 里，且带 superseded_by 标记
    archived = home / '.archive' / 'deploy-script.md'
    assert archived.is_file()
    meta, _body = memory._parse_frontmatter(archived.read_text(encoding='utf-8'))
    assert meta.get('superseded_by') == 'deploy-fix.md'


def test_supersede_onto_itself_becomes_update(home):
    """新旧同名时按 supersede 处理会把刚写的内容归档掉，记忆凭空消失。"""
    memory.write_memory_file('deploy', 'fact', 'deploy via script', 'old')
    _path, action = memory.write_memory_file(
        'deploy', 'correction', 'deploy via make ship-prod', 'new',
        supersedes='deploy')
    assert action == memory_write.UPDATE
    live = [(item['name'], item['type'], item['description'])
            for item in memory.list_memory_files()]
    assert live == [('deploy', 'correction', 'deploy via make ship-prod')]


def test_keywords_are_redacted(home):
    """关键词从原始描述派生，不脱敏就会把密钥写进 frontmatter。"""
    path, _action = memory.write_memory_file(
        'creds', 'fact', 'key sk-' + 'a' * 24, 'body')
    text = path.read_text(encoding='utf-8')
    assert 'sk-' + 'a' * 24 not in text
