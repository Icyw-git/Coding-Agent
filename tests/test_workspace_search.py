"""跨已登记工作区的联合搜索。

不做全盘扫描的理由：性能（一次检索可能变成几分钟）与隐私（把没让 agent 看过的
目录纳进来）都不可接受。只搜 `/workspace add` 登记过的目录，并带明确预算。
"""
from __future__ import annotations

import pytest

import workspace_search
import workspaces


@pytest.fixture
def roots(tmp_path):
    """两个工作区，各含一个命中；b 里还有不该被搜到的秘密文件。"""
    a = tmp_path / 'proj-a'
    a.mkdir()
    (a / 'notes.md').write_text('deploy 流程\n', encoding='utf-8')
    b = tmp_path / 'proj-b'
    b.mkdir()
    (b / 'readme.md').write_text('deploy 走 make ship-prod\n', encoding='utf-8')
    (b / '.env').write_text('DEPLOY_KEY=secret\n', encoding='utf-8')
    registry = tmp_path / 'ws.json'
    workspaces.add_workspace(str(a), name='a', file=registry)
    workspaces.add_workspace(str(b), name='b', file=registry)
    return {'a': a, 'b': b, 'registry': registry}


def _fake_search(hits_by_root=None, scanned=1):
    """按 root 名返回预置命中。"""
    hits_by_root = hits_by_root or {}

    def run(root, budget):
        name = root.name
        hits = hits_by_root.get(name, [])
        return {'hits': hits[:budget['max_results']], 'scanned': scanned}

    return run


# ---------------------------------------------------------------- 目标解析

def test_targets_only_registered_workspaces(roots, tmp_path):
    extra = tmp_path / 'not-registered'
    extra.mkdir()
    targets = workspace_search.targets(file=roots['registry'])
    names = {path.name for path in targets}
    assert names == {'proj-a', 'proj-b'}
    assert 'not-registered' not in names


def test_targets_skip_missing_directories(roots):
    import shutil
    shutil.rmtree(roots['a'])
    names = {path.name for path in
             workspace_search.targets(file=roots['registry'])}
    assert names == {'proj-b'}


def test_targets_deduplicate(roots):
    import workspaces as ws
    ws.add_workspace(str(roots['a']), name='a-again', file=roots['registry'])
    targets = workspace_search.targets(file=roots['registry'])
    assert len(targets) == len({str(path).casefold() for path in targets})


def test_targets_honour_exclusion(roots):
    targets = workspace_search.targets(file=roots['registry'],
                                       exclude=[str(roots['a'])])
    assert {path.name for path in targets} == {'proj-b'}


def test_targets_empty_without_registry(tmp_path):
    assert workspace_search.targets(file=tmp_path / 'none.json') == []


# ---------------------------------------------------------------- 搜索与分组

def test_search_groups_by_workspace(roots):
    result = workspace_search.search(
        'deploy', roots=[roots['a'], roots['b']],
        search_workspace=_fake_search({'proj-a': ['a:1: deploy'],
                                       'proj-b': ['b:1: deploy']}))
    assert len(result['groups']) == 2
    assert result['groups'][0]['root'].endswith('proj-a')


def test_search_skips_workspaces_without_hits(roots):
    result = workspace_search.search(
        'deploy', roots=[roots['a'], roots['b']],
        search_workspace=_fake_search({'proj-b': ['b:1: deploy']}))
    assert len(result['groups']) == 1
    assert result['groups'][0]['root'].endswith('proj-b')


def test_search_stops_at_result_budget(roots):
    """命中够了就停，不为了让结果"完整"而扫到底。"""
    result = workspace_search.search(
        'deploy', roots=[roots['a'], roots['b']], max_results=1,
        search_workspace=_fake_search({'proj-a': ['x'], 'proj-b': ['y']}))
    assert result['truncated'] is True
    assert sum(len(g['hits']) for g in result['groups']) == 1


def test_search_stops_at_file_budget(roots):
    result = workspace_search.search(
        'deploy', roots=[roots['a'], roots['b']], max_files=1,
        search_workspace=_fake_search({'proj-a': ['x'], 'proj-b': ['y']},
                                      scanned=1))
    assert result['truncated'] is True


def test_search_requires_callback(roots):
    with pytest.raises(ValueError, match='search_workspace'):
        workspace_search.search('x', roots=[roots['a']])


# ---------------------------------------------------------------- 输出

def test_format_groups_labels_each_project(roots):
    """一串没有归属的路径等于没回答"在哪个项目里"。"""
    text = workspace_search.format_groups(
        {'groups': [{'root': str(roots['a']), 'hits': ['a:1: deploy']},
                    {'root': str(roots['b']), 'hits': ['b:1: deploy', 'b:2: x']}],
         'truncated': False, 'scanned': 2})
    assert '3 match(es) across 2 workspace(s)' in text
    assert 'proj-a  (1)' in text
    assert 'proj-b  (2)' in text


def test_format_groups_mentions_budget_when_truncated(roots):
    text = workspace_search.format_groups(
        {'groups': [{'root': str(roots['a']), 'hits': ['x']}],
         'truncated': True, 'scanned': 1})
    assert 'budget' in text


def test_format_empty_explains_next_step(roots):
    text = workspace_search.format_groups({'groups': [], 'scanned': 5})
    assert 'No matches' in text
    assert '/workspace add' in text          # 给出下一步


def test_format_truncates_long_groups(roots):
    many = [f'line {i}' for i in range(30)]
    text = workspace_search.format_groups(
        {'groups': [{'root': str(roots['a']), 'hits': many}]}, per_group=5)
    assert '还有 25 条' in text


# ---------------------------------------------------------------- 接入 grep

def test_grep_scope_workspaces_groups_output(roots, monkeypatch):
    """工具层：scope=workspaces 走联合搜索并分组。"""
    import tools

    monkeypatch.setattr('workspace_search.targets',
                        lambda **_kw: [roots['a'], roots['b']])
    monkeypatch.setattr(
        'workspace_search.search',
        lambda *a, **_kw: {'groups': [{'root': str(roots['a']),
                                       'hits': ['notes.md:1: deploy']}],
                           'truncated': False, 'scanned': 1})
    out = tools.run_grep('deploy', scope='workspaces')
    assert 'proj-a' in out
    assert 'notes.md:1' in out


def test_grep_scope_workspaces_without_registry(monkeypatch):
    import tools
    monkeypatch.setattr('workspace_search.targets', lambda **_kw: [])
    out = tools.run_grep('x', scope='workspaces')
    assert '/workspace add' in out


def test_grep_default_scope_still_single_path(tmp_path):
    """默认 scope=workspace 行为不变：只搜给定路径。"""
    import tools
    (tmp_path / 'a.txt').write_text('needle here\n', encoding='utf-8')
    out = tools.run_grep('needle', path=str(tmp_path))
    assert 'a.txt' in out
