"""tool_patch：依赖注入、审批门与写盘回滚。"""
import pytest

import tool_patch


def _update_patch(path='a.txt'):
    return (f'*** Begin Patch\n*** Update File: {path}\n'
            '@@\n-old\n+new\n*** End Patch\n')


def _collaborators(root, *, denied=None, log=None):
    log = log if log is not None else []

    def resolve(path, cwd):
        log.append(('resolve', path))
        return root / path

    def capture(path):
        log.append(('capture', path.name))

    def gate(name, args):
        log.append(('gate', name))
        return denied

    return resolve, capture, gate


def test_apply_patch_uses_injected_hooks(tmp_path):
    (tmp_path / 'a.txt').write_text('old\n', encoding='utf-8')
    log = []
    resolve, capture, gate = _collaborators(tmp_path, log=log)

    result = tool_patch.apply_patch(
        _update_patch(), None, resolve=resolve, capture=capture, gate=gate)

    assert '1 updated' in result
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'new\n'
    # 顺序固定：先审批，再解析路径，写盘前捕获 checkpoint。
    assert [entry[0] for entry in log] == ['gate', 'resolve', 'capture']


def test_denied_patch_never_touches_disk(tmp_path):
    (tmp_path / 'a.txt').write_text('old\n', encoding='utf-8')
    log = []
    resolve, capture, gate = _collaborators(
        tmp_path, denied='Error: denied by policy', log=log)

    result = tool_patch.apply_patch(
        _update_patch(), None, resolve=resolve, capture=capture, gate=gate)

    assert result == 'Error: denied by policy'
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'old\n'
    assert [entry[0] for entry in log] == ['gate']


def test_invalid_patch_is_rejected_before_gate(tmp_path):
    log = []
    resolve, capture, gate = _collaborators(tmp_path, log=log)

    result = tool_patch.apply_patch(
        'not a patch', None, resolve=resolve, capture=capture, gate=gate)

    assert 'invalid patch' in result
    assert log == []


def test_apply_plans_rolls_back_on_failure(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('old', encoding='utf-8')
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a directory', encoding='utf-8')
    plans = [
        {'kind': 'write', 'path': target, 'data': b'new'},
        {'kind': 'write', 'path': blocker / 'c.txt', 'data': b'x'},
    ]

    with pytest.raises(OSError):
        tool_patch._apply_plans(plans, lambda path: None)

    assert target.read_text(encoding='utf-8') == 'old'


def test_patch_paths_includes_move_target():
    patch = ('*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n'
             '@@\n-x\n+y\n*** End Patch\n')

    assert tool_patch.patch_paths(patch) == ['old.py', 'new.py']


def test_parse_patch_reports_syntax_errors():
    with pytest.raises(tool_patch.PatchError, match='empty patch'):
        tool_patch.parse_patch('  ')
    with pytest.raises(tool_patch.PatchError, match='must start'):
        tool_patch.parse_patch('*** Update File: a.txt\n')
    with pytest.raises(tool_patch.PatchError, match='missing'):
        tool_patch.parse_patch('*** Begin Patch\n*** Delete File: a.txt\n')
