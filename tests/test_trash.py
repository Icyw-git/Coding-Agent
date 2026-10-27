"""可撤销删除：能力层、工具层、策略边界与真实回收站。"""
import os
from pathlib import Path

import pytest

import hooks
import tool_delete
import trash
from exec_policy import ASK, Policy

# ---------- 能力层 ----------

def test_available_reports_a_method():
    usable, method = trash.available()
    if os.name == 'nt':
        assert usable is True and method == 'recycle-bin'
    else:
        assert method in ('', 'trash-cli')


@pytest.mark.skipif(os.name != 'nt', reason='Windows recycle bin only')
def test_real_delete_moves_to_recycle_bin(tmp_path):
    target = tmp_path / 'to_trash.txt'
    target.write_text('bye', encoding='utf-8')

    result = trash.trash([target])

    assert len(result) == 1
    assert result[0].method == 'recycle-bin'
    assert result[0].recoverable is True
    assert not target.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows recycle bin only')
def test_real_delete_handles_directories(tmp_path):
    directory = tmp_path / 'subdir'
    directory.mkdir()
    (directory / 'inner.txt').write_text('x', encoding='utf-8')

    trash.trash([directory])
    assert not directory.exists()


def test_missing_path_is_rejected(tmp_path):
    with pytest.raises(trash.TrashError, match='does not exist'):
        trash.trash([tmp_path / 'nope.txt'])


def test_filesystem_root_is_refused():
    root = Path(Path.cwd().anchor)
    with pytest.raises(trash.TrashError, match='root'):
        trash.trash([root])


def test_empty_input_returns_empty():
    assert trash.trash([]) == []


def test_single_path_is_accepted(tmp_path):
    """传单个字符串不该被当成字符序列逐个删。"""
    target = tmp_path / 'one.txt'
    target.write_text('x', encoding='utf-8')
    if not trash.available()[0]:
        pytest.skip('no recycle bin on this platform')
    result = trash.trash(str(target))
    assert len(result) == 1 and result[0].path.endswith('one.txt')


def test_describe_status_mentions_availability():
    text = trash.describe_status()
    assert '可撤销删除' in text


# ---------- 工具层 ----------

class _Fake:
    def __init__(self, path, method='recycle-bin'):
        self.path = path
        self.method = method

    @property
    def recoverable(self):
        return True

    def describe(self):
        return f'{self.path} -> recycle bin'


def test_tool_formats_result_and_audits():
    events = []
    out = tool_delete.delete(['a.txt', 'b.txt'],
                            runner=lambda items: [_Fake(i) for i in items],
                            audit=lambda event, **data: events.append((event, data)))
    assert 'Moved 2 item(s) to the Recycle Bin' in out
    assert 'restore' in out.lower()
    assert events[0][0] == 'delete_trashed'
    assert events[0][1]['count'] == 2
    assert events[0][1]['method'] == 'recycle-bin'


def test_tool_reports_failure_without_raising():
    def boom(items):
        raise trash.TrashError('locked by another process')

    events = []
    out = tool_delete.delete(['a.txt'], runner=boom,
                            audit=lambda event, **data: events.append((event, data)))
    assert out.startswith('Error:')
    assert 'locked by another process' in out
    assert events[0][0] == 'delete_failed'


def test_tool_rejects_empty_paths():
    assert tool_delete.delete([]).startswith('Error:')


def test_tool_accepts_a_single_string():
    out = tool_delete.delete('a.txt', runner=lambda items: [_Fake(items[0])])
    assert 'Moved 1 item(s)' in out


def test_audit_failure_does_not_break_delete():
    def broken(event, **data):
        raise RuntimeError('audit down')

    out = tool_delete.delete(['a.txt'], runner=lambda items: [_Fake(i) for i in items],
                            audit=broken)
    assert 'Moved 1 item(s)' in out


# ---------- 策略边界 ----------

def test_delete_always_asks():
    decision = Policy().evaluate('delete_paths', {'paths': ['a.txt']})
    assert decision.action == ASK
    assert decision.code == 'delete_paths'


def test_delete_is_never_auto_allowed_by_tier():
    for mode in ('read-only', 'workspace-write'):
        decision = Policy(sandbox_mode=mode, approval_policy='on-request').evaluate(
            'delete_paths', {'paths': ['a.txt']})
        assert decision.action == ASK


def test_delete_is_denied_in_scheduled_turns():
    assert 'delete_paths' in hooks.SCHEDULED_FORBIDDEN_TOOLS


def test_delete_is_not_offered_to_subagents():
    import subagent

    assert 'delete_paths' in subagent._SUBAGENT_EXCLUDED


def test_delete_is_classified_destructive_for_recovery():
    import tool_recovery

    assert tool_recovery.classify('delete_paths', {'paths': ['a']}) == \
        tool_recovery.DESTRUCTIVE


# ---------- 审批面 ----------

def test_approval_detail_lists_every_target():
    import approval_view

    single = approval_view.detail('delete_paths', {'paths': ['D:/a.txt']})
    assert 'recycle bin' in single and 'D:/a.txt' in single

    many = approval_view.detail('delete_paths',
                                {'paths': [f'D:/f{i}.txt' for i in range(25)]})
    assert 'move 25 item(s)' in many
    assert '5 more' in many      # 只列前 20 条
