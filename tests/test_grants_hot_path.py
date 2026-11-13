"""R1-b：不需要目录写授权的判定，不该去读授权账本（next-design §9.4）。

`hooks.evaluate_call` 原来**每次**都调 `grants.active_write_paths()`。但账本只在两处
真正被用到：

- `_evaluate_bash`：命令可能写到工作区外时；
- `_evaluate_file`：`path_zone == PATH_OUTSIDE` 时。

其余调用读它纯属白花时间——实测 200 条授权下 read 判定 p50 约 39–54ms，而 0 条时
1.3–1.7ms（profile 第 8 节）。read 是最频繁的工具，这笔开销每回合都在付。

**关键安全性质**：`write_grants` 只可能把 ASK 放宽成 ALLOW（`path_grant`），
永不收紧。所以「少传」只可能让判定更严，不可能更松。这条性质是本轮的验收基础。
"""

import pytest

import exec_policy
import grants
import hooks


@pytest.fixture()
def spy(monkeypatch):
    """拦 `grants.active_write_paths`，记录被调用次数。"""
    calls = {'n': 0}
    real = grants.active_write_paths

    def counting(*args, **kwargs):
        calls['n'] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(grants, 'active_write_paths', counting)
    return calls


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    ws.mkdir()
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'workspace-write')
    monkeypatch.setattr(hooks, '_h', lambda: type('H', (), {
        'DENY_LIST': ['sudo'],
        'DESTRUCTIVE': ['rm ', 'del '],
        'get_workdir': staticmethod(lambda: ws),
    })())
    return ws


def test_read_tool_does_not_touch_the_ledger(spy, workspace):
    hooks.evaluate_call('read', {'path': str(workspace / 'a.txt')})
    assert spy['n'] == 0, f'只读工具读了账本 {spy["n"]} 次'


@pytest.mark.parametrize('tool,args', [
    ('grep', {'pattern': 'x', 'path': '.'}),
    ('ls', {'path': '.'}),
    ('glob', {'pattern': '*.py'}),
    ('view_image', {'path': 'a.png'}),
    ('recall', {'query': 'x'}),
    ('web_fetch', {'url': 'https://example.com'}),
    ('screen_ocr', {}),
])
def test_non_writing_tools_do_not_touch_the_ledger(spy, workspace, tool, args):
    hooks.evaluate_call(tool, args)
    assert spy['n'] == 0, f'{tool} 读了账本 {spy["n"]} 次'


def test_readonly_bash_does_not_touch_the_ledger(spy, workspace):
    hooks.evaluate_call('bash', {'command': 'ls -la'})
    assert spy['n'] == 0, f'只读命令读了账本 {spy["n"]} 次'


def test_bash_that_may_write_outside_still_reads_the_ledger(spy, workspace):
    """可能越界写的命令仍要读账本：那正是授权能放宽的那条路径。"""
    hooks.evaluate_call('bash', {'command': 'rm -rf ' + str(workspace.parent / 'other')})
    assert spy['n'] == 1, '越界写必须完整检查授权'


def test_write_tool_outside_workspace_reads_the_ledger(spy, workspace):
    hooks.evaluate_call('write', {'path': str(workspace.parent / 'x.txt'),
                                  'content': 'hi'})
    assert spy['n'] == 1


def test_write_tool_inside_workspace_does_not_read_the_ledger(spy, workspace):
    """工作区内写入不需要目录授权——它本来就不 ASK。"""
    hooks.evaluate_call('write', {'path': str(workspace / 'x.txt'), 'content': 'hi'})
    assert spy['n'] == 0, f'工作区内写读了账本 {spy["n"]} 次'


def test_omitting_grants_can_only_make_the_decision_stricter(workspace, tmp_path):
    """核心安全性质：`write_grants` 只放宽，不可能收紧。

    拿一份真实授权，比较「不传 grants」与「传 grants」的判定：后者只能是同等或更宽。
    """
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'a.txt'
    decision_without = exec_policy.load_policy().evaluate(
        'write', {'path': str(target), 'content': 'x'},
        secrets=(), path_zone=exec_policy.PATH_OUTSIDE,
        workspace=str(workspace))
    decision_with = exec_policy.load_policy().evaluate(
        'write', {'path': str(target), 'content': 'x'},
        secrets=(), path_zone=exec_policy.PATH_OUTSIDE,
        workspace=str(workspace), write_grants=(outside.resolve(),))

    order = {'deny': 0, 'ask': 1, 'allow': 2}
    assert order[decision_with.action] >= order[decision_without.action], (
        decision_without, decision_with)
    assert decision_with.code == 'path_grant'


# ============================================================
# 决策矩阵：读不读账本不能改变判定
# ============================================================


def _decide(tool, args, *, zone, workspace, write_grants=()):
    return exec_policy.load_policy().evaluate(
        tool, args, deny_list=(), destructive=(), secrets=(),
        path_zone=zone, workspace=str(workspace), write_grants=write_grants)


@pytest.mark.parametrize('zone', [exec_policy.PATH_WORKSPACE,
                                  exec_policy.PATH_OUTSIDE,
                                  exec_policy.PATH_SYSTEM,
                                  exec_policy.PATH_SECRET])
def test_zone_matrix_is_unchanged_by_the_optimization(zone, workspace, tmp_path):
    """四种分区下，「按需求传 grants」与「一律传 grants」判定必须一致。"""
    target = tmp_path / 'outside' / 'a.txt'
    target.parent.mkdir(exist_ok=True)
    args = {'path': str(target), 'content': 'x'}
    grants_in = (tmp_path / 'outside').resolve()

    lazily = _decide('write', args, zone=zone, workspace=workspace,
                     write_grants=((grants_in,)
                                   if hooks.needs_write_grants('write', args, zone)
                                   else ()))
    eagerly = _decide('write', args, zone=zone, workspace=workspace,
                      write_grants=(grants_in,))
    assert (lazily.action, lazily.code) == (eagerly.action, eagerly.code), (
        zone, lazily, eagerly)


def test_expired_grant_is_ignored_either_way(workspace, tmp_path, monkeypatch):
    """到期授权：两种传法都必须不放大判定（授权失效不能因为少读而复活）。"""
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = str(outside / 'a.txt')
    args = {'path': path, 'content': 'x'}

    monkeypatch.setattr(grants, 'active_write_paths', lambda *a, **k: ())
    decided = hooks.evaluate_call('write', args)
    assert decided.action != 'allow', '到期的授权不该放行'

    inside = tmp_path / 'ws' / 'a.txt'
    args_in = {'path': str(inside), 'content': 'x'}
    assert hooks.evaluate_call('write', args_in).action == 'allow'


def test_revoked_grant_is_not_cached_across_calls(workspace, tmp_path, monkeypatch):
    """撤销后下一次判定必须收紧——不能因为“这次不读账本”而用上一次的结果。"""
    outside = tmp_path / 'outside'
    outside.mkdir()
    args = {'path': str(outside / 'a.txt'), 'content': 'x'}

    monkeypatch.setattr(grants, 'active_write_paths',
                        lambda *a, **k: (outside.resolve(),))
    assert hooks.evaluate_call('write', args).code == 'path_grant'

    monkeypatch.setattr(grants, 'active_write_paths', lambda *a, **k: ())
    assert hooks.evaluate_call('write', args).action == 'ask'


def test_needs_write_grants_covers_every_declared_file_tool():
    """矩阵式收口：声明成文件工具的，必须都在 `needs_write_grants` 的判定里。

    硬编码名单的风险是「加了新文件工具但忘了归类」——这条会在那时红。
    """
    for tool in exec_policy.READ_WRITE_TOOLS + exec_policy.READ_ONLY_FILE_TOOLS:
        outside = hooks.needs_write_grants(
            tool, {'path': 'x'}, exec_policy.PATH_OUTSIDE)
        inside = hooks.needs_write_grants(
            tool, {'path': 'x'}, exec_policy.PATH_WORKSPACE)
        assert inside is False, f'{tool} 在工作区内不该读账本'
        # 越界时读写都要读账本：`_evaluate_file` 的 PATH_OUTSIDE 分支是共用的，
        # 授权在那条分支里会把 ASK 放宽成 ALLOW——读写都走它，不能只算写工具。
        assert outside is True, tool
