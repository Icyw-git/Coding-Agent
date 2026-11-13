"""R1-c 其三：项目事实不能在别的项目里被召回（next-design §6.1）。

`workspace` 作用域上一批已经能存下来，但**召回不过滤**——等于没生效：A 项目的
「部署走 make ship-prod」到了 B 项目照样被注入，用户看到的是"它拿别的项目的规矩
管我"。

契约：

- `global`：任何工作区都召回（个人偏好）；
- `workspace`：只在 `owner` 与当前工作区一致时召回；
- `session`：只在写入它的那次会话里召回；
- 旧文件没有 owner（scope=global）→ 照旧召回，**不自动猜归属**；
  `/memories` 里标记「待确认」。
"""

import pytest

import memory


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    return tmp_path


def _owner(path) -> str:
    """工作区 owner：规范根路径（与 memory.workspace_owner 同一口径）。"""
    return memory.workspace_owner(path)


def _seed(alpha, beta):
    # 两条项目事实的措辞**必须不同**：措辞一样会被 memory_write 判成同一件事并合并
    # （那是既有设计，不是这里要测的东西）。
    memory.write_memory_file('alpha-deploy', 'fact', 'alpha 项目部署走 make ship-prod',
                             '正文', scope='workspace', owner=_owner(alpha))
    memory.write_memory_file('beta-deploy', 'fact', 'beta 项目部署用 scripts/release.sh',
                             '正文', scope='workspace', owner=_owner(beta))
    memory.write_memory_file('personal', 'preference', '部署前先备份再动手',
                             '正文', scope='global')


def _recall(alpha_or_beta, text='部署走什么'):
    """召回结果转成名字（`local_relevant_memories` 返回的是文件名）。"""
    names = memory.local_relevant_memories(
        [{'role': 'user', 'content': text}], workspace=alpha_or_beta)
    return [name.removesuffix('.md') for name in names]


def test_alpha_does_not_recall_beta_entries(home):
    alpha, beta = home / 'alpha', home / 'beta'
    alpha.mkdir()
    beta.mkdir()
    _seed(alpha, beta)

    names = _recall(alpha, '部署走什么 部署')
    assert 'alpha-deploy' in names, names
    assert 'beta-deploy' not in names, f'A 项目召回了 B 的事实：{names}'
    assert 'personal' in names, '全局偏好应当照旧召回'


def test_beta_symmetrically(home):
    alpha, beta = home / 'alpha', home / 'beta'
    alpha.mkdir()
    beta.mkdir()
    _seed(alpha, beta)

    names = _recall(beta, '部署用什么')
    assert 'beta-deploy' in names, names
    assert 'alpha-deploy' not in names, names


def test_session_scope_is_only_recalled_in_its_own_session(home, monkeypatch):
    alpha = home / 'alpha'
    alpha.mkdir()
    memory.write_memory_file('scratch', 'context', '这次先别托运',
                             '正文', scope='session', source='sess-1')

    import hooks

    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'sess-1')
    assert 'scratch' in _recall(alpha, '这次先别托运')

    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'sess-2')
    assert 'scratch' not in _recall(alpha, '这次先别托运')


def test_legacy_global_entries_still_recall_and_are_marked_pending(home):
    """旧文件没有 owner：照旧召回（不自动猜归属），但要能标出待确认。"""
    alpha = home / 'alpha'
    alpha.mkdir()
    memory.write_memory_file('legacy-project-fact', 'fact', '部署走 make ship-prod',
                             '正文', scope='global')
    assert 'legacy-project-fact' in _recall(alpha)

    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'legacy-project-fact')
    assert memory.workspace_pending_owner(entry) is True


def test_workspace_entry_without_owner_is_not_recalled_elsewhere(home):
    """scope=workspace 但没有 owner：归属不明，**不猜**，只在没有工作区时召回。"""
    alpha = home / 'alpha'
    alpha.mkdir()
    memory.write_memory_file('orphan', 'fact', '部署走 make ship-prod', '正文',
                             scope='workspace', owner='')
    assert 'orphan' not in _recall(alpha), '归属不明的工作区记忆不该被猜给这个项目'
